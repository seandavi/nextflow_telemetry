"""services/transitions.py (#373): the one writer of job and run status, on the testcontainers Postgres."""
from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nextflow_telemetry.config import settings
from nextflow_telemetry.control_db import (
    collection_samples_tbl,
    dead_letter_tbl,
    jobs_tbl,
    run_failed_tasks_tbl,
    run_task_counts_tbl,
    runs_tbl,
)
from nextflow_telemetry.control_models import RegisterWorkflowRequest, SampleRegistration
from nextflow_telemetry.services import registry, transitions

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
_runs = itertools.count(1000, 1000)


@pytest.fixture
async def engine(db_asyncpg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_asyncpg_url)
    yield engine
    await engine.dispose()


async def seed(
    engine: AsyncEngine, workflow_id: str, samples: int = 1, *, max_retries: int = 1, collection: str | None = None
) -> tuple[int, list[str]]:
    """A workflow scoped to ``collection`` (default: its own id) plus ``samples`` registered samples, with jobs
    reconciled; returns (pk, readset ids)."""
    first = next(_runs)
    collection = collection or workflow_id
    async with engine.begin() as conn:
        workflow, _ = await registry.register_workflow(
            conn,
            RegisterWorkflowRequest(
                workflow_id=workflow_id, version="1", repository_url="u", revision="r", max_retries=max_retries,
                collections=[collection],
            ),
            now=NOW,
        )
        rows = [
            SampleRegistration(sample_id=f"{workflow_id}-s{i}", ncbi_accession=f"SRR{first + i}", collection=collection)
            for i in range(samples)
        ]
        await registry.register_samples(conn, rows, now=NOW)
        await transitions.reconcile_jobs(conn)
    async with engine.connect() as conn:
        found = (await conn.execute(text("select readset_id from samples where sample_id like :p order by id"), {"p": f"{workflow_id}-s%"})).scalars()
        return workflow["id"], list(found)


async def claim(engine: AsyncEngine, workflow_id: str | None = None, limit: int = 10, now: datetime = NOW) -> dict[str, Any]:
    async with engine.begin() as conn:
        batch = await transitions.claim_batch(
            conn, limit=limit, workflow_ids=[workflow_id] if workflow_id else None, workflow_version=None, now=now
        )
    assert batch is not None
    return batch


async def run_row(engine: AsyncEngine, run_name: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(runs_tbl).where(runs_tbl.c.run_name == run_name))).mappings().one())


async def job_statuses(engine: AsyncEngine, pk: int) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(jobs_tbl.c.status).where(jobs_tbl.c.workflow_pk == pk).order_by(jobs_tbl.c.id))).scalars())


async def apply(engine: AsyncEngine, fn: Any, *args: Any, **kwargs: Any) -> Any:
    async with engine.begin() as conn:
        return await fn(conn, *args, **kwargs)


