"""Sample catalog router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.sample import SampleService


class SampleRegisterRequest(BaseModel):
    """Request body for registering or updating a sample."""
    sample_id: str = Field(description="Unique identifier for the sample, e.g. an SRA accession like 'SRR1234567'.")
    metadata: dict[str, Any] | None = Field(default=None, description="Optional arbitrary JSON metadata (source, phenotype, cohort, etc.). Merged on upsert.")


class SampleResponse(BaseModel):
    """A sample record from the catalog."""
    id: int = Field(description="Auto-incrementing database primary key.")
    sample_id: str = Field(description="Unique sample identifier as supplied by the caller.")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary JSON metadata attached to this sample.")
    created_at: Any = Field(description="UTC timestamp when this sample was first registered.")
    updated_at: Any = Field(description="UTC timestamp of the most recent metadata update.")

    model_config = {"from_attributes": True}


def create_samples_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/samples", tags=["samples"])
    svc = SampleService(engine=engine)

    @router.post(
        "",
        response_model=SampleResponse,
        status_code=201,
        summary="Register or update a sample",
        description=(
            "Adds a sample to the catalog, or updates its metadata if it already exists (upsert on `sample_id`). "
            "The sample becomes eligible for processing once at least one active workflow exists. "
            "Call `POST /admin/reconcile-jobs` after registering new samples to materialise pending jobs."
        ),
    )
    async def register_sample(req: SampleRegisterRequest):
        row = await svc.register(req.sample_id, req.metadata)
        return SampleResponse(
            id=row["id"],
            sample_id=row["sample_id"],
            metadata=row["metadata_"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @router.get(
        "",
        response_model=list[SampleResponse],
        summary="List samples",
        description=(
            "Returns a paginated list of all samples in the catalog, ordered by insertion time. "
            "Use `limit` and `offset` for pagination."
        ),
    )
    async def list_samples(
        limit: int = Query(default=100, ge=1, le=1000, description="Maximum number of samples to return."),
        offset: int = Query(default=0, ge=0, description="Number of samples to skip (for pagination)."),
    ):
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

    @router.get(
        "/{sample_id}",
        response_model=SampleResponse,
        summary="Get a sample by ID",
        description=(
            "Retrieves a single sample record by its `sample_id` string. "
            "Returns 404 if the sample has not been registered."
        ),
    )
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
