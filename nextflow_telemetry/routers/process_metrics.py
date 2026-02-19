from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..services.process_metrics import ProcessMetricsService


def create_process_metrics_router(service: ProcessMetricsService) -> APIRouter:
    router = APIRouter(prefix="/metrics/processes", tags=["process-metrics"])

    @router.get("/summary")
    async def process_summary(
        window_days: int | None = Query(default=None, ge=1),
        min_samples: int = Query(default=50, ge=1),
        limit: int = Query(default=10, ge=1, le=200),
    ):
        try:
            return service.summary(window_days=window_days, min_samples=min_samples, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/retries")
    async def process_retries(
        window_days: int | None = Query(default=None, ge=1),
        min_samples: int = Query(default=50, ge=1),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        try:
            return service.retries(window_days=window_days, min_samples=min_samples, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/resources-by-attempt")
    async def process_resources_by_attempt(
        window_days: int | None = Query(default=None, ge=1),
        min_samples: int = Query(default=50, ge=1),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        try:
            return service.resources_by_attempt(window_days=window_days, min_samples=min_samples, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/failures")
    async def process_failures(
        window_days: int | None = Query(default=None, ge=1),
        min_samples: int = Query(default=50, ge=1),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        try:
            return service.failures(window_days=window_days, min_samples=min_samples, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/failure-signatures")
    async def process_failure_signatures(
        window_days: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        try:
            return service.failure_signatures(window_days=window_days, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
