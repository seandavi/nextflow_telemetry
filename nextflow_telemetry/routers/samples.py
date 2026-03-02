from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import models
from ..services.samples import SamplesService


def create_samples_router(service: SamplesService) -> APIRouter:
    router = APIRouter(prefix="/samples", tags=["samples"])

    @router.post("", response_model=models.SampleResponse, status_code=201)
    async def create_sample(body: models.SampleCreate):
        try:
            return await service.create(
                sample_id=body.sample_id,
                srr_accessions=body.srr_accessions,
                metadata_=body.metadata_,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("", response_model=models.SampleListResponse)
    async def list_samples(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return await service.list(limit=limit, offset=offset)

    @router.get("/{sample_id}", response_model=models.SampleResponse)
    async def get_sample(sample_id: str):
        result = await service.get(sample_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")
        return result

    @router.patch("/{sample_id}", response_model=models.SampleResponse)
    async def update_sample(sample_id: str, body: models.SampleUpdate):
        kwargs = body.dict(exclude_unset=True)
        result = await service.update(sample_id, **kwargs)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")
        return result

    @router.delete("/{sample_id}", status_code=204)
    async def delete_sample(sample_id: str):
        try:
            deleted = await service.delete(sample_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")

    return router