async def set_job_fields(engine: AsyncEngine, pk: int, **values: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(update(jobs_tbl).where(jobs_tbl.c.workflow_pk == pk).values(**values))


async def test_claim_order_priority_then_age(engine: AsyncEngine) -> None:
    pk_a, _ = await seed(engine, "ord-a", samples=2)
    pk_b, _ = await seed(engine, "ord-b", samples=2)
    async with engine.begin() as conn:
        # b's second job outranks everything; a's older job beats b's first.
        await conn.execute(update(jobs_tbl).where(jobs_tbl.c.workflow_pk == pk_a).values(created_at=NOW - timedelta(days=2)))
        await conn.execute(update(jobs_tbl).where(jobs_tbl.c.workflow_pk == pk_b).values(created_at=NOW - timedelta(days=1)))
        top = (await conn.execute(select(jobs_tbl.c.id).where(jobs_tbl.c.workflow_pk == pk_b).order_by(jobs_tbl.c.id.desc()))).scalars().first()
        await conn.execute(update(jobs_tbl).where(jobs_tbl.c.id == top).values(priority=5))

    first = await claim(engine, limit=1)
    assert first["workflow_pk"] == pk_b
    second = await claim(engine, limit=1)
    assert second["workflow_pk"] == pk_a


async def test_claim_skip_locked_disjoint(engine: AsyncEngine, db_asyncpg_url: str) -> None:
    pk, _ = await seed(engine, "skip", samples=4)
    other = create_async_engine(db_asyncpg_url)
    try:
        async with engine.connect() as holder:
            async with holder.begin():
                one = await transitions.claim_batch(holder, limit=2, workflow_ids=None, workflow_version=None, now=NOW)
                assert one is not None
                two = await claim(other, limit=2)
                assert {j["sample_id"] for j in one["jobs"]}.isdisjoint(j["sample_id"] for j in two["jobs"])
                assert len(one["jobs"]) == len(two["jobs"]) == 2
    finally:
        await other.dispose()
    assert await job_statuses(engine, pk) == ["claimed"] * 4


async def test_claim_sets_claim_deadline(engine: AsyncEngine) -> None:
    await seed(engine, "ttl")
    batch = await claim(engine, "ttl")
    run = await run_row(engine, batch["run_name"])
    assert run["status"] == "claimed"
    assert run["claim_expires_at"] == NOW + timedelta(minutes=settings.CLAIM_TTL_MINUTES)
    assert run["submit_deadline"] is None and run["liveness_deadline"] is None


async def test_claim_filters_and_readset_keys(engine: AsyncEngine) -> None:
    pk, readsets = await seed(engine, "keyed", samples=2)
    async with engine.begin() as conn:
        assert await transitions.claim_batch(conn, limit=5, workflow_ids=["nosuch"], workflow_version=None, now=NOW) is None
        assert await transitions.claim_batch(conn, limit=5, workflow_ids=["keyed"], workflow_version="9", now=NOW) is None
    batch = await claim(engine, "keyed", limit=1)
    assert [j["sample_id"] for j in batch["jobs"]] == readsets[:1]
    assert batch["workflow_pk"] == pk


async def test_submitted_swaps_to_backstop(engine: AsyncEngine) -> None:
    await seed(engine, "sub")
    run_name = (await claim(engine, "sub"))["run_name"]
    later = NOW + timedelta(minutes=1)
    assert await apply(engine, transitions.mark_submitted, run_name, "slurm-1", now=later) is True
    run = await run_row(engine, run_name)
    assert (run["status"], run["executor_job_id"], run["claim_expires_at"]) == ("submitted", "slurm-1", None)
    assert run["submit_deadline"] == later + timedelta(hours=settings.SUBMIT_BACKSTOP_HOURS)
    assert await apply(engine, transitions.mark_submitted, run_name, None, now=later) is False


async def test_heartbeat_sets_liveness_and_clears_others(engine: AsyncEngine) -> None:
    await seed(engine, "hb")
    run_name = (await claim(engine, "hb"))["run_name"]
    await apply(engine, transitions.mark_submitted, run_name, None, now=NOW)
    beat = NOW + timedelta(minutes=3)
    await apply(engine, transitions.apply_run_event, run_name, {"type": "heartbeat"}, now=beat)
    run = await run_row(engine, run_name)
    assert run["last_heartbeat_at"] == beat
    assert run["liveness_deadline"] == beat + timedelta(minutes=settings.LIVENESS_MINUTES)
    assert run["submit_deadline"] is None and run["claim_expires_at"] is None


async def test_started_on_claimed_run_replaces_claim_deadline(engine: AsyncEngine) -> None:
    await seed(engine, "started")
    run_name = (await claim(engine, "started"))["run_name"]
    await apply(engine, transitions.apply_weblog_event, {"runName": run_name, "runId": "nf-1", "event": "started"}, now=NOW)
    run = await run_row(engine, run_name)
    assert (run["status"], run["run_id"], run["claim_expires_at"]) == ("running", "nf-1", None)
    assert run["submit_deadline"] == NOW + timedelta(hours=settings.SUBMIT_BACKSTOP_HOURS)


async def test_wrapper_events_set_summary_fields(engine: AsyncEngine) -> None:
    await seed(engine, "wrap")
    run_name = (await claim(engine, "wrap"))["run_name"]
    await apply(engine, transitions.apply_run_event, run_name, {"type": "wrapper_started"}, now=NOW)
    await apply(engine, transitions.apply_run_event, run_name, {"type": "pre_nextflow", "wait_seconds": 42}, now=NOW)
    await apply(engine, transitions.apply_run_event, run_name, {"type": "slurm_state", "state": "RUNNING", "reason": "x"}, now=NOW)
    run = await run_row(engine, run_name)
    assert (run["status"], run["wait_seconds"], run["last_known_slurm_state"], run["slurm_reason"]) == ("submitted", 42, "RUNNING", "x")
    assert run["liveness_deadline"] is not None and run["submit_deadline"] is None

    await apply(engine, transitions.apply_run_event, run_name, {"type": "wrapper_exited", "exit_code": 3}, now=NOW)
    run = await run_row(engine, run_name)
    assert (run["status"], run["wrapper_exit_code"], run["slurm_reason"]) == ("failed", 3, "wrapper exited 3")
    assert run["liveness_deadline"] is None

    # A heartbeat arriving after the close doesn't reopen any deadline.
    await apply(engine, transitions.apply_run_event, run_name, {"type": "heartbeat"}, now=NOW + timedelta(minutes=1))
    assert (await run_row(engine, run_name))["liveness_deadline"] is None


async def test_close_run_retries_within_budget_and_dead_letters_beyond(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "close", max_retries=1)
    first = (await claim(engine, "close"))["run_name"]
    closed = await apply(engine, transitions.close_run, first, "failed", "boom", now=NOW)
    assert (closed.already_closed, closed.swept) == (False, 1)
    assert await job_statuses(engine, pk) == ["pending"]

    second = (await claim(engine, "close"))["run_name"]
    await apply(engine, transitions.close_run, second, "failed", None, now=NOW)
    assert await job_statuses(engine, pk) == ["failed"]
    async with engine.connect() as conn:
        job = (await conn.execute(select(jobs_tbl))).mappings().one()
        letter = (await conn.execute(select(dead_letter_tbl))).mappings().one()
    assert (job["retry_count"], job["failure_reason"]) == (2, transitions.SWEEP_REASON_DEFAULT)
    assert (letter["run_name"], letter["reason"], letter["resolved_at"]) == (second, transitions.SWEEP_REASON_DEFAULT, None)
    run = await run_row(engine, first)
    assert run["status"] == "failed" and run["slurm_reason"] == "boom"


async def test_close_run_idempotent(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "idem")
    run_name = (await claim(engine, "idem"))["run_name"]
    assert (await apply(engine, transitions.close_run, run_name, "completed", None, now=NOW)).already_closed is False
    again = await apply(engine, transitions.close_run, run_name, "failed", "late", now=NOW)
    assert (again.already_closed, again.swept) == (True, 0)
    assert (await run_row(engine, run_name))["status"] == "completed"
    assert await apply(engine, transitions.close_run, "nosuch", "completed", None, now=NOW) is None


async def test_redead_letter_resets_row(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "redead", max_retries=0)
    first = (await claim(engine, "redead"))["run_name"]
    await apply(engine, transitions.close_run, first, "failed", "first", now=NOW)
    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection=None, job_ids=None, now=NOW) == 1

    second = (await claim(engine, "redead"))["run_name"]
    later = NOW + timedelta(hours=1)
    await apply(engine, transitions.close_run, second, "failed", "second", now=later)
    async with engine.connect() as conn:
        letter = (await conn.execute(select(dead_letter_tbl))).mappings().one()
    assert (letter["reason"], letter["run_name"], letter["resolved_at"], letter["created_at"]) == ("second", second, None, later)


