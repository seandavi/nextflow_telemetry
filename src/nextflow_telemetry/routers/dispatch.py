"""Dispatch router — client-facing endpoints for claiming and reporting jobs."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import workflow_executions_tbl, workflow_runs_tbl

if sys.version_info >= (3, 13):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7

CLAIM_TTL_MINUTES = 5


class DispatchBatchRequest(BaseModel):
    workflow_id: str
    workflow_version: str
    limit: int = 50


class DispatchedJob(BaseModel):
    sample_id: str
    workflow_id: str
    workflow_version: str


class DispatchBatchResponse(BaseModel):
    run_name: str
    jobs: list[DispatchedJob]


class SubmittedRequest(BaseModel):
    run_name: str
    executor_job_id: str | None = None
    sample_ids: list[str]


def create_dispatch_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/dispatch", tags=["dispatch"])

    @router.post("/batch", response_model=DispatchBatchResponse)
    async def dispatch_batch(req: DispatchBatchRequest):
        """Client calls this to claim a batch of pending executions.

        Returns the run_name the client MUST pass as ``-name`` to nextflow run.
        """
        now = datetime.now(timezone.utc)

        async with engine.begin() as conn:
            # Claim pending executions for this workflow/version
            result = await conn.execute(
                select(workflow_executions_tbl)
                .where(
                    workflow_executions_tbl.c.workflow_id == req.workflow_id,
                    workflow_executions_tbl.c.workflow_version == req.workflow_version,
                    workflow_executions_tbl.c.status == "pending",
                )
                .limit(req.limit)
                .with_for_update(skip_locked=True)
            )
            rows = result.mappings().all()

            if not rows:
                return Response(status_code=204)

            execution_ids = [r["id"] for r in rows]
            sample_ids = [r["sample_id"] for r in rows]
            run_name = str(_uuid7())

            # Create the workflow_run record
            await conn.execute(
                workflow_runs_tbl.insert().values(
                    run_name=run_name,
                    workflow_id=req.workflow_id,
                    workflow_version=req.workflow_version,
                    status="claimed",
                    claimed_at=now,
                )
            )

            # Associate executions with this run and mark claimed
            await conn.execute(
                update(workflow_executions_tbl)
                .where(workflow_executions_tbl.c.id.in_(execution_ids))
                .values(run_name=run_name, status="claimed")
            )

        return DispatchBatchResponse(
            run_name=run_name,
            jobs=[
                DispatchedJob(
                    sample_id=r["sample_id"],
                    workflow_id=r["workflow_id"],
                    workflow_version=r["workflow_version"],
                )
                for r in rows
            ],
        )

    @router.post("/submitted")
    async def report_submitted(req: SubmittedRequest):
        """Client reports that it has successfully submitted the run to the executor."""
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
                update(workflow_executions_tbl)
                .where(
                    workflow_executions_tbl.c.run_name == req.run_name,
                    workflow_executions_tbl.c.status == "claimed",
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
                    update(workflow_executions_tbl)
                    .where(
                        workflow_executions_tbl.c.run_name.in_(expired_run_names),
                        workflow_executions_tbl.c.status == "claimed",
                    )
                    .values(status="pending", run_name=None)
                )

        return {"requeued_runs": len(expired_run_names)}

    return router
