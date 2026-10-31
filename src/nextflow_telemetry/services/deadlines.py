"""Deadline sweep: replaces v2's per-run RunDO alarms (ADR-0013 decision 4, #374).

Expired rows are taken from ``runs`` with ``FOR UPDATE SKIP LOCKED``. When the gap
since the previous sweep is longer than the liveness window (the service was down),
every open deadline is first pushed out by the gap, so a run is not failed for
heartbeats nobody could receive.

Each expiry goes through the shared transition module (``transitions.expire_claim`` and
``transitions.close_run``), the only code that writes job and run status (#373).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..config import Settings
from ..control_db import deadline_sweep_tbl, runs_tbl
from ..log import logger
from . import transitions

CLAIM_EXPIRED_REASON = "claim expired without submitted confirmation"
BACKSTOP_REASON = "no wrapper activity within submit backstop"
LIVENESS_REASON = "presumed dead: heartbeats stopped"
RUN_TERMINAL = ("completed", "failed", "expired")
JOB_INFLIGHT = ("claimed", "submitted", "running")

_NO_DEADLINES = {"claim_expires_at": None, "submit_deadline": None, "liveness_deadline": None}


@dataclass(frozen=True)
class DeadlinePolicy:
    claim_ttl: timedelta
    submit_backstop: timedelta
    liveness: timedelta
    sweep_interval: timedelta

    def __post_init__(self) -> None:
        zero = timedelta(0)
        if min(self.claim_ttl, self.submit_backstop, self.liveness, self.sweep_interval) <= zero:
            raise ValueError("deadline policy durations must be positive")
        if self.sweep_interval * 2 > self.liveness:
            raise ValueError(
                "DEADLINE_SWEEP_SECONDS must be at most half of LIVENESS_MINUTES, "
                "or every sweep's own gap trips the outage grace period"
            )

    @classmethod
    def from_settings(cls, s: Settings) -> DeadlinePolicy:
        return cls(
            claim_ttl=timedelta(minutes=s.CLAIM_TTL_MINUTES),
            submit_backstop=timedelta(hours=s.SUBMIT_BACKSTOP_HOURS),
            liveness=timedelta(minutes=s.LIVENESS_MINUTES),
            sweep_interval=timedelta(seconds=s.DEADLINE_SWEEP_SECONDS),
        )


@dataclass
class SweepResult:
    skipped: bool = False            # another sweep holds deadline_sweep
    gap: timedelta | None = None     # since the previous sweep; None on the first
    shifted: int = 0                 # runs whose deadlines the grace period pushed out
    expired_claims: int = 0
    backstop_failures: int = 0
    liveness_failures: int = 0
    errors: int = 0                  # runs whose transition raised; retried next sweep


async def sweep_deadlines(conn: AsyncConnection, policy: DeadlinePolicy, now: datetime) -> SweepResult:
    """One sweep inside the caller's transaction. ``now`` is timezone-aware UTC."""
    await conn.execute(
        pg_insert(deadline_sweep_tbl).values(id=1).on_conflict_do_nothing(index_elements=["id"])
    )
    row = (await conn.execute(
        select(deadline_sweep_tbl.c.last_swept_at)
        .where(deadline_sweep_tbl.c.id == 1)
        .with_for_update(skip_locked=True)
    )).first()
    if row is None:
        return SweepResult(skipped=True)

    result = SweepResult()
    last_swept_at = row[0]
    if last_swept_at is not None:
        result.gap = gap = now - last_swept_at
        if gap > policy.liveness:
            # Waits on row locks instead of skipping them, so no open run misses the shift.
            shifted = await conn.execute(
                update(runs_tbl)
                .where(func.num_nonnulls(
                    runs_tbl.c.claim_expires_at, runs_tbl.c.submit_deadline,
                    runs_tbl.c.liveness_deadline) > 0)
                .values(
                    claim_expires_at=runs_tbl.c.claim_expires_at + gap,
                    submit_deadline=runs_tbl.c.submit_deadline + gap,
                    liveness_deadline=runs_tbl.c.liveness_deadline + gap,
                )
            )
            result.shifted = shifted.rowcount
            logger.warning(
                "deadline.sweep.grace",
                extra={"gap_seconds": gap.total_seconds(), "shifted": result.shifted},
            )

    expired = (await conn.execute(
        select(runs_tbl.c.run_name, runs_tbl.c.claim_expires_at, runs_tbl.c.submit_deadline,
               runs_tbl.c.liveness_deadline)
        .where(or_(runs_tbl.c.claim_expires_at <= now, runs_tbl.c.submit_deadline <= now,
                   runs_tbl.c.liveness_deadline <= now))
        .order_by(runs_tbl.c.run_name)
        .with_for_update(skip_locked=True)
    )).all()
    for run_name, claim_expires_at, submit_deadline, _liveness_deadline in expired:
        try:
            async with conn.begin_nested():
                # ck_runs_one_deadline: exactly one of the three is set.
                if claim_expires_at is not None:
                    await transitions.expire_claim(conn, run_name, now=now)
                    result.expired_claims += 1
                elif submit_deadline is not None:
                    await transitions.close_run(conn, run_name, "failed", BACKSTOP_REASON, now=now)
                    result.backstop_failures += 1
                else:
                    await transitions.close_run(conn, run_name, "failed", LIVENESS_REASON, now=now)
                    result.liveness_failures += 1
                # RunDO's deleteAll() after every alarm: a no-op transition must not
                # leave the row to be selected on every sweep.
                await conn.execute(
                    update(runs_tbl).where(runs_tbl.c.run_name == run_name).values(**_NO_DEADLINES)
                )
        except Exception:
            # The savepoint rolled back, so the run keeps its deadline for the next sweep.
            logger.exception("deadline.sweep.transition_failed", extra={"run_name": run_name})
            result.errors += 1

    await conn.execute(
        update(deadline_sweep_tbl).where(deadline_sweep_tbl.c.id == 1).values(last_swept_at=now)
    )
    return result


async def run_deadline_sweeps(engine: AsyncEngine, policy: DeadlinePolicy) -> None:
    """Background loop. Cancelling the task stops it."""
    while True:
        try:
            async with engine.begin() as conn:
                result = await sweep_deadlines(conn, policy, datetime.now(timezone.utc))
            if (result.shifted or result.expired_claims or result.backstop_failures
                    or result.liveness_failures or result.errors):
                logger.info("deadline.sweep", extra={
                    **asdict(result),
                    "gap": None if result.gap is None else result.gap.total_seconds(),
                })
        except Exception:
            logger.exception("deadline.sweep.failed")
        await asyncio.sleep(policy.sweep_interval.total_seconds())
