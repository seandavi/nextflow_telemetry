"""Dispatch router: the daemon-facing claim and submitted calls (ADR-0013 #373)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_models import ClaimedBatch, ClaimRequest, RequeuedExpired, Submitted, SubmittedRequest
from ..deps import require_service
from ..services import transitions
from .control_route import ControlRoute, clamp, utcnow


def _workflow_ids(requested: str | list[str] | None) -> list[str]:
    """A string or list, each comma-split and stripped, empties dropped (#250)."""
    if requested is None:
        return []
    items = [requested] if isinstance(requested, str) else requested
    return [part.strip() for item in items for part in item.split(",") if part.strip()]


def create_dispatch_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/dispatch", tags=["dispatch"], route_class=ControlRoute)

    @router.post(
        "/batch",
        response_model=ClaimedBatch,
        dependencies=[Depends(require_service)],
        responses={204: {"description": "No pending jobs are available for the requested workflow filter."}},
        summary="Claim a batch of pending jobs",
        description=(
            "Locks up to `limit` pending jobs of one workflow version (highest `priority` first, then oldest), "
            "marks them `claimed` and mints the run. Returns everything needed to build the `nextflow run` "
            "command. **HTTP 204** (no body) when nothing is pending for the filter. A claim that is not "
            "confirmed with `POST /dispatch/submitted` before `CLAIM_TTL_MINUTES` expires and its jobs return "
            "to the pool."
        ),
    )
    async def dispatch_batch(req: ClaimRequest):
        async with engine.begin() as conn:
            batch = await transitions.claim_batch(
                conn,
                limit=clamp(req.limit, 1, 500),
                workflow_ids=_workflow_ids(req.workflow_id),
                workflow_version=req.workflow_version,
                now=utcnow(),
            )
        return batch if batch is not None else Response(status_code=204)

    @router.post(
        "/submitted",
        response_model=Submitted,
        dependencies=[Depends(require_service)],
        summary="Confirm a run has been submitted to the executor",
        description=(
            "Moves the run from `claimed` to `submitted` and records the executor job id. "
            "404 when the run is unknown or no longer `claimed`."
        ),
    )
    async def report_submitted(req: SubmittedRequest):
        async with engine.begin() as conn:
            ok = await transitions.mark_submitted(conn, req.run_name, req.executor_job_id, now=utcnow())
        if not ok:
            raise HTTPException(status_code=404, detail=f"No claimed run with name '{req.run_name}' found")
        return {"run_name": req.run_name, "status": "submitted"}

    @router.post(
        "/requeue-expired",
        response_model=RequeuedExpired,
        dependencies=[Depends(require_service)],
        deprecated=True,
        summary="Deprecated no-op: claim expiry is a per-run deadline",
    )
    async def requeue_expired(response: Response):
        response.headers["Deprecation"] = "true"
        return {"requeued_runs": 0}

    return router
