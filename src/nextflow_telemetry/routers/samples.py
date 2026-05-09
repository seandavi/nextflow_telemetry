"""Sample catalog router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.sample import SampleService
from ..utils import parse_srrs


class SampleRegisterRequest(BaseModel):
    """Request body for registering or updating a sample."""
    sample_id: str = Field(description="Unique identifier. For SRA samples derive via srrs_to_sample_id().")
    ncbi_accession: str = Field(description="Semicolon-separated SRR accessions (e.g. 'SRR001;SRR002'). Must contain at least one non-empty accession. Normalised to sorted, deduplicated form on write.")
    biosample_id: str | None = Field(default=None, description="NCBI BioSample accession (e.g. 'SAMN12345678'). Annotation only — not used as identity.")
    metadata: dict[str, Any] | None = Field(default=None, description="Optional arbitrary JSON metadata. Replaced on upsert.")

    @field_validator("ncbi_accession")
    @classmethod
    def _ncbi_accession_non_empty(cls, v: str) -> str:
        if not parse_srrs(v):
            raise ValueError("must contain at least one non-empty SRR accession")
        return v


class SampleResponse(BaseModel):
    """A sample record from the catalog."""
    id: int = Field(description="Auto-incrementing database primary key.")
    sample_id: str = Field(description="Unique sample identifier (md5 of SRRs for SRA samples).")
    ncbi_accession: str | None = Field(default=None, description="Canonical semicolon-separated SRR list.")
    biosample_id: str | None = Field(default=None, description="NCBI BioSample accession, if known.")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary JSON metadata.")
    created_at: Any = Field(description="UTC timestamp when this sample was first registered.")
    updated_at: Any = Field(description="UTC timestamp of the most recent update.")

    model_config = {"from_attributes": True}


def _row_to_response(row: dict) -> SampleResponse:
    return SampleResponse(
        id=row["id"],
        sample_id=row["sample_id"],
        ncbi_accession=row.get("ncbi_accession"),
        biosample_id=row.get("biosample_id"),
        metadata=row["metadata_"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_samples_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/samples", tags=["samples"])
    svc = SampleService(engine=engine)

    @router.post(
        "",
        response_model=SampleResponse,
        status_code=201,
        summary="Register or update a sample",
        description=(
            "Adds a sample to the catalog, or updates its fields if it already exists "
            "(upsert on `sample_id`). For SRA samples, derive `sample_id` from the SRR "
            "list using `srrs_to_sample_id(srrs)` so that identity is content-addressed. "
            "Call `POST /admin/reconcile-jobs` after registering new samples to create pending jobs."
        ),
    )
    async def register_sample(req: SampleRegisterRequest):
        row = await svc.register(
            req.sample_id,
            ncbi_accession=req.ncbi_accession,
            biosample_id=req.biosample_id,
            metadata=req.metadata,
        )
        return _row_to_response(row)

    @router.get(
        "",
        response_model=list[SampleResponse],
        summary="List samples",
        description="Returns a paginated list of all samples, ordered by insertion time.",
    )
    async def list_samples(
        limit: int = Query(default=100, ge=1, le=1000, description="Maximum number of samples to return."),
        offset: int = Query(default=0, ge=0, description="Number of samples to skip."),
    ):
        rows = await svc.list_samples(limit=limit, offset=offset)
        return [_row_to_response(r) for r in rows]

    @router.get(
        "/by-srr/{srr_accession}",
        response_model=SampleResponse,
        summary="Look up a sample by SRR accession",
        description=(
            "Returns the sample whose `ncbi_accession` contains the given SRR. "
            "Returns 404 if no sample is registered with that accession."
        ),
    )
    async def get_by_srr(srr_accession: str):
        row = await svc.get_by_srr(srr_accession)
        if not row:
            raise HTTPException(status_code=404, detail=f"No sample found with SRR '{srr_accession}'")
        return _row_to_response(row)

    @router.get(
        "/by-biosample/{biosample_id}",
        response_model=list[SampleResponse],
        summary="Look up samples by BioSample accession",
        description=(
            "Returns all samples with the given `biosample_id`, newest first. "
            "Multiple results occur when the BioSample gained new SRRs over time."
        ),
    )
    async def get_by_biosample(biosample_id: str):
        rows = await svc.get_by_biosample(biosample_id)
        return [_row_to_response(r) for r in rows]

    @router.get(
        "/{sample_id}",
        response_model=SampleResponse,
        summary="Get a sample by ID",
        description="Retrieves a single sample by its `sample_id`. Returns 404 if not found.",
    )
    async def get_sample(sample_id: str):
        row = await svc.get(sample_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")
        return _row_to_response(row)

    return router
