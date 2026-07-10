"""Tests for services/dispatch.py — DispatchService.

Same testcontainers Postgres fixture pattern as test_lifecycle.py: drives
the service directly (no FastAPI TestClient) against a fresh AsyncEngine.
test_integration.py / test_e2e.py already cover the same behaviour through
the HTTP layer — these are the behaviour-preservation proof for the service
extraction itself.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.db import (
    jobs_tbl,
    samples_tbl,
    workflow_runs_tbl,
    workflows_tbl,
)
from nextflow_telemetry.services.dispatch import DispatchService


def _run(coro):
    return asyncio.run(coro)


async def _seed_workflow(engine, *, max_retries: int = 3) -> tuple[int, str]:
    now = datetime.now(timezone.utc)
    wf_id = f"ds-wf-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        pk = (
            await conn.execute(
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
        ).scalar_one()
    return pk, wf_id


async def _seed_sample(engine, *, ncbi_accession: str = "SRR000001") -> str:
    now = datetime.now(timezone.utc)
    sample_id = f"SRR-ds-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            insert(samples_tbl).values(
                sample_id=sample_id,
                ncbi_accession=ncbi_accession,
                metadata_={"foo": "bar"},
                created_at=now,
                updated_at=now,
            )
        )
    return sample_id


async def _seed_job(
    engine, *, workflow_pk: int, workflow_id: str, sample_id: str, status: str = "pending"
) -> int:
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        job_id = (
            await conn.execute(
                insert(jobs_tbl)
                .returning(jobs_tbl.c.id)
                .values(
                    sample_id=sample_id,
                    workflow_pk=workflow_pk,
                    workflow_id=workflow_id,
                    workflow_version="1.0.0",
                    status=status,
                    created_at=now,
                )
            )
        ).scalar_one()
    return job_id


async def _get_job(engine, job_id: int) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(jobs_tbl).where(jobs_tbl.c.id == job_id))
        ).mappings().first()
        return dict(row)


def test_claim_batch_claims_pending_jobs_then_none_when_empty(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            svc = DispatchService(engine=engine)
            wf_pk, wf_id = await _seed_workflow(engine)
            sample_a = await _seed_sample(engine)
            sample_b = await _seed_sample(engine)
            job_a = await _seed_job(engine, workflow_pk=wf_pk, workflow_id=wf_id, sample_id=sample_a)
            job_b = await _seed_job(engine, workflow_pk=wf_pk, workflow_id=wf_id, sample_id=sample_b)

            batch = await svc.claim_batch(limit=10, workflow_id=[wf_id])
            assert batch is not None
            assert batch.run_name.startswith("r")
            assert batch.workflow_id == wf_id
            assert batch.workflow_version == "1.0.0"
            assert batch.workflow_pk == wf_pk
            assert batch.repository_url == "https://example.org/repo"
            assert batch.revision == "abc123"
            assert {j.sample_id for j in batch.jobs} == {sample_a, sample_b}
            for j in batch.jobs:
                assert j.ncbi_accession == "SRR000001"
                assert j.metadata == {"foo": "bar"}

            for job_id in (job_a, job_b):
                job = await _get_job(engine, job_id)
                assert job["status"] == "claimed"
                assert job["run_name"] == batch.run_name

            # Nothing left pending for this workflow.
            empty = await svc.claim_batch(limit=10, workflow_id=[wf_id])
            assert empty is None
        finally:
            await engine.dispose()

    _run(go())


def test_report_submitted_true_then_false(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            svc = DispatchService(engine=engine)
            wf_pk, wf_id = await _seed_workflow(engine)
            sample_id = await _seed_sample(engine)
            await _seed_job(engine, workflow_pk=wf_pk, workflow_id=wf_id, sample_id=sample_id)

            batch = await svc.claim_batch(limit=10, workflow_id=[wf_id])
            assert batch is not None

            ok = await svc.report_submitted(batch.run_name, "SLURM_1")
            assert ok is True

            # Already submitted — second confirmation is a no-op / False.
            ok_again = await svc.report_submitted(batch.run_name, "SLURM_1")
            assert ok_again is False

            # Unknown run name.
            unknown = await svc.report_submitted("no-such-run", None)
            assert unknown is False
        finally:
            await engine.dispose()

    _run(go())


def test_requeue_expired_recycles_stale_claims(db_asyncpg_url):
    async def go():
        engine = create_async_engine(db_asyncpg_url)
        try:
            svc = DispatchService(engine=engine)
            wf_pk, wf_id = await _seed_workflow(engine)
            sample_id = await _seed_sample(engine)

            stale_run_name = f"r-stale-{uuid.uuid4().hex[:8]}"
            stale_claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with engine.begin() as conn:
                await conn.execute(
                    insert(workflow_runs_tbl).values(
                        run_name=stale_run_name,
                        workflow_id=wf_id,
                        workflow_version="1.0.0",
                        workflow_pk=wf_pk,
                        revision="abc123",
                        status="claimed",
                        claimed_at=stale_claimed_at,
                    )
                )
            job_id = await _seed_job(
                engine, workflow_pk=wf_pk, workflow_id=wf_id, sample_id=sample_id, status="claimed"
            )
            async with engine.begin() as conn:
                await conn.execute(
                    jobs_tbl.update()
                    .where(jobs_tbl.c.id == job_id)
                    .values(run_name=stale_run_name)
                )

            count = await svc.requeue_expired()
            assert count == 1

            job = await _get_job(engine, job_id)
            assert job["status"] == "pending"
            assert job["run_name"] is None
        finally:
            await engine.dispose()

    _run(go())
