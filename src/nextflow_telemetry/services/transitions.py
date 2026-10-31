"""The one place that writes job and run status (ADR-0013 #373). Routers, the deadline
sweep (M4) and the event ingester (M5) call these functions inside their own transaction.
Every function is idempotent under a repeated call with the same arguments, except
``record_task``, which counts.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, case, exists, func, insert, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import settings
from ..control_db import (
    collection_samples_tbl,
    dead_letter_tbl,
    jobs_tbl,
    run_failed_tasks_tbl,
    run_task_counts_tbl,
    runs_tbl,
    samples_tbl,
    workflows_tbl,
)

if sys.version_info >= (3, 14):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7

RUN_TERMINAL = ("completed", "failed", "expired")
JOB_INFLIGHT = ("claimed", "submitted", "running")
SWEEP_REASON_DEFAULT = "run completed without MARK_COMPLETE"
RUN_FAILED_TASKS_MAX = 200

# The earliest sample of each readset is a hashed set, not a correlated min(): with stale statistics
# (right after a bulk load) the planner turned the correlated form into a quadratic index scan.
_RECONCILE_SQL = text(
    """
    insert into jobs (sample_id, workflow_pk, workflow_id, workflow_version, status, retry_count)
    select s.sample_id, w.id, w.workflow_id, w.version, 'pending', 0
      from workflows w cross join samples s
     where w.status = 'active'
       and (w.collections is null or exists (
             select 1 from collection_samples cs
              where cs.sample_id = s.sample_id and cs.collection_id = any(w.collections)))
       and (w.sample_key = 'sample_id'
            or s.id in (select min(id) from samples where readset_id is not null group by readset_id))
    on conflict (sample_id, workflow_pk) do nothing
    """
)

_PRIORITY_ORDER = (jobs_tbl.c.priority.desc(), jobs_tbl.c.created_at, jobs_tbl.c.id)


@dataclass(frozen=True)
class CloseResult:
    already_closed: bool
    swept: int


def _claim_ttl() -> timedelta:
    return timedelta(minutes=settings.CLAIM_TTL_MINUTES)


def _backstop() -> timedelta:
    return timedelta(hours=settings.SUBMIT_BACKSTOP_HOURS)


def _liveness() -> timedelta:
    return timedelta(minutes=settings.LIVENESS_MINUTES)


async def reconcile_jobs(conn: AsyncConnection) -> int:
    """One pending job per (sample or readset) x active workflow; returns the rows created."""
    return (await conn.execute(_RECONCILE_SQL)).rowcount


async def claim_batch(
    conn: AsyncConnection,
    *,
    limit: int,
    workflow_ids: list[str] | None,
    workflow_version: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Claim up to ``limit`` pending jobs of one workflow version and mint their run."""
    pick = (
        select(jobs_tbl.c.workflow_pk)
        .select_from(jobs_tbl.join(workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk))
        .where(jobs_tbl.c.status == "pending", workflows_tbl.c.status == "active")
        .order_by(*_PRIORITY_ORDER)
        .limit(1)
        .with_for_update(skip_locked=True, of=jobs_tbl)
    )
    if workflow_ids:
        pick = pick.where(jobs_tbl.c.workflow_id.in_(workflow_ids))
    if workflow_version:
        pick = pick.where(jobs_tbl.c.workflow_version == workflow_version)
    workflow_pk = (await conn.execute(pick)).scalar_one_or_none()
    if workflow_pk is None:
        return None

    taken = (
        await conn.execute(
            select(jobs_tbl.c.id, jobs_tbl.c.sample_id)
            .where(jobs_tbl.c.status == "pending", jobs_tbl.c.workflow_pk == workflow_pk)
            .order_by(*_PRIORITY_ORDER)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not taken:
        return None

    workflow = (await conn.execute(select(workflows_tbl).where(workflows_tbl.c.id == workflow_pk))).mappings().one()
    run_name = "r" + str(_uuid7())
    await conn.execute(
        insert(runs_tbl).values(
            run_name=run_name,
            workflow_id=workflow["workflow_id"],
            workflow_version=workflow["version"],
            workflow_pk=workflow_pk,
            revision=workflow["revision"],
            status="claimed",
            claimed_at=now,
            claim_expires_at=now + _claim_ttl(),
        )
    )
    await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.id.in_([t.id for t in taken]))
        .values(status="claimed", run_name=run_name)
    )

    sample_rows = (
        await conn.execute(
            select(
                samples_tbl.c.sample_id, samples_tbl.c.readset_id, samples_tbl.c.ncbi_accession, samples_tbl.c.metadata
            ).where(samples_tbl.c.sample_id.in_([t.sample_id for t in taken]))
        )
    ).mappings()
    samples = {r["sample_id"]: r for r in sample_rows}
    key = "readset_id" if workflow["sample_key"] == "readset_id" else "sample_id"
    return {
        "run_name": run_name,
        "workflow_pk": workflow_pk,
        "workflow_id": workflow["workflow_id"],
        "workflow_version": workflow["version"],
        "repository_url": workflow["repository_url"],
        "revision": workflow["revision"],
        "params": workflow["params"],
        "jobs": [
            {
                "sample_id": samples[t.sample_id][key],
                "ncbi_accession": samples[t.sample_id]["ncbi_accession"],
                "metadata": samples[t.sample_id]["metadata"],
            }
            for t in taken
        ],
    }


