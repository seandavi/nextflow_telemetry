from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .. import models
from ..services.jobs import JobsService


def create_jobs_router(service: JobsService) -> APIRouter:
    router = APIRouter(prefix="/jobs", tags=["jobs"])

    @router.post("", response_model=models.JobResponse, status_code=201)
    async def create_job(body: models.JobCreate):
        try:
            return await service.create(
                sample_id=body.sample_id,
                pipeline_id=body.pipeline_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("", response_model=models.JobListResponse)
    async def list_jobs(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        status: Optional[str] = Query(default=None),
        sample_id: Optional[str] = Query(default=None),
        pipeline_id: Optional[str] = Query(default=None),
    ):
        return await service.list(
            limit=limit,
            offset=offset,
            status=status,
            sample_id=sample_id,
            pipeline_id=pipeline_id,
        )

    @router.get("/{job_id}", response_model=models.JobResponse)
    async def get_job(job_id: int):
        result = await service.get(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return result

    @router.patch("/{job_id}", response_model=models.JobResponse)
    async def update_job_status(job_id: int, body: models.JobStatusUpdate):
        try:
            result = await service.update_status(job_id, body.status.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return result

    return router
