"""Process-level analytics derived from the raw telemetry event stream."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import models
from ..services.process_metrics import ProcessMetricsService

_WINDOW_DESC = "Limit results to events in the last N days. Omit for all-time."
_MIN_SAMPLES_DESC = "Minimum number of completed events a process must have to appear in ranked lists."
_LIMIT_DESC = "Maximum number of rows to return in ranked lists."


def create_process_metrics_router(service: ProcessMetricsService) -> APIRouter:
    router = APIRouter(prefix="/metrics/processes", tags=["process-metrics"])

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
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DESC),
        min_samples: int = Query(default=50, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=10, ge=1, le=200, description=_LIMIT_DESC),
    ):
        try:
            return await service.summary(window_days=window_days, min_samples=min_samples, limit=limit)
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
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DESC),
        min_samples: int = Query(default=50, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=50, ge=1, le=500, description=_LIMIT_DESC),
    ):
        try:
            return await service.retries(window_days=window_days, min_samples=min_samples, limit=limit)
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
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DESC),
        min_samples: int = Query(default=50, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=100, ge=1, le=1000, description=_LIMIT_DESC),
    ):
        try:
            return await service.resources_by_attempt(window_days=window_days, min_samples=min_samples, limit=limit)
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
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DESC),
        min_samples: int = Query(default=50, ge=1, description=_MIN_SAMPLES_DESC),
        limit: int = Query(default=50, ge=1, le=500, description=_LIMIT_DESC),
    ):
        try:
            return await service.failures(window_days=window_days, min_samples=min_samples, limit=limit)
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
        window_days: int | None = Query(default=None, ge=1, description=_WINDOW_DESC),
        limit: int = Query(default=100, ge=1, le=1000, description=_LIMIT_DESC),
    ):
        try:
            return await service.failure_signatures(window_days=window_days, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