async def test_late_mark_complete_never_flips(engine: AsyncEngine) -> None:
    pk, readsets = await seed(engine, "late", max_retries=0)
    run_name = (await claim(engine, "late"))["run_name"]
    await apply(engine, transitions.close_run, run_name, "failed", None, now=NOW)
    assert await apply(engine, transitions.complete_sample, run_name, readsets[0], now=NOW) == 0
    assert await job_statuses(engine, pk) == ["failed"]


async def test_complete_sample_by_readset_or_sample_id(engine: AsyncEngine) -> None:
    pk, readsets = await seed(engine, "done", samples=2)
    batch = await claim(engine, "done")
    run_name = batch["run_name"]
    mark = {"process": "cmgd:MARK_COMPLETE", "status": "COMPLETED", "tag": f"{readsets[0]}:{run_name}"}
    await apply(engine, transitions.apply_weblog_event, {"runName": run_name, "event": "process_completed", "trace": mark}, now=NOW)
    assert sorted(await job_statuses(engine, pk)) == ["claimed", "completed"]
    assert await apply(engine, transitions.complete_sample, run_name, "done-s1", now=NOW) == 1


async def test_expire_claim_burns_no_retry(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "expire")
    run_name = (await claim(engine, "expire"))["run_name"]
    assert await apply(engine, transitions.expire_claim, run_name, now=NOW) is True
    run = await run_row(engine, run_name)
    assert (run["status"], run["claim_expires_at"]) == ("expired", None)
    async with engine.connect() as conn:
        job = (await conn.execute(select(jobs_tbl))).mappings().one()
    assert (job["status"], job["run_name"], job["retry_count"]) == ("pending", None, 0)
    assert await apply(engine, transitions.expire_claim, run_name, now=NOW) is False


