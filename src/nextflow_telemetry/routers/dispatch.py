"""Dispatch router — client-facing endpoints for claiming and reporting jobs."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, samples_tbl, workflow_runs_tbl, workflows_tbl

if sys.version_info >= (3, 13):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7

CLAIM_TTL_MINUTES = 5


class DispatchBatchRequest(BaseModel):
    """Request body for claiming a batch of pending jobs."""
    limit: int = Field(default=50, ge=1, le=500, description="Maximum number of jobs to claim in this batch. All claimed jobs will be for the same workflow and version.")
    workflow_id: list[str] | None = Field(default=None, description="Optional: restrict the claim to one or more workflows. If omitted, the oldest pending jobs across any workflow are returned.")
    workflow_version: str | None = Field(default=None, description="Optional: restrict the claim to a specific version of `workflow_id`.")


class DispatchedJob(BaseModel):
    """A single job included in a dispatch batch."""
    sample_id: str = Field(description="The sample identifier to be processed in this run.")
    ncbi_accession: str | None = Field(default=None, description="Canonical semicolon-separated SRR accessions for this sample.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional sample metadata.")


class DispatchBatchResponse(BaseModel):
    """Response from a successful POST /dispatch/batch.

    Contains everything the client needs to build the `nextflow run` command.
    All jobs in the batch belong to the same workflow version so a single
    command can process the full list of sample IDs.
    """
    run_name: str = Field(description="Server-assigned run name. Must be passed as `-name` to `nextflow run` so that weblog events can be correlated back to this batch.")
    workflow_id: str = Field(description="Logical workflow identifier.")
    workflow_version: str = Field(description="Workflow version being run.")
    workflow_pk: int = Field(description="Database primary key of the workflow definition, for reference.")
    repository_url: str = Field(description="Git URL or local path to pass to `nextflow run`.")
    revision: str = Field(description="Git revision (branch/tag/commit) to check out.")
    jobs: list[DispatchedJob] = Field(description="List of jobs in this batch. Pass the sample IDs as `--sample_ids` (comma-separated) to the pipeline.")


class SubmittedRequest(BaseModel):
    """Request body for confirming that a run has been submitted to the executor."""
    run_name: str = Field(description="The `run_name` returned by POST /dispatch/batch. Must match exactly.")
    executor_job_id: str | None = Field(default=None, description="Optional: the job ID assigned by the executor (SLURM job ID, local PID, etc.) for cross-referencing.")
    sample_ids: list[str] = Field(default=[], description="Informational: the sample IDs included in this run. All jobs associated with `run_name` are transitioned regardless of this list.")


def create_dispatch_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/dispatch", tags=["dispatch"])

    @router.post(
        "/batch",
        response_model=DispatchBatchResponse,
        responses={204: {"description": "No pending jobs are available for the requested workflow filter."}},
        summary="Claim a batch of pending jobs",
        description=(
            "Atomically selects and locks a set of `pending` jobs, transitions them to `claimed`, "
            "and creates a `workflow_run` record. Returns all the information needed to build a "
            "`nextflow run` command: repository URL, revision, profile, run name, and sample IDs. "
            "Returns **HTTP 204** (no body) if there are no pending jobs matching the filter. "
            "Claims that are not confirmed with `POST /dispatch/submitted` within 5 minutes are "
            "automatically recycled by `POST /dispatch/requeue-expired`."
        ),
    )
    async def dispatch_batch(req: DispatchBatchRequest):
        now = datetime.now(timezone.utc)

        async with engine.begin() as conn:
            q = (
                select(jobs_tbl, workflows_tbl)
                .join(workflows_tbl, jobs_tbl.c.workflow_pk == workflows_tbl.c.id)
                .where(
                    jobs_tbl.c.status == "pending",
                    workflows_tbl.c.status == "active",
                )
            )
            if req.workflow_id:
                q = q.where(jobs_tbl.c.workflow_id.in_(req.workflow_id))
            if req.workflow_version:
                q = q.where(jobs_tbl.c.workflow_version == req.workflow_version)

            result = await conn.execute(
                q.order_by(jobs_tbl.c.workflow_id, jobs_tbl.c.workflow_version,
                           jobs_tbl.c.created_at)
                .limit(req.limit)
                .with_for_update(of=jobs_tbl, skip_locked=True)
            )
            rows = result.mappings().all()

            if not rows:
                return Response(status_code=204)

            first_wf_id = rows[0]["workflow_id"]
            first_wf_ver = rows[0]["workflow_version"]
            rows = [r for r in rows if r["workflow_id"] == first_wf_id
                    and r["workflow_version"] == first_wf_ver]

            job_ids = [r["id"] for r in rows]
            run_name = "r" + str(_uuid7())
            workflow_pk = rows[0]["workflow_pk"]
            repository_url = rows[0]["repository_url"]
            revision = rows[0]["revision"]

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

            await conn.execute(
                update(jobs_tbl)
                .where(jobs_tbl.c.id.in_(job_ids))
                .values(run_name=run_name, status="claimed")
            )

            # Fetch sample fields in a separate query to avoid JOIN conflicts
            # with the FOR UPDATE SKIP LOCKED above.
            sample_ids = [r["sample_id"] for r in rows]
            meta_result = await conn.execute(
                select(
                    samples_tbl.c.sample_id,
                    samples_tbl.c.ncbi_accession,
                    samples_tbl.c.metadata_,
                )
                .where(samples_tbl.c.sample_id.in_(sample_ids))
            )
            sample_map: dict[str, Any] = {
                r["sample_id"]: r
                for r in meta_result.mappings().all()
            }

        return DispatchBatchResponse(
            run_name=run_name,
            workflow_id=first_wf_id,
            workflow_version=first_wf_ver,
            workflow_pk=workflow_pk,
            repository_url=repository_url,
            revision=revision,
            jobs=[
                DispatchedJob(
                    sample_id=r["sample_id"],
                    ncbi_accession=sample_map.get(r["sample_id"], {}).get("ncbi_accession"),
                    metadata=sample_map.get(r["sample_id"], {}).get("metadata_") or {},
                )
                for r in rows
            ],
        )

    @router.post(
        "/submitted",
        summary="Confirm a run has been submitted to the executor",
        description=(
            "Called immediately after the client successfully submits the Nextflow run to its executor "
            "(local subprocess, SLURM, etc.). Transitions the `workflow_run` from `claimed` to `submitted` "
            "and records the optional `executor_job_id` for cross-referencing. "
            "Returns 404 if the run name is not found or is not in `claimed` state."
        ),
    )
    async def report_submitted(req: SubmittedRequest):
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

    @router.post(
        "/requeue-expired",
        summary="Requeue stale claimed runs",
        description=(
            "Finds `workflow_run` records that have been in `claimed` state for longer than 5 minutes "
            "without a `POST /dispatch/submitted` confirmation, marks them `expired`, and resets their "
            "associated jobs back to `pending` so they re-enter the dispatch pool. "
            "Intended to be called periodically by a scheduler (cron, daemon loop) to recover from "
            "client crashes or network failures between claim and submission."
        ),
    )
    async def requeue_expired():
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