async def mark_submitted(conn: AsyncConnection, run_name: str, executor_job_id: str | None, *, now: datetime) -> bool:
    """claimed -> submitted. Returns False when the run isn't claimed."""
    updated = await conn.execute(
        update(runs_tbl)
        .where(runs_tbl.c.run_name == run_name, runs_tbl.c.status == "claimed")
        .values(
            status="submitted",
            submitted_at=now,
            executor_job_id=executor_job_id,
            claim_expires_at=None,
            submit_deadline=case((runs_tbl.c.liveness_deadline.is_(None), now + _backstop()), else_=None),
        )
    )
    if updated.rowcount == 0:
        return False
    await conn.execute(
        update(jobs_tbl).where(jobs_tbl.c.run_name == run_name, jobs_tbl.c.status == "claimed").values(status="submitted")
    )
    return True


async def mark_running(conn: AsyncConnection, run_name: str, run_id: str | None, *, now: datetime) -> None:
    """claimed or submitted -> running."""
    updated = await conn.execute(
        update(runs_tbl)
        .where(runs_tbl.c.run_name == run_name, runs_tbl.c.status.in_(("claimed", "submitted")))
        .values(
            status="running",
            run_id=run_id,
            started_at=now,
            claim_expires_at=None,
            submit_deadline=case(
                (
                    runs_tbl.c.liveness_deadline.is_(None),
                    func.coalesce(runs_tbl.c.submit_deadline, now + _backstop()),
                ),
                else_=None,
            ),
        )
    )
    if updated.rowcount:
        await conn.execute(
            update(jobs_tbl)
            .where(jobs_tbl.c.run_name == run_name, jobs_tbl.c.status.in_(("claimed", "submitted")))
            .values(status="running")
        )


async def complete_sample(conn: AsyncConnection, run_name: str, tag: str, *, now: datetime) -> int:
    """Complete the run's job for ``tag`` (a readset id or a sample id). Never flips a swept job."""
    readset_samples = select(samples_tbl.c.sample_id).where(samples_tbl.c.readset_id == tag)
    done = await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.status.notin_(("completed", "failed")),
            or_(jobs_tbl.c.sample_id == tag, jobs_tbl.c.sample_id.in_(readset_samples)),
        )
        .values(status="completed", completed_at=now)
    )
    return done.rowcount


async def record_task(
    conn: AsyncConnection, run_name: str, sample_id: str | None, trace: dict[str, Any], *, now: datetime
) -> None:
    """Count a task outcome and keep the first ``RUN_FAILED_TASKS_MAX`` non-COMPLETED tasks. Unknown runs are skipped."""
    status = str(trace.get("status") or "unknown")
    counted = pg_insert(run_task_counts_tbl).from_select(
        ["run_name", "status", "n"],
        select(literal(run_name, String), literal(status, String), literal(1)).where(
            exists().where(runs_tbl.c.run_name == run_name)
        ),
    )
    counted = counted.on_conflict_do_update(
        index_elements=["run_name", "status"], set_={"n": run_task_counts_tbl.c.n + 1}
    )
    if (await conn.execute(counted)).rowcount == 0 or status == "COMPLETED":
        return
    kept = (
        await conn.execute(
            select(func.count()).select_from(run_failed_tasks_tbl).where(run_failed_tasks_tbl.c.run_name == run_name)
        )
    ).scalar_one()
    if kept >= RUN_FAILED_TASKS_MAX:
        return
    await conn.execute(
        insert(run_failed_tasks_tbl).values(
            run_name=run_name,
            process=str(trace.get("process") or "unknown"),
            sample_id=sample_id,
            exit_code=None if trace.get("exit") is None else str(trace["exit"]),
            task_hash=trace.get("hash"),
            attempt=trace.get("attempt"),
            error_action=trace.get("error_action"),
            utc_time=now,
        )
    )


