"""Dispatch router — client-facing endpoints for claiming and reporting jobs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.dispatch import DispatchService


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
    svc = DispatchService(engine=engine)

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
        result = await svc.claim_batch(req.limit, req.workflow_id, req.workflow_version)
        if result is None:
            return Response(status_code=204)

        return DispatchBatchResponse(
            run_name=result.run_name,
            workflow_id=result.workflow_id,
            workflow_version=result.workflow_version,
            workflow_pk=result.workflow_pk,
            repository_url=result.repository_url,
            revision=result.revision,
            jobs=[
                DispatchedJob(
                    sample_id=j.sample_id,
                    ncbi_accession=j.ncbi_accession,
                    metadata=j.metadata,
                )
                for j in result.jobs
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
        ok = await svc.report_submitted(req.run_name, req.executor_job_id)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"No claimed run with name '{req.run_name}' found",
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
        return {"requeued_runs": await svc.requeue_expired()}

    return router
