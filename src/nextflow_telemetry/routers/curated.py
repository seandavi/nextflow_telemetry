"""Curated sample annotations router.

Provides endpoints for importing curatedMetagenomicData-style TSV files
and querying the resulting study/annotation records.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.curated import CuratedService


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class DroppedRowResponse(BaseModel):
    """A row that was skipped during import (missing ncbi_accession)."""
    row_index: int
    subject_id: str | None = None


class ImportSummaryResponse(BaseModel):
    """Summary of a TSV import operation."""
    rows_loaded: int
    rows_updated: int
    rows_dropped: int
    dropped_rows: list[DroppedRowResponse]


class StudyResponse(BaseModel):
    """A curated study record."""
    id: int
    study_name: str
    source_file: str | None = None
    metadata: dict[str, Any] | None = None
    loaded_at: Any

    model_config = {"from_attributes": True}


class AnnotationResponse(BaseModel):
    """A curated sample annotation record."""
    id: int
    sample_id: str
    study_name: str
    ncbi_accession: str | None = None
    metadata: dict[str, Any]
    loaded_at: Any

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_curated_router(engine: AsyncEngine) -> APIRouter:
    """Return a router for curated annotation endpoints, injected with *engine*."""
    router = APIRouter(prefix="/curated", tags=["curated"])
    svc = CuratedService(engine=engine)

    # -----------------------------------------------------------------------
    # Import
    # -----------------------------------------------------------------------

    @router.post(
        "/import",
        response_model=ImportSummaryResponse,
        status_code=200,
        summary="Import a curatedMetagenomicData TSV",
        description=(
            "Upload a tab-separated file where one column (case-insensitive) is "
            "`ncbi_accession`. Each row becomes a `curated_sample_annotations` record "
            "keyed by the content-addressed `sample_id` (md5 of sorted SRRs). "
            "Rows with a missing or empty `ncbi_accession` are dropped and reported. "
            "Re-importing the same study is idempotent — rows are upserted on "
            "`(sample_id, study_name)`."
        ),
    )
    async def import_tsv(
        file: UploadFile = File(..., description="Tab-separated file to import."),
        study_name: str = Form(..., description="Unique name for this study (e.g. 'ArtachoA_2021')."),
        pubmed_id: str | None = Form(default=None, description="PubMed ID for the study."),
        doi: str | None = Form(default=None, description="DOI for the study."),
    ):
        content = await file.read()
        try:
            summary = await svc.import_tsv(
                tsv_content=content,
                study_name=study_name,
                source_file=file.filename,
                pubmed_id=pubmed_id,
                doi=doi,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return ImportSummaryResponse(
            rows_loaded=summary.rows_loaded,
            rows_updated=summary.rows_updated,
            rows_dropped=summary.rows_dropped,
            dropped_rows=[
                DroppedRowResponse(row_index=d.row_index, subject_id=d.subject_id)
                for d in summary.dropped_rows
            ],
        )

    # -----------------------------------------------------------------------
    # Studies
    # -----------------------------------------------------------------------

    @router.get(
        "/studies",
        response_model=list[StudyResponse],
        summary="List all curated studies",
        description="Returns all imported studies ordered by name.",
    )
    async def list_studies():
        rows = await svc.list_studies()
        return [_study_to_response(r) for r in rows]

    @router.get(
        "/studies/{study_name}",
        response_model=StudyResponse,
        summary="Get a curated study by name",
        description="Returns a single study record. Returns 404 if the study does not exist.",
    )
    async def get_study(study_name: str):
        row = await svc.get_study(study_name)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Study '{study_name}' not found")
        return _study_to_response(row)

    @router.get(
        "/studies/{study_name}/samples",
        response_model=list[AnnotationResponse],
        summary="List samples for a curated study",
        description="Returns a paginated list of annotation records for the given study.",
    )
    async def list_study_samples(
        study_name: str,
        limit: int = Query(default=100, ge=1, le=1000, description="Maximum rows to return."),
        offset: int = Query(default=0, ge=0, description="Number of rows to skip."),
    ):
        rows = await svc.list_study_samples(study_name, limit=limit, offset=offset)
        return [_annotation_to_response(r) for r in rows]

    # -----------------------------------------------------------------------
    # Samples (cross-study lookup)
    # -----------------------------------------------------------------------

    @router.get(
        "/samples/{sample_id}",
        response_model=list[AnnotationResponse],
        summary="Get curated annotations for a sample",
        description=(
            "Returns all curated annotation records for the given `sample_id` "
            "across all studies. Returns an empty list if no annotations exist."
        ),
    )
    async def get_sample_annotations(sample_id: str):
        rows = await svc.get_sample_annotations(sample_id)
        return [_annotation_to_response(r) for r in rows]

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _study_to_response(row: Any) -> StudyResponse:
    from ..services.curated import StudyRow
    r: StudyRow = row
    return StudyResponse(
        id=r.id,
        study_name=r.study_name,
        source_file=r.source_file,
        metadata=r.metadata_,
        loaded_at=r.loaded_at,
    )


def _annotation_to_response(row: Any) -> AnnotationResponse:
    from ..services.curated import AnnotationRow
    r: AnnotationRow = row
    return AnnotationResponse(
        id=r.id,
        sample_id=r.sample_id,
        study_name=r.study_name,
        ncbi_accession=r.ncbi_accession,
        metadata=r.metadata_,
        loaded_at=r.loaded_at,
    )
