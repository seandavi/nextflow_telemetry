"""Dispatch router — client-facing endpoints for claiming and reporting jobs."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, workflow_runs_tbl, workflows_tbl

if sys.version_info >= (3, 13):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7

CLAIM_TTL_MINUTES = 5


class DispatchBatchRequest(BaseModel):
    limit: int = 50
    # Optional: filter to a specific workflow. If omitted, any pending job qualifies.
    workflow_id: str | None = None
    workflow_version: str | None = None


class DispatchedJob(BaseModel):
    sample_id: str


class DispatchBatchResponse(BaseModel):
    run_name: str
    workflow_id: str
    workflow_version: str
    workflow_pk: int
    repository_url: str
    revision: str
    profile: str
    jobs: list[DispatchedJob]


class SubmittedRequest(BaseModel):
    run_name: str
    executor_job_id: str | None = None
    sample_ids: list[str] = []  # informational; all jobs for this run_name are transitioned


def create_dispatch_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/dispatch", tags=["dispatch"])

    @router.post("/batch", response_model=DispatchBatchResponse)
    async def dispatch_batch(req: DispatchBatchRequest):
        """Claim a batch of pending jobs.

        Returns the run_name the client MUST pass as ``-name`` to nextflow run,
        plus workflow execution details (repository, revision, profile).
        All jobs in a batch belong to the same workflow/version.
        """
        now = datetime.now(timezone.utc)

        async with engine.begin() as conn:
            # Build job query with workflow join to get execution details.
            # All claimed jobs in a batch must be for the same workflow_id + version
            # so the client can build a single nextflow run command.
            q = (
                select(jobs_tbl, workflows_tbl)
                .join(workflows_tbl, jobs_tbl.c.workflow_pk == workflows_tbl.c.id)
                .where(
                    jobs_tbl.c.status == "pending",
                    workflows_tbl.c.status == "active",
                )
            )
            if req.workflow_id:
                q = q.where(jobs_tbl.c.workflow_id == req.workflow_id)
            if req.workflow_version:
                q = q.where(jobs_tbl.c.workflow_version == req.workflow_version)

            # Group by workflow to ensure all jobs in a batch share the same target
            result = await conn.execute(
                q.order_by(jobs_tbl.c.workflow_id, jobs_tbl.c.workflow_version,
                           jobs_tbl.c.created_at)
                .limit(req.limit)
                .with_for_update(of=jobs_tbl, skip_locked=True)
            )
            rows = result.mappings().all()

            if not rows:
                return Response(status_code=204)

            # Ensure all claimed jobs are for the same workflow (limit query already
            # orders by workflow, so rows[0] defines the workflow for this batch)
            first_wf_id = rows[0]["workflow_id"]
            first_wf_ver = rows[0]["workflow_version"]
            rows = [r for r in rows if r["workflow_id"] == first_wf_id
                    and r["workflow_version"] == first_wf_ver]

            job_ids = [r["id"] for r in rows]
            # Prefix with "r" so the name satisfies Nextflow's ^[a-z]... constraint
            run_name = "r" + str(_uuid7())
            workflow_pk = rows[0]["workflow_pk"]
            repository_url = rows[0]["repository_url"]
            revision = rows[0]["revision"]
            profile = rows[0]["profile"]

            # Create the workflow_run record
            await conn.execute(
                workflow_runs_tbl.insert().values(
                    run_name=run_name,
                    workflow_id=first_wf_id,
                    workflow_version=first_wf_ver,
                    workflow_pk=workflow_pk,
                    revision=revision,
                    status="claimed",
                    claimed_at=now,
                )
            )

            # Associate jobs with this run and mark claimed
            await conn.execute(
                update(jobs_tbl)
                .where(jobs_tbl.c.id.in_(job_ids))
                .values(run_name=run_name, status="claimed")
            )

        return DispatchBatchResponse(
            run_name=run_name,
            workflow_id=first_wf_id,
            workflow_version=first_wf_ver,
            workflow_pk=workflow_pk,
            repository_url=repository_url,
            revision=revision,
            profile=profile,
            jobs=[DispatchedJob(sample_id=r["sample_id"]) for r in rows],
        )

    @router.post("/submitted")
    async def report_submitted(req: SubmittedRequest):
        """Client reports that it successfully submitted the run to the executor."""
        now = datetime.now(timezone.utc)
        async with engine.begin() as conn:
            result = await conn.execute(
                update(workflow_runs_tbl)
                .where(
                    workflow_runs_tbl.c.run_name == req.run_name,
                    workflow_runs_tbl.c.status == "claimed",
                )
                .values(
                    status="submitted",
                    submitted_at=now,
                    executor_job_id=req.executor_job_id,
                )
                .returning(workflow_runs_tbl.c.run_name)
            )
            if not result.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail=f"No claimed run with name '{req.run_name}' found",
                )

            await conn.execute(
                update(jobs_tbl)
                .where(
                    jobs_tbl.c.run_name == req.run_name,
                    jobs_tbl.c.status == "claimed",
                )
                .values(status="running")
            )

        return {"run_name": req.run_name, "status": "submitted"}

    @router.post("/requeue-expired")
    async def requeue_expired():
        """Requeue claimed-but-never-submitted jobs whose TTL has expired.

        Intended to be called by a scheduler (cron / background task).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=CLAIM_TTL_MINUTES)
        async with engine.begin() as conn:
            result = await conn.execute(
                update(workflow_runs_tbl)
                .where(
                    workflow_runs_tbl.c.status == "claimed",
                    workflow_runs_tbl.c.claimed_at < cutoff,
                )
                .values(status="expired")
                .returning(workflow_runs_tbl.c.run_name)
            )
            expired_run_names = [r[0] for r in result.fetchall()]

            if expired_run_names:
                await conn.execute(
                    update(jobs_tbl)
                    .where(
                        jobs_tbl.c.run_name.in_(expired_run_names),
                        jobs_tbl.c.status == "claimed",
                    )
                    .values(status="pending", run_name=None)
                )

        return {"requeued_runs": len(expired_run_names)}

    return router
