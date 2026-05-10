"""Cohort summary router (issue #36).

Lightweight wrapper over CohortService that exposes:
  - GET /api/cohorts                     — list cohorts/collections
  - GET /api/cohorts/{id}/summary        — counts + completion % + per-process failures
  - GET /api/cohorts/{id}/failures       — drill-down: failed tasks for a process

A "cohort" is a collection (PRJNA bioproject, SRA Study, or manual group)
already represented by the collections + collection_samples tables.
"""
from __future__ import annotations

import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.cohort import CohortService


class CohortListItem(BaseModel):
    collection_id: str
    source: str = Field(description="bioproject | sra_study | manual")
    label: Optional[str] = None
    sample_count: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class CohortJobStatusCounts(BaseModel):
    pending: int = 0
    claimed: int = 0
    submitted: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0


class CohortFailureByProcessRow(BaseModel):
    process: str
    failed_count: int = Field(description="Total FAILED/ABORTED process_completed events.")
    sample_count: int = Field(description="Distinct samples with at least one failure at this process.")


class CohortSummaryResponse(BaseModel):
    collection_id: str
    source: str
    label: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_version: Optional[str] = None
    sample_count: int = Field(description="Total samples in the cohort.")
    total_jobs: int = Field(description="Total jobs across all statuses, after the workflow filter is applied.")
    job_status_counts: CohortJobStatusCounts
    completion_pct: float = Field(description="completed / total_jobs × 100. Zero when total_jobs is 0.")
    failure_by_process: list[CohortFailureByProcessRow]
    generated_at_utc: datetime.datetime


class CohortFailureRow(BaseModel):
    telemetry_id: int
    sample_id: Optional[str] = None
    run_name: str
    utc_time: datetime.datetime
    task_name: Optional[str] = None
    task_hash: Optional[str] = Field(default=None, description="Nextflow work dir hash; use with /task-logs to fetch logs.")
    status: str
    exit_code: Optional[str] = None
    attempt: int


class CohortFailuresResponse(BaseModel):
    collection_id: str
    process: str
    rows: list[CohortFailureRow]


def create_cohorts_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/cohorts", tags=["cohorts"])
    svc = CohortService(engine=engine)

    @router.get(
        "",
        response_model=list[CohortListItem],
        summary="List cohorts",
        description="Returns every collection (cohort) with its current sample count, newest first.",
    )
    async def list_cohorts() -> list[CohortListItem]:
        rows = await svc.list_cohorts()
        return [CohortListItem(**r) for r in rows]

    @router.get(
        "/{collection_id}/summary",
        response_model=CohortSummaryResponse,
        summary="Cohort summary: completion %, status counts, and failure-by-process",
        description=(
            "Aggregates job status across all samples in the cohort and identifies "
            "which Nextflow processes are producing the most failures. Filter by "
            "`workflow_id` and `workflow_version` to scope to a specific pipeline; "
            "omit them for an all-workflows view."
        ),
    )
    async def get_summary(
        collection_id: Annotated[str, Path(description="Collection identifier (e.g. PRJNA123456).")],
        workflow_id: Annotated[Optional[str], Query(description="Restrict to a single workflow.")] = None,
        workflow_version: Annotated[Optional[str], Query(description="Restrict to a specific workflow version.")] = None,
    ) -> CohortSummaryResponse:
        summary = await svc.summary(collection_id, workflow_id, workflow_version)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"Cohort '{collection_id}' not found.")
        return CohortSummaryResponse.model_validate(summary)

    @router.get(
        "/{collection_id}/failures",
        response_model=CohortFailuresResponse,
        summary="Failed tasks for a (cohort, process) — click-through drill-down",
        description=(
            "Returns up to `limit` failed `process_completed` events for the given "
            "process within the cohort, newest first. `task_hash` joins to "
            "`/task-logs/{run_name}/{task_hash}` for the log viewer."
        ),
    )
    async def get_failures(
        collection_id: Annotated[str, Path(description="Collection identifier.")],
        process: Annotated[str, Query(description="Fully-qualified Nextflow process name (e.g. `FETCH_READS`).")],
        workflow_id: Annotated[Optional[str], Query()] = None,
        workflow_version: Annotated[Optional[str], Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000, description="Max rows.")] = 200,
    ) -> CohortFailuresResponse:
        # Match /summary's behaviour: 404 on unknown cohort, not 200 + empty rows.
        if not await svc.cohort_exists(collection_id):
            raise HTTPException(status_code=404, detail=f"Cohort '{collection_id}' not found.")
        rows = await svc.failures_for_process(
            collection_id, process, workflow_id, workflow_version, limit=limit
        )
        return CohortFailuresResponse(
            collection_id=collection_id,
            process=process,
            rows=[CohortFailureRow(**r) for r in rows],
        )

    return router
