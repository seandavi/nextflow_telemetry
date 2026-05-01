"""Workflow registry router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import dead_letter_tbl, jobs_tbl, workflows_tbl
from ..models import WorkflowJobSummary
from ..services.workflow import WorkflowService


class WorkflowRegisterRequest(BaseModel):
    """Request body for registering or updating a workflow definition."""
    workflow_id: str = Field(description="Logical workflow name, e.g. 'curatedMetagenomics'. Combined with `version` to form the unique key.")
    version: str = Field(description="Semantic or arbitrary version string, e.g. '1.0.0'. Bumping this forces all samples to be reprocessed under the new version.")
    repository_url: str = Field(description="Git repository URL or absolute local path to the `main.nf` file. Passed directly to `nextflow run`.")
    revision: str = Field(description="Git branch, tag, or commit hash to check out. Mutable: updating it does not create a new version or force reruns.")
    profile: str = Field(default="standard", description="Nextflow profile to activate, e.g. 'test', 'slurm', 'docker'. Passed as `-profile` to `nextflow run`.")
    manifest_version: str | None = Field(default=None, description="Optional version string from the pipeline's `nextflow.config` manifest block, for display purposes.")
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum number of times a failed job will be re-enqueued before being sent to the dead-letter queue.")
    description: str | None = Field(default=None, description="Free-text description of what this workflow does, shown in listings.")


class WorkflowStatusRequest(BaseModel):
    """Request body for updating a workflow's lifecycle status."""
    status: str = Field(description="Target status: `active` (new jobs are dispatched), `paused` (no new dispatches but existing jobs continue), or `retired` (permanently disabled).")


class WorkflowRevisionRequest(BaseModel):
    """Request body for updating the git revision of a workflow."""
    revision: str = Field(description="New git branch, tag, or commit hash. Does not create a new version or re-enqueue existing jobs.")


class WorkflowResponse(BaseModel):
    """A workflow definition from the registry."""
    id: int = Field(description="Auto-incrementing database primary key used in PATCH endpoints.")
    workflow_id: str = Field(description="Logical workflow name.")
    version: str = Field(description="Version string. Together with `workflow_id` this is the unique identifier for a workflow.")
    repository_url: str = Field(description="Git URL or local path passed to `nextflow run`.")
    revision: str = Field(description="Current git revision (branch/tag/commit). Mutable without forcing reruns.")
    profile: str = Field(description="Nextflow profile activated for every run of this workflow.")
    manifest_version: str | None = Field(description="Pipeline manifest version, if recorded.")
    max_retries: int = Field(description="How many times a failed job will be retried before dead-lettering.")
    status: str = Field(description="Lifecycle status: `active`, `paused`, or `retired`.")
    description: str | None = Field(description="Optional human-readable description.")
    created_at: Any = Field(description="UTC timestamp of first registration.")
    updated_at: Any = Field(description="UTC timestamp of most recent change.")


def _to_response(row: dict) -> WorkflowResponse:
    return WorkflowResponse(**row)


