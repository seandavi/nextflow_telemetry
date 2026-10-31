"""Deadline sweep (services/deadlines.py, #374), driven with an injected clock."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from nextflow_telemetry.config import settings
from nextflow_telemetry.control_db import (
    dead_letter_tbl,
    deadline_sweep_tbl,
    ingest_cursor_tbl,
    jobs_tbl,
    runs_tbl,
    samples_tbl,
    workflows_tbl,
)
from nextflow_telemetry.services import deadlines
from nextflow_telemetry.services.event_ingest import EVENTS_STREAM
from nextflow_telemetry.services.deadlines import (
    BACKSTOP_REASON,
    CLAIM_EXPIRED_REASON,
    LIVENESS_REASON,
    DeadlinePolicy,
    SweepResult,
    sweep_deadlines,
)

T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
POLICY = DeadlinePolicy(
    timedelta(minutes=5), timedelta(hours=168), timedelta(minutes=10), timedelta(seconds=30))


@pytest.fixture()
async def engine(db_asyncpg_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(db_asyncpg_url)
    yield eng
    await eng.dispose()


async def _seed(
    conn: AsyncConnection, run_name: str, status: str, deadline_column: str | None,
    deadline: datetime | None, jobs: list[tuple[str, int]], max_retries: int = 3,
) -> int:
    """One run with one deadline, plus jobs given as (status, retry_count) under it."""
    pk = (await conn.execute(
        select(workflows_tbl.c.id).where(workflows_tbl.c.workflow_id == "wf"))).scalar()
    if pk is None:
        pk = (await conn.execute(
            insert(workflows_tbl)
            .values(workflow_id="wf", version="1", repository_url="https://example.test/wf",
                    revision="main", max_retries=max_retries)
            .returning(workflows_tbl.c.id)
        )).scalar_one()
    terminal = status in ("completed", "failed", "expired")
    await conn.execute(insert(runs_tbl).values(
        run_name=run_name, status=status, workflow_pk=pk,
        completed_at=T0 if terminal else None,
        **({deadline_column: deadline} if deadline_column else {})))
    for i, (job_status, retry_count) in enumerate(jobs):
        sample_id = f"{run_name}-s{i}"
        await conn.execute(insert(samples_tbl).values(sample_id=sample_id))
        await conn.execute(insert(jobs_tbl).values(
            sample_id=sample_id, workflow_pk=pk, workflow_id="wf", workflow_version="1",
            status=job_status, run_name=run_name, retry_count=retry_count,
            completed_at=T0 if job_status == "completed" else None,
            failed_at=T0 if job_status == "failed" else None))
    return pk


async def _sweep(engine: AsyncEngine, now: datetime) -> SweepResult:
    async with engine.begin() as conn:
        return await sweep_deadlines(conn, POLICY, now)


async def _run(engine: AsyncEngine, run_name: str):
    async with engine.connect() as conn:
        return (await conn.execute(
            select(runs_tbl).where(runs_tbl.c.run_name == run_name))).one()


async def _jobs(engine: AsyncEngine, run_name: str | None = None):
    async with engine.connect() as conn:
        q = select(jobs_tbl).order_by(jobs_tbl.c.id)
        return (await conn.execute(q)).all()


async def _dead_letters(engine: AsyncEngine):
    async with engine.connect() as conn:
        return (await conn.execute(select(dead_letter_tbl))).all()


def _no_deadlines(run) -> bool:
    return run.claim_expires_at is None and run.submit_deadline is None and run.liveness_deadline is None


async def test_claim_expiry_requeues_without_burning_a_retry(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at", deadline,
                    [("claimed", 1), ("claimed", 2)])
    await _sweep(engine, T0)
    now = deadline + timedelta(seconds=1)
    result = await _sweep(engine, now)

    assert result.expired_claims == 1
    run = await _run(engine, "r1")
    assert (run.status, run.slurm_reason, run.completed_at) == ("expired", CLAIM_EXPIRED_REASON, now)
    assert _no_deadlines(run)
    jobs = await _jobs(engine)
    assert [(j.status, j.run_name, j.retry_count) for j in jobs] == [
        ("pending", None, 1), ("pending", None, 2)]


async def test_unexpired_deadline_is_left_alone(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at", deadline, [("claimed", 0)])
    result = await _sweep(engine, deadline - timedelta(seconds=1))

    assert result.expired_claims == 0
    run = await _run(engine, "r1")
    assert (run.status, run.claim_expires_at) == ("claimed", deadline)
    assert (await _jobs(engine))[0].status == "claimed"


async def test_liveness_expiry_retries_within_budget(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "running", "liveness_deadline", deadline,
                    [("running", 0)], max_retries=1)
    result = await _sweep(engine, deadline)

    assert result.liveness_failures == 1
    run = await _run(engine, "r1")
    assert (run.status, run.slurm_reason) == ("failed", LIVENESS_REASON)
    assert _no_deadlines(run)
    job = (await _jobs(engine))[0]
    assert (job.status, job.retry_count, job.run_name) == ("pending", 1, None)
    assert await _dead_letters(engine) == []


async def test_liveness_expiry_dead_letters(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "running", "liveness_deadline", deadline,
                    [("running", 0)], max_retries=0)
    await _sweep(engine, deadline)

    assert (await _run(engine, "r1")).status == "failed"
    job = (await _jobs(engine))[0]
    assert (job.status, job.failed_at, job.failure_reason) == ("failed", deadline, LIVENESS_REASON)
    (dlq,) = await _dead_letters(engine)
    assert (dlq.job_id, dlq.run_name, dlq.reason) == (job.id, "r1", LIVENESS_REASON)


async def test_backstop_expiry_fails_run(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(hours=168)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "submitted", "submit_deadline", deadline,
                    [("submitted", 0), ("submitted", 1)], max_retries=1)
    result = await _sweep(engine, deadline)

    assert result.backstop_failures == 1
    run = await _run(engine, "r1")
    assert (run.status, run.slurm_reason) == ("failed", BACKSTOP_REASON)
    first, second = await _jobs(engine)
    assert (first.status, first.retry_count) == ("pending", 1)
    assert (second.status, second.retry_count) == ("failed", 2)
    assert len(await _dead_letters(engine)) == 1


async def test_failed_run_keeps_completed_jobs(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "running", "liveness_deadline", deadline,
                    [("completed", 0), ("running", 0)])
    await _sweep(engine, deadline)

    done, other = await _jobs(engine)
    assert (done.status, done.retry_count, done.run_name) == ("completed", 0, "r1")
    assert other.status == "pending"


async def test_outage_grace_pushes_open_deadlines_out(engine: AsyncEngine) -> None:
    claim, backstop, liveness = (T0 + timedelta(minutes=m) for m in (3, 60, 8))
    async with engine.begin() as conn:
        await _seed(conn, "rc", "claimed", "claim_expires_at", claim, [("claimed", 0)])
        await _seed(conn, "rb", "submitted", "submit_deadline", backstop, [("submitted", 0)])
        await _seed(conn, "rl", "running", "liveness_deadline", liveness, [("running", 0)])
        await _seed(conn, "rdone", "completed", None, None, [])
        await conn.execute(insert(deadline_sweep_tbl).values(id=1, last_swept_at=T0))
    gap = timedelta(hours=2)
    result = await _sweep(engine, T0 + gap)

    assert (result.shifted, result.gap) == (3, gap)
    assert result.expired_claims == result.backstop_failures == result.liveness_failures == 0
    assert (await _run(engine, "rc")).claim_expires_at == claim + gap
    assert (await _run(engine, "rb")).submit_deadline == backstop + gap
    assert (await _run(engine, "rl")).liveness_deadline == liveness + gap
    assert _no_deadlines(await _run(engine, "rdone"))

    after = await _sweep(engine, liveness + gap + timedelta(seconds=1))
    assert after.liveness_failures == 1
    assert (await _run(engine, "rl")).status == "failed"


@pytest.mark.parametrize("last_swept_at", [T0 - POLICY.liveness, None])
async def test_gap_within_liveness_and_first_sweep_do_not_shift(
        engine: AsyncEngine, last_swept_at: datetime | None) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at", deadline, [("claimed", 0)])
        await conn.execute(insert(deadline_sweep_tbl).values(id=1, last_swept_at=last_swept_at))
    result = await _sweep(engine, T0)

    assert result.shifted == 0
    assert (await _run(engine, "r1")).claim_expires_at == deadline


async def test_last_swept_at_is_recorded(engine: AsyncEngine) -> None:
    await _sweep(engine, T0)
    async with engine.connect() as conn:
        assert (await conn.execute(select(deadline_sweep_tbl.c.last_swept_at))).scalar_one() == T0
    await _sweep(engine, T0 + timedelta(seconds=30))
    async with engine.connect() as conn:
        assert (await conn.execute(select(deadline_sweep_tbl.c.last_swept_at))).scalar_one() == (
            T0 + timedelta(seconds=30))


async def test_concurrent_sweep_is_skipped(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at", deadline, [("claimed", 0)])
    await _sweep(engine, T0)
    async with engine.connect() as holder:
        await holder.execute(text("select 1 from deadline_sweep for update"))
        result = await _sweep(engine, deadline + timedelta(minutes=1))
    assert result.skipped
    assert (await _run(engine, "r1")).status == "claimed"


async def test_locked_run_is_skipped_until_released(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at", deadline, [("claimed", 0)])
    now = deadline + timedelta(seconds=1)
    async with engine.connect() as holder:
        await holder.execute(text("select 1 from runs where run_name = 'r1' for update"))
        result = await _sweep(engine, now)
        assert result.expired_claims == 0
        assert (await _run(engine, "r1")).status == "claimed"
    assert (await _sweep(engine, now + timedelta(seconds=30))).expired_claims == 1
    assert (await _run(engine, "r1")).status == "expired"


@pytest.mark.parametrize("cursor, stale", [
    (None, False),                                    # no cursor row: contract suite, pg-local
    ((None,), True),                                  # imported, never caught up
    ((T0 + timedelta(minutes=4, seconds=59),), True),  # just over liveness / 2 old
    ((T0 + timedelta(minutes=5),), False),            # exactly liveness / 2 old
])
async def test_ingest_stale_holds_liveness_and_backstop_not_claims(
        engine: AsyncEngine, cursor: tuple[datetime | None] | None, stale: bool) -> None:
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "rl", "running", "liveness_deadline", deadline, [("running", 0)])
        await _seed(conn, "rb", "submitted", "submit_deadline", deadline, [("submitted", 0)])
        await _seed(conn, "rc", "claimed", "claim_expires_at", deadline, [("claimed", 0)])
        if cursor is not None:
            await conn.execute(insert(ingest_cursor_tbl).values(
                stream=EVENTS_STREAM, object_key="", line_offset=0, caught_up_at=cursor[0]))
    result = await _sweep(engine, deadline)

    assert result.ingest_stale is stale
    assert result.expired_claims == 1
    assert (result.liveness_failures, result.backstop_failures) == ((0, 0) if stale else (1, 1))
    assert (await _run(engine, "rl")).status == ("running" if stale else "failed")
    assert (await _run(engine, "rb")).status == ("submitted" if stale else "failed")
    assert (await _run(engine, "rc")).status == "expired"


async def test_noop_transition_disarms_deadline(engine: AsyncEngine) -> None:
    deadline = T0 + timedelta(minutes=5)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "submitted", "claim_expires_at", deadline, [("submitted", 0)])
    await _sweep(engine, deadline)

    run = await _run(engine, "r1")
    assert run.status == "submitted"
    assert _no_deadlines(run)
    assert (await _jobs(engine))[0].status == "submitted"


async def test_failing_transition_is_isolated(engine: AsyncEngine, monkeypatch) -> None:
    """A transition that writes and then fails inside Postgres rolls back to its savepoint:
    that run keeps its deadline and its job, and the rest of the sweep still commits (#399)."""
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "bad", "running", "liveness_deadline", deadline, [("running", 0)])
        await _seed(conn, "good", "running", "liveness_deadline", deadline, [("running", 0)])
    real = deadlines.transitions.close_run

    async def flaky(conn, run_name, terminal, reason, *, now):
        out = await real(conn, run_name, terminal, reason, now=now)
        if run_name == "bad":
            await conn.execute(text("select 1/0"))  # an error after the writes, in the database
        return out

    monkeypatch.setattr(deadlines.transitions, "close_run", flaky)
    result = await _sweep(engine, deadline)

    assert (result.errors, result.liveness_failures) == (1, 1)
    bad = await _run(engine, "bad")
    assert (bad.status, bad.liveness_deadline) == ("running", deadline)
    assert [j.status for j in await _jobs(engine) if j.run_name == "bad"] == ["running"]
    assert (await _run(engine, "good")).status == "failed"
    async with engine.connect() as conn:
        swept = (await conn.execute(select(deadline_sweep_tbl.c.last_swept_at))).scalar_one()
    assert swept == deadline


