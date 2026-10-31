"""Sample catalog router: registration (single and bulk), lookups and facets (ADR-0013 #373)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_models import BulkRegistered, CollectionFacets, Sample, SampleList, SampleRegistration
from ..deps import require_service
from ..services import registry
from .control_route import ControlRoute, clamp, utcnow

BULK_MAX_BYTES = 64 * 1024 * 1024
BULK_MEDIA_TYPES = ("application/x-ndjson", "text/tab-separated-values")


def _conflict(err: registry.ReadsetConflict) -> HTTPException:
    return HTTPException(status_code=409, detail=f"runs already belong to another readset: {', '.join(err.runs)}")


async def _read_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > BULK_MAX_BYTES:
        raise HTTPException(status_code=413, detail="body exceeds the 64 MiB limit")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > BULK_MAX_BYTES:
            raise HTTPException(status_code=413, detail="body exceeds the 64 MiB limit")
    return bytes(body)


def create_samples_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/samples", tags=["samples"], route_class=ControlRoute)

    @router.post(
        "",
        response_model=Sample,
        status_code=201,
        dependencies=[Depends(require_service)],
        responses={409: {"description": "A run already belongs to another readset."}},
        summary="Register (upsert) a sample, optionally into a collection",
    )
    async def register_sample(req: SampleRegistration):
        try:
            async with engine.begin() as conn:
                await registry.register_samples(conn, [req], now=utcnow())
                return await registry.get_sample(conn, req.sample_id)
        except registry.ReadsetConflict as err:
            raise _conflict(err) from err

    @router.post(
        "/bulk",
        response_model=BulkRegistered,
        dependencies=[Depends(require_service)],
        responses={
            409: {"description": "A run already belongs to another readset."},
            413: {"description": "Over 64 MiB or 100,000 rows."},
            415: {"description": "Content-Type is not NDJSON or TSV."},
        },
        summary="Bulk-register samples from NDJSON or TSV, in one transaction",
        description=(
            "`Content-Type: application/x-ndjson` (one JSON object per line) or "
            "`text/tab-separated-values` (header with `sample_id`, `ncbi_accession`, optionally "
            "`biosample_id`, `collection`). `?collection=` sets the collection for rows that carry none. "
            "Loaded with COPY into a staging table and merged; any error writes nothing."
        ),
    )
    async def register_samples_bulk(request: Request, collection: str | None = None):
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type not in BULK_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail=f"Content-Type must be one of: {', '.join(BULK_MEDIA_TYPES)}")
        body = await _read_capped(request)
        try:
            rows = registry.parse_bulk(body, media_type, collection)
            async with engine.begin() as conn:
                result = await registry.register_samples(conn, rows, now=utcnow())
        except registry.BulkTooLarge as err:
            raise HTTPException(status_code=413, detail=str(err)) from err
        except registry.BulkParseError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except registry.ReadsetConflict as err:
            raise _conflict(err) from err
        return result.__dict__

    @router.get("", response_model=SampleList, summary="List samples with search and collection filters")
    async def list_samples(limit: int = 100, offset: int = 0, search: str | None = None, collection: str | None = None):
        limit, offset = clamp(limit, 1, 1000), max(0, offset)
        async with engine.connect() as conn:
            items, total = await registry.list_samples(conn, limit=limit, offset=offset, search=search, collection=collection)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @router.get(
        "/facets/collections", response_model=CollectionFacets, summary="Per-collection sample counts over the whole catalog"
    )
    async def collection_facets():
        async with engine.connect() as conn:
            return await registry.collection_facets(conn)

    @router.get("/by-srr/{srr}", response_model=Sample, summary="The sample containing a run accession")
    async def get_by_srr(srr: str):
        async with engine.connect() as conn:
            sample = await registry.get_sample_by_run(conn, srr)
        if sample is None:
            raise HTTPException(status_code=404, detail=f"No sample found with SRR '{srr}'")
        return sample

    @router.get(
        "/by-biosample/{biosample_id}", response_model=list[Sample], summary="Samples annotated with a BioSample id"
    )
    async def get_by_biosample(biosample_id: str):
        async with engine.connect() as conn:
            return await registry.samples_by_biosample(conn, biosample_id)

    @router.get("/{sample_id}", response_model=Sample, summary="One sample by md5 `sample_id` or `RS.` readset id")
    async def get_sample(sample_id: str):
        async with engine.connect() as conn:
            sample = await registry.get_sample(conn, sample_id)
        if sample is None:
            raise HTTPException(status_code=404, detail=f"Sample '{sample_id}' not found")
        return sample

    return router
