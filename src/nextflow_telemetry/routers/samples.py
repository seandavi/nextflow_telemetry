"""Sample catalog router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.sample import SampleService


class SampleRegisterRequest(BaseModel):
    sample_id: str
    metadata: dict[str, Any] | None = None


class SampleResponse(BaseModel):
    id: int
    sample_id: str
    metadata: dict[str, Any] | None = None
    created_at: Any
    updated_at: Any

    model_config = {"from_attributes": True}


def create_samples_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/samples", tags=["samples"])
    svc = SampleService(engine=engine)

    @router.post("", response_model=SampleResponse, status_code=201)
    async def register_sample(req: SampleRegisterRequest):
        """Register a new sample (or update metadata for an existing one).

        Triggers job creation via reconcile — call POST /admin/reconcile-jobs
        afterwards to materialise the new (sample × active workflow) jobs.
        """
        row = await svc.register(req.sample_id, req.metadata)
        return SampleResponse(
            id=row["id"],
            sample_id=row["sample_id"],
            metadata=row["metadata_"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @router.get("", response_model=list[SampleResponse])
    async def list_samples(limit: int = 100, offset: int = 0):
        rows = await svc.list_samples(limit=limit, offset=offset)
        return [
            SampleResponse(
                id=r["id"],
                sample_id=r["sample_id"],
                metadata=r["metadata_"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    @router.get("/{sample_id}", response_model=SampleResponse)
    async def get_sample(sample_id: str):
        row = await svc.get(sample_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")
        return SampleResponse(
            id=row["id"],
            sample_id=row["sample_id"],
            metadata=row["metadata_"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    return router
