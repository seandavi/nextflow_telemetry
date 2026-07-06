"""Submissions router — register a study/BioProject by accession, with provenance.

A submission is the first-class record of a "register these samples" action.
v1 supports the ENA-accession method; the ``method`` field leaves room for a
curation-TSV adapter without changing the resource shape.
"""
from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from ..deps import Principal, require_role_or_token
from ..services.submission import AccessionError, SubmissionService


class SubmissionRequest(BaseModel):
    accession: str = Field(description="Study or BioProject accession (e.g. 'PRJNA694605', 'SRP123456').")
    method: Literal["ena_accession"] = Field(
        default="ena_accession",
        description="How samples are supplied. Only 'ena_accession' is supported today.",
    )
    dry_run: bool = Field(
        default=False,
        description="Preview only: return the counts that would result, write nothing, mint no submission_id.",
    )


class SubmissionReceipt(BaseModel):
    submission_id: str = Field(description="Unique ID for this registration attempt (provenance handle).")
    collection_id: str
    source: str = Field(description="Provenance: 'bioproject' or 'sra_study'.")
    type: str = Field(description="Kind: 'project' for accession-registered collections.")
    status: str = Field(description="'succeeded', 'dry_run', or 'failed'.")
    samples_found: int = Field(description="Distinct samples the accession expanded to.")
    samples_added: int = Field(description="Samples newly registered by this submission.")
    samples_existing: int = Field(description="Samples that already existed (membership added, metadata untouched).")
    library_composition: dict[str, Any] | None = Field(
        default=None,
        description="Per-run library-metadata tallies (library_strategy/selection/source, instrument_platform) "
        "so an approver can sanity-check the study is shotgun metagenomics, not 16S/amplicon.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Advisory flags (e.g. amplicon/16S runs detected). Never blocks a submission.",
    )


def create_submissions_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/submissions", tags=["submissions"])
    svc = SubmissionService(engine=engine)

    @router.post(
        "",
        response_model=SubmissionReceipt,
        status_code=201,
        summary="Register a study/BioProject's samples from an accession",
        description=(
            "Expands an INSDC study or BioProject accession (PRJ… / SRP… / ERP… / DRP…) "
            "to its runs via the ENA Portal API, registers any new samples, records the "
            "collection + membership, and mints a submission_id for provenance. "
            "Registration only — call `POST /admin/reconcile-jobs` afterwards to create "
            "pending jobs. Existing samples keep their metadata. Requires an authenticated "
            "contributor/admin session or a valid operator token."
        ),
    )
    async def create_submission(
        req: SubmissionRequest,
        principal: Principal = Depends(require_role_or_token("contributor")),
    ):
        try:
            return await svc.register_from_accession(
                req.accession, submitted_by=principal.identity, dry_run=req.dry_run
            )
        except AccessionError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"ENA lookup failed: {e}")

    @router.get(
        "/{submission_id}",
        response_model=dict[str, Any],
        summary="Look up a submission by ID",
        description="Returns the submission record (provenance receipt). 404 if unknown.",
    )
    async def get_submission(submission_id: str):
        row = await svc.get(submission_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Submission '{submission_id}' not found")
        return row

    return router