async def test_requeued_dead_letter_reopens_on_a_second_failure(engine: AsyncEngine) -> None:
    """Dead-lettered, requeued (resolved_at set), then fails liveness again: the dead-letter
    row is open again, so GET /admin/dead-letter and the requeue filters see it (#405 code-r3-1)."""
    deadline = T0 + timedelta(minutes=10)
    async with engine.begin() as conn:
        await _seed(conn, "older-run", "failed", None, None, [])
        await _seed(conn, "r1", "running", "liveness_deadline", deadline, [("running", 3)], max_retries=3)
        job_id = (await conn.execute(select(jobs_tbl.c.id))).scalar_one()
        await conn.execute(insert(dead_letter_tbl).values(
            job_id=job_id, run_name="older-run", sample_id="r1-s0", workflow_id="wf",
            workflow_version="1", reason="earlier failure", created_at=T0, resolved_at=T0))
    await _sweep(engine, deadline)

    (job,) = await _jobs(engine)
    assert job.status == "failed"
    (dl,) = await _dead_letters(engine)
    assert dl.resolved_at is None
    assert dl.run_name == "r1"


def test_policy_from_settings_defaults() -> None:
    assert DeadlinePolicy.from_settings(settings) == DeadlinePolicy(
        timedelta(minutes=5), timedelta(hours=168), timedelta(minutes=10), timedelta(seconds=30))


