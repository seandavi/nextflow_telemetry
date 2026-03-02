from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import models
from ..services.pipelines import PipelinesService


def create_pipelines_router(service: PipelinesService) -> APIRouter:
    router = APIRouter(prefix="/pipelines", tags=["pipelines"])

    @router.post("", response_model=models.PipelineResponse, status_code=201)
    async def create_pipeline(body: models.PipelineCreate):
        try:
            return await service.create(
                pipeline_id=body.pipeline_id,
                repository=body.repository,
                branch=body.branch,
                description=body.description,
                default_params=body.default_params,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("", response_model=models.PipelineListResponse)
    async def list_pipelines(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return await service.list(limit=limit, offset=offset)

    @router.get("/{pipeline_id}", response_model=models.PipelineResponse)
    async def get_pipeline(pipeline_id: str):
        result = await service.get(pipeline_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")
        return result

    @router.patch("/{pipeline_id}", response_model=models.PipelineResponse)
    async def update_pipeline(pipeline_id: str, body: models.PipelineUpdate):
        kwargs = body.dict(exclude_unset=True)
        result = await service.update(pipeline_id, **kwargs)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")
        return result

    @router.delete("/{pipeline_id}", status_code=204)
    async def delete_pipeline(pipeline_id: str):
        try:
            deleted = await service.delete(pipeline_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    return router