def create_workflows_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/workflows", tags=["workflows"])
    svc = WorkflowService(engine=engine)

    @router.post(
        "",
        response_model=WorkflowResponse,
        status_code=201,
        summary="Register or update a workflow",
        description=(
            "Adds a workflow to the registry keyed on `(workflow_id, version)`, or updates its "
            "mutable fields (`repository_url`, `revision`, `profile`, `max_retries`, `description`) "
            "if it already exists. "
            "Only `active` workflows are eligible for job dispatch. "
            "To force all samples to be reprocessed, register a new entry with a bumped `version`."
        ),
    )
    async def register_workflow(req: WorkflowRegisterRequest):
        row = await svc.register(
            workflow_id=req.workflow_id,
            version=req.version,
            repository_url=req.repository_url,
            revision=req.revision,
            profile=req.profile,
            manifest_version=req.manifest_version,
            max_retries=req.max_retries,
            description=req.description,
        )
        return _to_response(row)

    @router.get(
        "",
        response_model=list[WorkflowResponse],
        summary="List workflows",
        description=(
            "Returns all registered workflows. "
            "Filter by lifecycle status with the `?status=active|paused|retired` query parameter. "
            "Use the returned `id` field when calling the PATCH endpoints."
        ),
    )
    async def list_workflows(
        status: str | None = Query(default=None, description="Optional filter: `active`, `paused`, or `retired`."),
    ):
        rows = await svc.list_workflows(status=status)
        return [_to_response(r) for r in rows]

    @router.get(
        "/{workflow_pk}",
        response_model=WorkflowResponse,
        summary="Get a workflow by primary key",
        description=(
            "Retrieves a single workflow by its integer database primary key (`id`). "
            "Returns 404 if no workflow with that key exists."
        ),
    )
    async def get_workflow(workflow_pk: int):
        row = await svc.get_by_pk(workflow_pk)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    @router.patch(
        "/{workflow_pk}/status",
        response_model=WorkflowResponse,
        summary="Update workflow lifecycle status",
        description=(
            "Transitions the workflow between lifecycle states: `active` → `paused` → `retired`. "
            "Pausing stops new jobs from being dispatched but does not cancel in-flight runs. "
            "Retiring is permanent — paused and retired workflows are excluded from reconciliation and dispatch."
        ),
    )
    async def update_status(workflow_pk: int, req: WorkflowStatusRequest):
        try:
            row = await svc.update_status(workflow_pk, req.status)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    @router.patch(
        "/{workflow_pk}/revision",
        response_model=WorkflowResponse,
        summary="Update the git revision",
        description=(
            "Updates the git branch, tag, or commit hash that will be used for future runs of this "
            "workflow, without changing the version or re-enqueuing existing jobs. "
            "Useful for pointing a workflow at a new patch release or hotfix branch."
        ),
    )
    async def update_revision(workflow_pk: int, req: WorkflowRevisionRequest):
        row = await svc.update_revision(workflow_pk, req.revision)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    @router.get(
        "/{workflow_pk}/job-summary",
        response_model=WorkflowJobSummary,
        summary="Job status summary for a workflow",
        description=(
            "Returns the count of jobs in each status for the specified workflow, "
            "plus the number of dead-letter entries and overall completion percentage. "
            "Returns 404 if no workflow with the given primary key exists."
        ),
    )
    async def job_summary(workflow_pk: int):
        async with engine.connect() as conn:
            # Resolve workflow metadata; 404 if not found
            wf_row = await conn.execute(
                select(
                    workflows_tbl.c.id,
                    workflows_tbl.c.workflow_id,
                    workflows_tbl.c.version,
                ).where(workflows_tbl.c.id == workflow_pk)
            )
            wf = wf_row.mappings().one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")

            # Count jobs grouped by status
            status_counts_q = (
                select(jobs_tbl.c.status, func.count().label("cnt"))
                .where(jobs_tbl.c.workflow_pk == workflow_pk)
                .group_by(jobs_tbl.c.status)
            )
            status_result = await conn.execute(status_counts_q)
            counts: dict[str, int] = {r.status: r.cnt for r in status_result}

            # Count dead-letter entries linked to jobs for this workflow
            dlq_q = (
                select(func.count().label("cnt"))
                .select_from(
                    dead_letter_tbl.join(jobs_tbl, dead_letter_tbl.c.job_id == jobs_tbl.c.id)
                )
                .where(jobs_tbl.c.workflow_pk == workflow_pk)
            )
            dlq_result = await conn.execute(dlq_q)
            dlq_count: int = dlq_result.scalar_one()

        pending   = counts.get("pending",   0)
        claimed   = counts.get("claimed",   0)
        running   = counts.get("running",   0)
        completed = counts.get("completed", 0)
        failed    = counts.get("failed",    0)
        total     = pending + claimed + running + completed + failed
        pct       = 100.0 * completed / total if total > 0 else 0.0

        return WorkflowJobSummary(
            workflow_pk=workflow_pk,
            workflow_id=wf["workflow_id"],
            version=wf["version"],
            total=total,
            pending=pending,
            claimed=claimed,
            running=running,
            completed=completed,
            failed=failed,
            dead_letter=dlq_count,
            completion_pct=round(pct, 2),
        )

    return router