async def test_weblog_completed_failure_reason(engine: AsyncEngine) -> None:
    await seed(engine, "weblog", max_retries=0)
    run_name = (await claim(engine, "weblog"))["run_name"]
    body = {"runName": run_name, "event": "completed", "metadata": {"workflow": {"success": False, "errorMessage": None, "errorReport": "disk full"}}}
    await apply(engine, transitions.apply_weblog_event, body, now=NOW)
    run = await run_row(engine, run_name)
    assert (run["status"], run["slurm_reason"]) == ("failed", "disk full")

    other = (await claim_after_requeue(engine, "weblog"))
    await apply(engine, transitions.apply_weblog_event, {"runName": other, "event": "completed", "metadata": {"workflow": {"success": True}}}, now=NOW)
    assert (await run_row(engine, other))["status"] == "completed"


async def claim_after_requeue(engine: AsyncEngine, workflow_id: str) -> str:
    await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection=None, job_ids=None, now=NOW)
    return str((await claim(engine, workflow_id))["run_name"])


async def test_record_task_counts_and_caps(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed(engine, "tasks")
    run_name = (await claim(engine, "tasks"))["run_name"]
    monkeypatch.setattr(transitions, "RUN_FAILED_TASKS_MAX", 2)
    await apply(engine, transitions.record_task, run_name, "s1", {"status": "COMPLETED", "process": "a"}, now=NOW)
    for attempt in (1, 2, 3):
        trace = {"status": "FAILED", "process": "kneaddata", "exit": 137, "hash": "ab/cdef01", "attempt": attempt, "error_action": "RETRY"}
        await apply(engine, transitions.record_task, run_name, "s1", trace, now=NOW)
    await apply(engine, transitions.record_task, "nosuch-run", None, {"status": "FAILED"}, now=NOW)

    async with engine.connect() as conn:
        counts = {s: n for s, n in await conn.execute(select(run_task_counts_tbl.c.status, run_task_counts_tbl.c.n))}
        failed = (await conn.execute(select(run_failed_tasks_tbl).order_by(run_failed_tasks_tbl.c.id))).mappings().all()
    assert counts == {"COMPLETED": 1, "FAILED": 3}
    assert [(f["process"], f["exit_code"], f["attempt"], f["task_hash"]) for f in failed] == [
        ("kneaddata", "137", 1, "ab/cdef01"),
        ("kneaddata", "137", 2, "ab/cdef01"),
    ]


async def test_requeue_dead_letter_filters(engine: AsyncEngine) -> None:
    pk_a, _ = await seed(engine, "rq-a", samples=2, max_retries=0, collection="rq-col-a")
    pk_b, _ = await seed(engine, "rq-b", samples=1, max_retries=0, collection="rq-col-b")
    for workflow_id in ("rq-a", "rq-b"):
        run_name = (await claim(engine, workflow_id))["run_name"]
        await apply(engine, transitions.close_run, run_name, "failed", None, now=NOW)
    async with engine.connect() as conn:
        letters = (await conn.execute(select(dead_letter_tbl.c.id, dead_letter_tbl.c.job_id, dead_letter_tbl.c.sample_id))).all()
    assert len(letters) == 3
    one_a = next(l.job_id for l in letters if l.sample_id == "rq-a-s0")

    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=pk_b, collection=None, job_ids=None, now=NOW) == 1
    assert await job_statuses(engine, pk_b) == ["pending"]
    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection="rq-col-b", job_ids=None, now=NOW) == 0
    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection=None, job_ids=[one_a], now=NOW) == 1
    assert sorted(await job_statuses(engine, pk_a)) == ["failed", "pending"]
    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection=None, job_ids=None, now=NOW) == 1
    assert await job_statuses(engine, pk_a) == ["pending", "pending"]