async def close_run(
    conn: AsyncConnection, run_name: str, terminal: str, reason: str | None, *, now: datetime
) -> CloseResult | None:
    """Close the run (if open) and sweep its in-flight jobs: retry within budget, else dead-letter.

    The sweep runs even when the run was already closed. Returns None for an unknown run.
    """
    status = (
        await conn.execute(select(runs_tbl.c.status).where(runs_tbl.c.run_name == run_name).with_for_update())
    ).scalar_one_or_none()
    if status is None:
        return None
    already_closed = status in RUN_TERMINAL
    if not already_closed:
        await conn.execute(
            update(runs_tbl)
            .where(runs_tbl.c.run_name == run_name)
            .values(
                status=terminal,
                completed_at=now,
                slurm_reason=func.coalesce(reason, runs_tbl.c.slurm_reason),
                claim_expires_at=None,
                submit_deadline=None,
                liveness_deadline=None,
            )
        )

    inflight = (
        await conn.execute(
            select(
                jobs_tbl.c.id,
                jobs_tbl.c.sample_id,
                jobs_tbl.c.workflow_id,
                jobs_tbl.c.workflow_version,
                jobs_tbl.c.retry_count,
                workflows_tbl.c.max_retries,
            )
            .select_from(jobs_tbl.join(workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk))
            .where(jobs_tbl.c.run_name == run_name, jobs_tbl.c.status.in_(JOB_INFLIGHT))
            .with_for_update(of=jobs_tbl)
        )
    ).all()
    retry = [j.id for j in inflight if j.retry_count < j.max_retries]
    dead = [j for j in inflight if j.retry_count >= j.max_retries]
    if retry:
        await conn.execute(
            update(jobs_tbl)
            .where(jobs_tbl.c.id.in_(retry))
            .values(
                retry_count=jobs_tbl.c.retry_count + 1,
                status="pending",
                run_name=None,
                failed_at=None,
                failure_reason=None,
            )
        )
    if dead:
        failure_reason = reason or SWEEP_REASON_DEFAULT
        await conn.execute(
            update(jobs_tbl)
            .where(jobs_tbl.c.id.in_([j.id for j in dead]))
            .values(
                retry_count=jobs_tbl.c.retry_count + 1,
                status="failed",
                failed_at=now,
                failure_reason=failure_reason,
            )
        )
        letters = pg_insert(dead_letter_tbl)
        await conn.execute(
            letters.on_conflict_do_update(
                index_elements=["job_id"],
                set_={
                    "run_name": letters.excluded.run_name,
                    "reason": letters.excluded.reason,
                    "created_at": letters.excluded.created_at,
                    "resolved_at": None,
                },
            ),
            [
                {
                    "job_id": j.id,
                    "run_name": run_name,
                    "sample_id": j.sample_id,
                    "workflow_id": j.workflow_id,
                    "workflow_version": j.workflow_version,
                    "reason": failure_reason,
                    "created_at": now,
                }
                for j in dead
            ],
        )
    return CloseResult(already_closed=already_closed, swept=len(inflight))


async def expire_claim(conn: AsyncConnection, run_name: str, *, now: datetime) -> bool:
    """claimed -> expired; its jobs go back to pending without burning a retry."""
    expired = await conn.execute(
        update(runs_tbl)
        .where(runs_tbl.c.run_name == run_name, runs_tbl.c.status == "claimed")
        .values(
            status="expired",
            completed_at=now,
            slurm_reason="claim expired without submitted confirmation",
            claim_expires_at=None,
            submit_deadline=None,
            liveness_deadline=None,
        )
    )
    if expired.rowcount == 0:
        return False
    await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.run_name == run_name, jobs_tbl.c.status == "claimed")
        .values(status="pending", run_name=None)
    )
    return True


async def apply_weblog_event(conn: AsyncConnection, body: dict[str, Any], *, now: datetime) -> None:
    """The state changes of one Nextflow weblog event (``POST /telemetry``)."""
    run_name = str(body.get("runName") or body.get("run_name") or "")
    event = str(body.get("event") or "")
    trace = body["trace"] if isinstance(body.get("trace"), dict) else None
    sample_id = str(trace["tag"]).split(":")[0] if trace and trace.get("tag") else None

    if event == "started":
        await mark_running(conn, run_name, body.get("runId") or body.get("run_id"), now=now)
    elif (
        event == "process_completed"
        and sample_id
        and trace is not None
        and str(trace.get("process") or "").endswith("MARK_COMPLETE")
        and trace.get("status") == "COMPLETED"
    ):
        await complete_sample(conn, run_name, sample_id, now=now)
    if event == "process_completed" and trace:
        await record_task(conn, run_name, sample_id, trace, now=now)
    if event == "completed":
        wf = (body.get("metadata") or {}).get("workflow") or {}
        if wf.get("success") is False:
            why = next(
                (wf[k] for k in ("errorMessage", "errorReport") if wf.get(k) is not None),
                "nextflow reported success=false",
            )
            await close_run(conn, run_name, "failed", str(why)[:1000], now=now)
        else:
            await close_run(conn, run_name, "completed", None, now=now)


