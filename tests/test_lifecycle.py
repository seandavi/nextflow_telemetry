"""Tests for services/lifecycle.py — the job/run status-transition module.

Uses the same testcontainers Postgres fixture pattern as test_integration.py
(session-scoped schema via conftest.py, fresh AsyncEngine per test), but
drives transitions directly through lifecycle functions instead of via the
FastAPI TestClient — this is the pure-ish contract test for the module
itself, independent of the routers that call it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.db import (
    dead_letter_tbl,
    jobs_tbl,
    samples_tbl,
    workflow_runs_tbl,
    workflows_tbl,
)
from nextflow_telemetry.services import lifecycle
from nextflow_telemetry.services.lifecycle import JobStatus, RunStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio

    return asyncio.run(coro)


async def _seed_workflow(engine, *, max_retries: int = 3) -> int:
    now = datetime.now(timezone.utc)
    wf_id = f"lc-wf-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        res = await conn.execute(
            insert(workflows_tbl)
            .returning(workflows_tbl.c.id)
            .values(
                workflow_id=wf_id,
                version="1.0.0",
                repository_url="https://example.org/repo",
                revision="abc123",
                max_retries=max_retries,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        return res.scalar_one()


async def _seed_sample(engine) -> str:
    now = datetime.now(timezone.utc)
    sample_id = f"SRR-lc-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            insert(samples_tbl).values(
                sample_id=sample_id,
                ncbi_accession="SRR000001",
                created_at=now,
                updated_at=now,
            )
        )
    return sample_id


async def _seed_job(engine, *, workflow_pk: int, sample_id: str, status: str = "pending",
                     run_name: str | None = None, retry_count: int = 0) -> int:
    now = datetime.now(timezone.utc)
    wf_row_stmt = select(workflows_tbl.c.workflow_id, workflows_tbl.c.version).where(
        workflows_tbl.c.id == workflow_pk
    )
    async with engine.begin() as conn:
        wf_row = (await conn.execute(wf_row_stmt)).first()
        res = await conn.execute(
            insert(jobs_tbl)
            .returning(jobs_tbl.c.id)
            .values(
                sample_id=sample_id,
                workflow_pk=workflow_pk,
                workflow_id=wf_row.workflow_id,
                workflow_version=wf_row.version,
                run_name=run_name,
                status=status,
                retry_count=retry_count,
                created_at=now,
            )
        )
        return res.scalar_one()


async def _seed_run(engine, *, run_name: str, workflow_pk: int, workflow_id: str,
                     workflow_version: str, status: str = "claimed", claimed_at=None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(workflow_runs_tbl).values(
                run_name=run_name,
                workflow_id=workflow_id,
                workflow_version=workflow_version,
                workflow_pk=workflow_pk,
                revision="abc123",
                status=status,
                claimed_at=claimed_at or datetime.now(timezone.utc),
            )
        )


async def _get_job(engine, job_id: int) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(jobs_tbl).where(jobs_tbl.c.id == job_id))
        ).mappings().first()
        return dict(row)


async def _get_run(engine, run_name: str) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(workflow_runs_tbl).where(workflow_runs_tbl.c.run_name == run_name)
            )
        ).mappings().first()
        return dict(row)


# ---------------------------------------------------------------------------
# Happy path: claim -> submitted -> running -> complete_sample
# ---------------------------------------------------------------------------

def test_happy_path_claim_submitted_running_complete(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            sample_id = await _seed_sample(engine)
            job_id = await _seed_job(engine, workflow_pk=wf_pk, sample_id=sample_id)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            now = datetime.now(timezone.utc)

            async with engine.begin() as conn:
                await lifecycle.claim(
                    conn,
                    [job_id],
                    run_name,
                    {
                        "workflow_id": "lc-wf",
                        "workflow_version": "1.0.0",
                        "workflow_pk": wf_pk,
                        "revision": "abc123",
                        "claimed_at": now,
                    },
                )
            run = await _get_run(engine, run_name)
            job = await _get_job(engine, job_id)
            assert run["status"] == RunStatus.claimed
            assert job["status"] == JobStatus.claimed
            assert job["run_name"] == run_name

            async with engine.begin() as conn:
                ok = await lifecycle.mark_submitted(conn, run_name, "SLURM_1")
            assert ok is True
            run = await _get_run(engine, run_name)
            job = await _get_job(engine, job_id)
            assert run["status"] == RunStatus.submitted
            assert run["executor_job_id"] == "SLURM_1"
            assert job["status"] == JobStatus.submitted

            async with engine.begin() as conn:
                await lifecycle.mark_running(conn, run_name, "nf-run-id-1", now)
            run = await _get_run(engine, run_name)
            job = await _get_job(engine, job_id)
            assert run["status"] == RunStatus.running
            assert run["run_id"] == "nf-run-id-1"
            assert job["status"] == JobStatus.running

            async with engine.begin() as conn:
                n = await lifecycle.complete_sample(conn, run_name, sample_id, now)
            assert n == 1
            job = await _get_job(engine, job_id)
            assert job["status"] == JobStatus.completed
            assert job["completed_at"] is not None
        finally:
            await engine.dispose()

    _run(go())


# ---------------------------------------------------------------------------
# Tolerant no-ops
# ---------------------------------------------------------------------------

def test_mark_running_tolerates_claimed_and_submitted_predecessors(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            now = datetime.now(timezone.utc)

            for predecessor in ("claimed", "submitted"):
                sample_id = await _seed_sample(engine)
                run_name = f"run-{uuid.uuid4().hex[:8]}"
                await _seed_run(
                    engine, run_name=run_name, workflow_pk=wf_pk,
                    workflow_id="lc-wf", workflow_version="1.0.0", status=predecessor,
                )
                job_id = await _seed_job(
                    engine, workflow_pk=wf_pk, sample_id=sample_id,
                    status=predecessor, run_name=run_name,
                )
                async with engine.begin() as conn:
                    await lifecycle.mark_running(conn, run_name, "nf-run-id", now)
                job = await _get_job(engine, job_id)
                assert job["status"] == JobStatus.running, predecessor
        finally:
            await engine.dispose()

    _run(go())


def test_mark_submitted_no_match_returns_false_without_raising(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            async with engine.begin() as conn:
                ok = await lifecycle.mark_submitted(conn, "no-such-run", "X")
            assert ok is False
        finally:
            await engine.dispose()

    _run(go())


def test_complete_sample_no_match_returns_zero(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                n = await lifecycle.complete_sample(conn, "no-such-run", "no-such-sample", now)
            assert n == 0
        finally:
            await engine.dispose()

    _run(go())


def test_close_run_no_such_run_returns_none(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                prior = await lifecycle.close_run(conn, "no-such-run", RunStatus.completed, now)
            assert prior is None
        finally:
            await engine.dispose()

    _run(go())


def test_close_run_returns_prior_status_and_is_idempotent(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            await _seed_run(
                engine, run_name=run_name, workflow_pk=wf_pk,
                workflow_id="lc-wf", workflow_version="1.0.0", status="running",
            )
            now = datetime.now(timezone.utc)

            async with engine.begin() as conn:
                prior = await lifecycle.close_run(conn, run_name, RunStatus.completed, now)
            assert prior == "running"
            run = await _get_run(engine, run_name)
            assert run["status"] == RunStatus.completed
            first_completed_at = run["completed_at"]

            # Second call: already terminal -> no-op write, but prior status
            # returned is the (now terminal) status so callers can tell
            # "already closed" apart from "closed just now".
            later = now + timedelta(seconds=5)
            async with engine.begin() as conn:
                prior2 = await lifecycle.close_run(conn, run_name, RunStatus.failed, later)
            assert prior2 == "completed"
            run = await _get_run(engine, run_name)
            assert run["status"] == RunStatus.completed  # unchanged, not flipped to failed
            assert run["completed_at"] == first_completed_at
        finally:
            await engine.dispose()

    _run(go())


# ---------------------------------------------------------------------------
# sweep_incomplete: retry-vs-DLQ branches
# ---------------------------------------------------------------------------

def test_sweep_incomplete_retries_within_budget(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine, max_retries=3)
            sample_id = await _seed_sample(engine)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            await _seed_run(
                engine, run_name=run_name, workflow_pk=wf_pk,
                workflow_id="lc-wf", workflow_version="1.0.0", status="running",
            )
            job_id = await _seed_job(
                engine, workflow_pk=wf_pk, sample_id=sample_id,
                status="running", run_name=run_name, retry_count=0,
            )
            now = datetime.now(timezone.utc)

            async with engine.begin() as conn:
                n = await lifecycle.sweep_incomplete(conn, run_name, now)
            assert n == 1
            job = await _get_job(engine, job_id)
            assert job["status"] == JobStatus.pending
            assert job["run_name"] is None
            assert job["retry_count"] == 1
            assert job["failed_at"] is None

            async with engine.connect() as conn:
                dlq = (
                    await conn.execute(
                        select(dead_letter_tbl).where(dead_letter_tbl.c.job_id == job_id)
                    )
                ).mappings().all()
            assert dlq == []
        finally:
            await engine.dispose()

    _run(go())


def test_sweep_incomplete_dead_letters_when_retries_exhausted(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine, max_retries=1)
            sample_id = await _seed_sample(engine)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            await _seed_run(
                engine, run_name=run_name, workflow_pk=wf_pk,
                workflow_id="lc-wf", workflow_version="1.0.0", status="running",
            )
            job_id = await _seed_job(
                engine, workflow_pk=wf_pk, sample_id=sample_id,
                status="running", run_name=run_name, retry_count=1,
            )
            now = datetime.now(timezone.utc)

            async with engine.begin() as conn:
                n = await lifecycle.sweep_incomplete(conn, run_name, now)
            assert n == 1
            job = await _get_job(engine, job_id)
            assert job["status"] == JobStatus.failed
            assert job["run_name"] == run_name  # left in place on failure
            assert job["retry_count"] == 2
            assert job["failed_at"] is not None

            async with engine.connect() as conn:
                dlq = (
                    await conn.execute(
                        select(dead_letter_tbl).where(dead_letter_tbl.c.job_id == job_id)
                    )
                ).mappings().all()
            assert len(dlq) == 1
            assert dlq[0]["reason"] == "run completed without MARK_COMPLETE"
        finally:
            await engine.dispose()

    _run(go())


def test_sweep_incomplete_matches_nothing_is_a_noop(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                n = await lifecycle.sweep_incomplete(conn, "no-such-run", now)
            assert n == 0
        finally:
            await engine.dispose()

    _run(go())


# ---------------------------------------------------------------------------
# requeue_expired
# ---------------------------------------------------------------------------

def test_requeue_expired_resets_to_pending_no_retry_penalty(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            sample_id = await _seed_sample(engine)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            now = datetime.now(timezone.utc)
            stale_claimed_at = now - timedelta(minutes=10)
            await _seed_run(
                engine, run_name=run_name, workflow_pk=wf_pk,
                workflow_id="lc-wf", workflow_version="1.0.0",
                status="claimed", claimed_at=stale_claimed_at,
            )
            job_id = await _seed_job(
                engine, workflow_pk=wf_pk, sample_id=sample_id,
                status="claimed", run_name=run_name, retry_count=0,
            )

            cutoff = now - timedelta(minutes=5)
            async with engine.begin() as conn:
                count = await lifecycle.requeue_expired(conn, cutoff)
            assert count == 1

            run = await _get_run(engine, run_name)
            assert run["status"] == RunStatus.expired

            job = await _get_job(engine, job_id)
            assert job["status"] == JobStatus.pending
            assert job["run_name"] is None
            assert job["retry_count"] == 0  # no retry penalty for expiry
        finally:
            await engine.dispose()

    _run(go())


def test_requeue_expired_ignores_fresh_claims(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            run_name = f"run-{uuid.uuid4().hex[:8]}"
            now = datetime.now(timezone.utc)
            await _seed_run(
                engine, run_name=run_name, workflow_pk=wf_pk,
                workflow_id="lc-wf", workflow_version="1.0.0",
                status="claimed", claimed_at=now,
            )
            cutoff = now - timedelta(minutes=5)
            async with engine.begin() as conn:
                count = await lifecycle.requeue_expired(conn, cutoff)
            assert count == 0
            run = await _get_run(engine, run_name)
            assert run["status"] == RunStatus.claimed
        finally:
            await engine.dispose()

    _run(go())


# ---------------------------------------------------------------------------
# reset_jobs_to_pending / requeue_dead_letter
# ---------------------------------------------------------------------------

def test_reset_jobs_to_pending(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            s1 = await _seed_sample(engine)
            s2 = await _seed_sample(engine)
            job1 = await _seed_job(engine, workflow_pk=wf_pk, sample_id=s1, status="running")
            job2 = await _seed_job(engine, workflow_pk=wf_pk, sample_id=s2, status="failed", retry_count=2)

            async with engine.begin() as conn:
                n = await lifecycle.reset_jobs_to_pending(
                    conn, wf_pk, [JobStatus.running, JobStatus.failed]
                )
            assert n == 2
            for job_id in (job1, job2):
                job = await _get_job(engine, job_id)
                assert job["status"] == JobStatus.pending
                assert job["retry_count"] == 0
                assert job["run_name"] is None
        finally:
            await engine.dispose()

    _run(go())


def test_requeue_dead_letter(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            wf_pk = await _seed_workflow(engine)
            sample_id = await _seed_sample(engine)
            job_id = await _seed_job(engine, workflow_pk=wf_pk, sample_id=sample_id, status="failed", retry_count=3)
            now = datetime.now(timezone.utc)
            async with engine.begin() as conn:
                res = await conn.execute(
                    insert(dead_letter_tbl)
                    .returning(dead_letter_tbl.c.id)
                    .values(
                        job_id=job_id,
                        run_name="whatever",
                        sample_id=sample_id,
                        workflow_id="lc-wf",
                        workflow_version="1.0.0",
                        reason="test",
                        created_at=now,
                    )
                )
                dlq_id = res.scalar_one()

            async with engine.begin() as conn:
                n = await lifecycle.requeue_dead_letter(conn, [job_id], [dlq_id], now)
            assert n == 1
            job = await _get_job(engine, job_id)
            assert job["status"] == JobStatus.pending
            assert job["retry_count"] == 0
            assert job["run_name"] is None

            async with engine.connect() as conn:
                dlq_row = (
                    await conn.execute(
                        select(dead_letter_tbl).where(dead_letter_tbl.c.id == dlq_id)
                    )
                ).mappings().first()
            assert dlq_row["resolved_at"] is not None
        finally:
            await engine.dispose()

    _run(go())