async def test_requeue_moves_only_failed_jobs(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "rq-state", max_retries=0)
    run_name = (await claim(engine, "rq-state"))["run_name"]
    await apply(engine, transitions.close_run, run_name, "failed", None, now=NOW)
    # reset-running already returned the job to pending and a new claim took it.
    await set_job_fields(engine, pk, status="pending", run_name=None, failed_at=None)
    second = (await claim(engine, "rq-state"))["run_name"]
    assert await apply(engine, transitions.requeue_dead_letter, workflow_pk=None, collection=None, job_ids=None, now=NOW) == 1
    async with engine.connect() as conn:
        job = (await conn.execute(select(jobs_tbl))).mappings().one()
    assert (job["status"], job["run_name"]) == ("claimed", second)


async def test_reset_running_resolves_dead_letter(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "reset", samples=2, max_retries=0)
    run_name = (await claim(engine, "reset", limit=1))["run_name"]
    await apply(engine, transitions.close_run, run_name, "failed", None, now=NOW)
    second = (await claim(engine, "reset", limit=1))["run_name"]
    await apply(engine, transitions.apply_weblog_event, {"runName": second, "event": "started"}, now=NOW)
    assert sorted(await job_statuses(engine, pk)) == ["failed", "running"]

    assert await apply(engine, transitions.reset_jobs_to_pending, pk, now=NOW) == 2
    assert await job_statuses(engine, pk) == ["pending", "pending"]
    async with engine.connect() as conn:
        assert (await conn.execute(select(dead_letter_tbl.c.resolved_at))).scalar_one() == NOW


async def test_reconcile_scoped_readset_deduped_idempotent(engine: AsyncEngine) -> None:
    pk, _ = await seed(engine, "scoped", samples=2, collection="rec-in")
    async with engine.begin() as conn:
        await registry.register_samples(conn, [SampleRegistration(sample_id="outside", ncbi_accession="SRR999001", collection="rec-out")], now=NOW)
        # Same runs, different sample id: one readset, so still one job.
        await registry.register_samples(conn, [SampleRegistration(sample_id="scoped-dup", ncbi_accession="SRR999002;SRR999003", collection="rec-in")], now=NOW)
        await registry.register_samples(conn, [SampleRegistration(sample_id="scoped-dup2", ncbi_accession="SRR999003,SRR999002", collection="rec-in")], now=NOW)
        await registry.register_samples(conn, [SampleRegistration(sample_id="no-readset", ncbi_accession="SRR999004;Not_applicable", collection="rec-in")], now=NOW)
    async with engine.begin() as conn:
        assert await transitions.reconcile_jobs(conn) == 1
    assert len(await job_statuses(engine, pk)) == 3
    async with engine.begin() as conn:
        assert await transitions.reconcile_jobs(conn) == 0