async def _refresh_liveness(conn: AsyncConnection, run_name: str, *, now: datetime) -> None:
    """Any wrapper sign of life: stamp the heartbeat and, on an open run, make liveness the only deadline."""
    open_run = runs_tbl.c.status.notin_(RUN_TERMINAL)
    await conn.execute(
        update(runs_tbl)
        .where(runs_tbl.c.run_name == run_name)
        .values(
            last_heartbeat_at=now,
            claim_expires_at=case((open_run, None), else_=runs_tbl.c.claim_expires_at),
            submit_deadline=case((open_run, None), else_=runs_tbl.c.submit_deadline),
            liveness_deadline=case((open_run, now + _liveness()), else_=runs_tbl.c.liveness_deadline),
        )
    )


async def apply_run_event(conn: AsyncConnection, run_name: str, event: dict[str, Any], *, now: datetime) -> None:
    """The state changes of one wrapper or daemon event (``POST /runs/{run}/event``)."""
    kind = event.get("type")
    if kind == "wrapper_started":
        await mark_submitted(conn, run_name, None, now=now)
        await _refresh_liveness(conn, run_name, now=now)
    elif kind == "heartbeat":
        await _refresh_liveness(conn, run_name, now=now)
    elif kind == "pre_nextflow":
        if event.get("wait_seconds") is not None:
            await conn.execute(
                update(runs_tbl).where(runs_tbl.c.run_name == run_name).values(wait_seconds=event["wait_seconds"])
            )
    elif kind == "slurm_state":
        await conn.execute(
            update(runs_tbl)
            .where(runs_tbl.c.run_name == run_name)
            .values(last_known_slurm_state=event.get("state"), slurm_reason=event.get("reason"))
        )
    elif kind == "wrapper_exited":
        exit_code = event.get("exit_code")
        await conn.execute(
            update(runs_tbl).where(runs_tbl.c.run_name == run_name).values(wrapper_exit_code=exit_code)
        )
        ok = exit_code == 0
        await close_run(
            conn,
            run_name,
            "completed" if ok else "failed",
            None if ok else f"wrapper exited {exit_code}",
            now=now,
        )


async def mark_log_uploaded(conn: AsyncConnection, run_name: str, *, now: datetime) -> None:
    await conn.execute(update(runs_tbl).where(runs_tbl.c.run_name == run_name).values(nextflow_log_uploaded_at=now))


async def requeue_dead_letter(
    conn: AsyncConnection,
    *,
    workflow_pk: int | None,
    collection: str | None,
    job_ids: list[int] | None,
    now: datetime,
) -> int:
    """Resolve the matching unresolved dead letters and return their still-failed jobs to pending."""
    stmt = (
        select(dead_letter_tbl.c.id, dead_letter_tbl.c.job_id)
        .select_from(dead_letter_tbl.join(jobs_tbl, jobs_tbl.c.id == dead_letter_tbl.c.job_id))
        .where(dead_letter_tbl.c.resolved_at.is_(None))
        .with_for_update(of=dead_letter_tbl)
    )
    if workflow_pk is not None:
        stmt = stmt.where(jobs_tbl.c.workflow_pk == workflow_pk)
    if collection is not None:
        stmt = stmt.where(
            exists().where(
                collection_samples_tbl.c.sample_id == dead_letter_tbl.c.sample_id,
                collection_samples_tbl.c.collection_id == collection,
            )
        )
    if job_ids is not None:
        stmt = stmt.where(dead_letter_tbl.c.job_id.in_(job_ids))
    matched = (await conn.execute(stmt)).all()
    if not matched:
        return 0
    await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.id.in_([m.job_id for m in matched]), jobs_tbl.c.status == "failed")
        .values(status="pending", retry_count=0, run_name=None, failed_at=None, failure_reason=None)
    )
    await conn.execute(
        update(dead_letter_tbl).where(dead_letter_tbl.c.id.in_([m.id for m in matched])).values(resolved_at=now)
    )
    return len(matched)


async def reset_jobs_to_pending(conn: AsyncConnection, workflow_pk: int, *, now: datetime) -> int:
    """Return a workflow's running and failed jobs to pending, resolving the dead letters of the failed ones."""
    failed_jobs = select(jobs_tbl.c.id).where(jobs_tbl.c.workflow_pk == workflow_pk, jobs_tbl.c.status == "failed")
    await conn.execute(
        update(dead_letter_tbl)
        .where(dead_letter_tbl.c.resolved_at.is_(None), dead_letter_tbl.c.job_id.in_(failed_jobs))
        .values(resolved_at=now)
    )
    reset = await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.workflow_pk == workflow_pk, jobs_tbl.c.status.in_(("running", "failed")))
        .values(status="pending", run_name=None, retry_count=0, failed_at=None, failure_reason=None)
    )
    return reset.rowcount