def test_policy_rejects_bad_intervals() -> None:
    minute = timedelta(minutes=1)
    with pytest.raises(ValueError, match="DEADLINE_SWEEP_SECONDS"):
        DeadlinePolicy(minute, minute, minute, timedelta(seconds=31))
    DeadlinePolicy(minute, minute, minute, timedelta(seconds=30))
    for bad in range(4):
        values = [minute] * 3 + [timedelta(seconds=1)]
        values[bad] = timedelta(0)
        with pytest.raises(ValueError):
            DeadlinePolicy(*values)


async def test_sweep_loop_fires_in_real_time(engine: AsyncEngine) -> None:
    policy = DeadlinePolicy(
        timedelta(seconds=0.6), timedelta(hours=1), timedelta(seconds=1.2), timedelta(seconds=0.05))
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at",
                    datetime.now(timezone.utc) + timedelta(seconds=0.3), [("claimed", 0)])
    task = asyncio.create_task(deadlines.run_deadline_sweeps(engine, policy))
    try:
        for _ in range(100):
            if (await _run(engine, "r1")).status == "expired":
                break
            await asyncio.sleep(0.05)
        assert (await _run(engine, "r1")).status == "expired"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


async def test_lifespan_starts_sweep_when_enabled(
        engine: AsyncEngine, monkeypatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.setenv("DEADLINE_SWEEP_ENABLED", "1")
    monkeypatch.setenv("DEADLINE_SWEEP_SECONDS", "0.05")
    monkeypatch.setenv("LIVENESS_MINUTES", "0.01")
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at",
                    datetime.now(timezone.utc) - timedelta(seconds=1), [("claimed", 0)])
    request.getfixturevalue("integration_client")
    for _ in range(100):
        if (await _run(engine, "r1")).status == "expired":
            break
        await asyncio.sleep(0.05)
    assert (await _run(engine, "r1")).status == "expired"


async def test_lifespan_does_not_sweep_when_disabled(
        engine: AsyncEngine, monkeypatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.delenv("DEADLINE_SWEEP_ENABLED", raising=False)
    async with engine.begin() as conn:
        await _seed(conn, "r1", "claimed", "claim_expires_at",
                    datetime.now(timezone.utc) - timedelta(seconds=1), [("claimed", 0)])
    request.getfixturevalue("integration_client")
    await asyncio.sleep(0.5)
    assert (await _run(engine, "r1")).status == "claimed"