def test_event_value_coercion() -> None:
    assert [transitions._int(v) for v in (7, 1.9, -1.9, True, float("nan"), float("inf"), 2**31, -(2**31) - 1, "1", None)] == [
        7, 1, -1, None, None, None, None, None, None, None,
    ]
    assert transitions._int(2**31 - 1) == 2**31 - 1 and transitions._int(-(2**31)) == -(2**31)
    assert [transitions._str(v) for v in ("a\x00b", 3, 1.5, True, None, {"a": 1})] == ["ab", "3", "1.5", None, None, None]
    assert transitions._dict({"a": 1}) == {"a": 1} and transitions._dict([1]) == {} and transitions._dict(None) == {}


async def test_ill_typed_event_values_are_coerced(engine: AsyncEngine) -> None:
    await seed(engine, "ill")
    run_name = (await claim(engine, "ill"))["run_name"]
    bad_trace = {"status": "FAILED", "process": {"a": 1}, "exit": 1.5, "hash": 12, "attempt": 2.7, "error_action": ["x"], "tag": 5}
    await apply(engine, transitions.apply_weblog_event, {"runName": run_name, "event": "process_completed", "trace": bad_trace}, now=NOW)
    await apply(engine, transitions.apply_weblog_event, {"runName": run_name, "event": "started", "runId": 77}, now=NOW)
    await apply(engine, transitions.apply_run_event, run_name, {"type": "pre_nextflow", "wait_seconds": "soon"}, now=NOW)
    await apply(engine, transitions.apply_run_event, run_name, {"type": "slurm_state", "state": 4, "reason": ["x"]}, now=NOW)
    run = await run_row(engine, run_name)
    assert (run["run_id"], run["wait_seconds"], run["last_known_slurm_state"], run["slurm_reason"]) == ("77", None, "4", None)
    async with engine.connect() as conn:
        failed = (await conn.execute(select(run_failed_tasks_tbl))).mappings().one()
    assert (failed["process"], failed["sample_id"], failed["exit_code"], failed["task_hash"], failed["attempt"], failed["error_action"]) == (
        "unknown", "5", "1.5", "12", 2, None,
    )

    await apply(engine, transitions.apply_weblog_event, {"runName": run_name, "event": "completed", "metadata": "text"}, now=NOW)
    assert (await run_row(engine, run_name))["status"] == "completed"
    await seed(engine, "ill2")
    second = (await claim(engine, "ill2"))["run_name"]
    meta = {"workflow": {"success": False, "errorMessage": {"x": 1}, "errorReport": "why"}}
    await apply(engine, transitions.apply_weblog_event, {"runName": second, "event": "completed", "metadata": meta}, now=NOW)
    assert (await run_row(engine, second))["slurm_reason"] == "why"

    await seed(engine, "ill3")
    third = (await claim(engine, "ill3"))["run_name"]
    await apply(engine, transitions.apply_run_event, third, {"type": "wrapper_exited", "exit_code": "x"}, now=NOW)
    assert (await run_row(engine, third))["wrapper_exit_code"] is None


async def test_set_job_priority_pending_members_only(engine: AsyncEngine) -> None:
    pk_a, _ = await seed(engine, "pri-a", samples=2, collection="pri-col-a")
    pk_b, _ = await seed(engine, "pri-b", samples=1, collection="pri-col-b")

    async def priority(collection_id: str, value: int, workflow_pk: int | None = None) -> int | None:
        return await apply(engine, transitions.set_job_priority, collection_id=collection_id, priority=value, workflow_pk=workflow_pk)

    assert await priority("pri-col-a", -1) == 2
    assert await priority("pri-col-a", -1) == 2
    await claim(engine, "pri-a", limit=1)
    assert await priority("pri-col-a", 4) == 1
    assert await priority("pri-col-b", 3, pk_a) == 0
    assert await priority("pri-col-b", 3, pk_b) == 1
    assert await priority("nosuch", 1) is None
    assert await priority("pri-col-a", 1, 999999) is None

    async with engine.connect() as conn:
        rows = (await conn.execute(select(jobs_tbl.c.workflow_pk, jobs_tbl.c.status, jobs_tbl.c.priority))).all()
    assert sorted((r.status, r.priority) for r in rows if r.workflow_pk == pk_a) == [("claimed", -1), ("pending", 4)]
    assert [(r.status, r.priority) for r in rows if r.workflow_pk == pk_b] == [("pending", 3)]
