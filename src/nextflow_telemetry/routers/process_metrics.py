"""Process-level analytics derived from the raw telemetry event stream."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .. import models
from ..services.process_metrics import ProcessMetricsService

_WINDOW_DAYS_DESC = "Limit results to events in the last N days. Cannot be combined with window_hours, since, or until."
_WINDOW_HOURS_DESC = "Limit results to events in the last N hours. Cannot be combined with window_days."
_SINCE_DESC = "Inclusive lower bound (UTC ISO-8601). Can be combined with until."
_UNTIL_DESC = "Inclusive upper bound (UTC ISO-8601). Can be combined with since."
_WORKFLOW_ID_DESC = "Filter to a specific workflow (e.g. 'cmgd_nextflow')."
_WORKFLOW_VERSION_DESC = "Filter to a specific workflow version. Only meaningful with workflow_id."
_RUN_NAME_DESC = "Filter to a single Nextflow run_name."
_SAMPLE_ID_DESC = "Filter to a single sample_id."
_MIN_SAMPLES_DESC = "Minimum completed events a process must have to appear in ranked lists. Set to 1 to see all processes."
_LIMIT_DESC = "Maximum number of rows to return in ranked lists."


def create_process_metrics_router(service: ProcessMetricsService) -> APIRouter:
    router = APIRouter(prefix="/metrics/processes", tags=["process-metrics"])

    @router.get(
        "/running",
        response_model=models.RunningProcessesResponse,
        summary="Tasks currently in flight",
        description=(
            "Returns live counts of tasks that are actively running (process_started but not "
            "process_completed) or queued in SLURM (process_submitted but not started), "
            "grouped by process name. Only considers runs currently in 'running' state."
        ),
    )
    async def process_running():
        return await service.running()

    @router.get(
        "/summary",
        response_model=models.ProcessSummaryResponse,
        summary="Process execution summary",
        description=(
            "Returns high-level KPI cards (total runs, success/failure rates, retry rates) "
            "plus ranked tables of the most-failing and most-retried processes. "
            "Designed to populate an operations dashboard overview page."
        ),
    )
    async def process_summary(
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        run_name: str | None = Query(default=None, description=_RUN_NAME_DESC),
        sample_id: str | None = Query(default=None, description=_SAMPLE_ID_DESC),
        min_samples: int = Query(default=5, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=10, ge=1, le=200, description=_LIMIT_DESC),
    ):
        try:
            return await service.summary(
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                run_name=run_name, sample_id=sample_id,
                min_samples=min_samples, limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/retries",
        response_model=models.ProcessRetriesResponse,
        summary="Retry statistics by process and attempt",
        description=(
            "Breaks down retry behaviour per process: how often tasks are retried, "
            "how many retries succeed vs ultimately fail, and which attempt number has the "
            "highest success rate. Useful for tuning `max_retries` and resource requests."
        ),
    )
    async def process_retries(
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        run_name: str | None = Query(default=None, description=_RUN_NAME_DESC),
        sample_id: str | None = Query(default=None, description=_SAMPLE_ID_DESC),
        min_samples: int = Query(default=5, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=50, ge=1, le=500, description=_LIMIT_DESC),
    ):
        try:
            return await service.retries(
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                run_name=run_name, sample_id=sample_id,
                min_samples=min_samples, limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/resources-by-attempt",
        response_model=models.ProcessResourcesByAttemptResponse,
        summary="CPU, memory, and I/O usage by process and attempt",
        description=(
            "Returns average and 95th-percentile CPU utilisation, memory utilisation, peak RSS, "
            "and disk I/O for each process broken down by attempt number. "
            "Use this to identify processes that are under-resourced on first attempt and compare "
            "resource consumption between initial runs and retries."
        ),
    )
    async def process_resources_by_attempt(
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        run_name: str | None = Query(default=None, description=_RUN_NAME_DESC),
        sample_id: str | None = Query(default=None, description=_SAMPLE_ID_DESC),
        min_samples: int = Query(default=5, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=100, ge=1, le=1000, description=_LIMIT_DESC),
    ):
        try:
            return await service.resources_by_attempt(
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                run_name=run_name, sample_id=sample_id,
                min_samples=min_samples, limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/failures",
        response_model=models.ProcessFailuresResponse,
        summary="Failure rate by process",
        description=(
            "Lists processes ranked by failure count, including success and failure totals and "
            "the most common exit code among failed tasks. "
            "Useful for identifying persistent problem processes that need attention."
        ),
    )
    async def process_failures(
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        run_name: str | None = Query(default=None, description=_RUN_NAME_DESC),
        sample_id: str | None = Query(default=None, description=_SAMPLE_ID_DESC),
        min_samples: int = Query(default=5, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=50, ge=1, le=500, description=_LIMIT_DESC),
    ):
        try:
            return await service.failures(
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                run_name=run_name, sample_id=sample_id,
                min_samples=min_samples, limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/failure-signatures",
        response_model=models.ProcessFailureSignaturesResponse,
        summary="Failure signatures grouped by process and exit code",
        description=(
            "Returns a frequency table of (process, exit_code) pairs, revealing whether failures "
            "cluster around specific exit codes (e.g. OOM kills, missing files, network errors). "
            "Each unique combination is a 'failure signature' that can be mapped to a root cause."
        ),
    )
    async def process_failure_signatures(
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        run_name: str | None = Query(default=None, description=_RUN_NAME_DESC),
        sample_id: str | None = Query(default=None, description=_SAMPLE_ID_DESC),
        limit: int = Query(default=100, ge=1, le=1000, description=_LIMIT_DESC),
    ):
        try:
            return await service.failure_signatures(
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                run_name=run_name, sample_id=sample_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/timeline",
        response_model=models.ProcessTimelineResponse,
        summary="Failure and success counts over time",
        description=(
            "Groups process_completed events into time buckets (hour/day/week) and returns "
            "success/failure counts per bucket. Use this to see whether failures are a recent "
            "regression or long-running background noise, and to correlate failure spikes with "
            "deployments. Supports the same workflow, time-window, and process filters as other endpoints."
        ),
    )
    async def process_timeline(
        bucket: Literal["hour", "day", "week"] = Query(default="hour", description="Time bucket size."),
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DAYS_DESC),
        window_hours: int | None = Query(default=None, ge=1, description=_WINDOW_HOURS_DESC),
        since: dt.datetime | None = Query(default=None, description=_SINCE_DESC),
        until: dt.datetime | None = Query(default=None, description=_UNTIL_DESC),
        workflow_id: str | None = Query(default=None, description=_WORKFLOW_ID_DESC),
        workflow_version: str | None = Query(default=None, description=_WORKFLOW_VERSION_DESC),
        process: str | None = Query(default=None, description="Filter to a specific process name."),
    ):
        try:
            return await service.timeline(
                bucket=bucket,
                window_days=window_days, window_hours=window_hours,
                since=since, until=until,
                workflow_id=workflow_id, workflow_version=workflow_version,
                process=process,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
