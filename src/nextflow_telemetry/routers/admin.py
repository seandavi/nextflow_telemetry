"""Admin router: reconcile, reset, close, requeue and the operator reads (ADR-0013 #373)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_models import (
    ClosedRun,
    CloseRunRequest,
    DeadLetterList,
    Dispatchability,
    ExpiredStaleRuns,
    HeartbeatWatchdog,
    JobPriorityRequest,
    JobPriorityUpdated,
    Reconciled,
    RequeueDeadLetterRequest,
    RequeuedDeadLetter,
    ResetRunning,
    Stats,
)
from ..deps import require_service
from ..services import control_reads, transitions
from .control_route import ControlRoute, clamp, utcnow


def create_admin_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"], route_class=ControlRoute)

    @router.post(
        "/reconcile-jobs",
        response_model=Reconciled,
        dependencies=[Depends(require_service)],
        summary="Create pending jobs for samples × active workflow versions",
        description=(
            "One set-based statement. A workflow's `collections` scope limits its samples; a readset-keyed "
            "workflow gets one job per readset. Idempotent."
        ),
    )
    async def reconcile_jobs():
        async with engine.begin() as conn:
            return {"jobs_created": await transitions.reconcile_jobs(conn)}

    @router.post(
        "/reset-running",
        response_model=ResetRunning,
        dependencies=[Depends(require_service)],
        summary="Reset running and failed jobs of a workflow to pending",
    )
    async def reset_running(workflow_pk: int | None = None):
        if workflow_pk is None:
            raise HTTPException(status_code=422, detail="workflow_pk is required")
        async with engine.begin() as conn:
            return {"reset": await transitions.reset_jobs_to_pending(conn, workflow_pk, now=utcnow())}

    @router.post(
        "/close-run",
        response_model=ClosedRun,
        dependencies=[Depends(require_service)],
        summary="Force-close a run as completed and sweep its jobs",
        description="`run_name` as a query parameter or in the JSON body.",
    )
    async def close_run(run_name: str | None = Query(None), req: CloseRunRequest | None = Body(None)):
        name = run_name or (req.run_name if req else None)
        if not name:
            raise HTTPException(status_code=422, detail="run_name is required")
        async with engine.begin() as conn:
            closed = await transitions.close_run(conn, name, "completed", None, now=utcnow())
        if closed is None:
            raise HTTPException(status_code=404, detail=f"No workflow run with name '{name}'")
        return {"run_name": name, "already_closed": closed.already_closed, "swept": closed.swept}

    @router.post(
        "/expire-stale-runs",
        response_model=ExpiredStaleRuns,
        dependencies=[Depends(require_service)],
        deprecated=True,
        summary="Deprecated no-op: run expiry is a per-run deadline",
    )
    async def expire_stale_runs(response: Response):
        response.headers["Deprecation"] = "true"
        return {"stale_runs_closed": 0, "jobs_swept": 0}

    @router.post(
        "/heartbeat-watchdog",
        response_model=HeartbeatWatchdog,
        dependencies=[Depends(require_service)],
        deprecated=True,
        summary="Deprecated no-op: liveness is a per-run deadline",
    )
    async def heartbeat_watchdog(response: Response):
        response.headers["Deprecation"] = "true"
        return {"checked_at": utcnow(), "stale_after_minutes": 0, "stale_runs_failed": 0, "jobs_swept": 0, "runs": []}

    @router.post(
        "/requeue-dead-letter",
        response_model=RequeuedDeadLetter,
        dependencies=[Depends(require_service)],
        summary="Return dead-lettered jobs to pending",
        description=(
            "Optional filters in the JSON body: `workflow_pk`, `collection`, `job_ids`. No body, or `{}`, "
            "requeues every unresolved row. Only jobs still `failed` move; every matching row is resolved."
        ),
    )
    async def requeue_dead_letter(req: RequeueDeadLetterRequest | None = Body(None)):
        req = req or RequeueDeadLetterRequest()
        async with engine.begin() as conn:
            requeued = await transitions.requeue_dead_letter(
                conn, workflow_pk=req.workflow_pk, collection=req.collection, job_ids=req.job_ids, now=utcnow()
            )
        return {"requeued": requeued}

    @router.post(
        "/job-priority",
        response_model=JobPriorityUpdated,
        dependencies=[Depends(require_service)],
        summary="Set the claim priority of a collection's pending jobs (higher is claimed first)",
        description=(
            "Sets `priority` on every pending job whose sample is in `collection_id`, limited to `workflow_pk` "
            "when given. Claimed, running and finished jobs keep theirs. `updated` counts the jobs matched, so a "
            "repeat call returns the same. 404 for an unknown collection or workflow."
        ),
    )
    async def job_priority(req: JobPriorityRequest):
        async with engine.begin() as conn:
            updated = await transitions.set_job_priority(
                conn, collection_id=req.collection_id, priority=req.priority, workflow_pk=req.workflow_pk
            )
        if updated is None:
            what = f"Collection '{req.collection_id}'" if req.workflow_pk is None else (
                f"Collection '{req.collection_id}' or workflow {req.workflow_pk}"
            )
            raise HTTPException(status_code=404, detail=f"{what} not found")
        return {"updated": updated}

    @router.get(
        "/dead-letter",
        response_model=DeadLetterList,
        summary="Dead-lettered jobs, newest first",
        description="Filter by `workflow_pk`, `collection` and `resolved` (omit for both).",
    )
    async def list_dead_letter(
        workflow_pk: int | None = None,
        collection: str | None = None,
        resolved: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        limit, offset = clamp(limit, 1, 1000), max(0, offset)
        async with engine.connect() as conn:
            items, total = await control_reads.list_dead_letter(
                conn, workflow_pk=workflow_pk, collection=collection, resolved=resolved, limit=limit, offset=offset
            )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @router.get(
        "/dispatchability",
        response_model=Dispatchability,
        summary="Pending work no active daemon can claim",
    )
    async def dispatchability():
        async with engine.connect() as conn:
            return await control_reads.dispatchability(conn, utcnow())

    @router.get("/stats", response_model=Stats, summary="Counts of samples, workflows, jobs, runs and dead letters")
    async def stats():
        async with engine.connect() as conn:
            return await control_reads.stats(conn)

    return router
