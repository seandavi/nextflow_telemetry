"""Admin router — operational endpoints for reconciliation and maintenance."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.reconcile import ReconcileService


def create_admin_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])
    reconcile_svc = ReconcileService(engine=engine)

    @router.post(
        "/reconcile-jobs",
        summary="Reconcile the jobs table",
        description=(
            "Scans the cross-product of all registered samples and all `active` workflows, "
            "then inserts a `pending` job for every (sample, workflow_id, version) triple that "
            "does not yet have one. Uses `ON CONFLICT DO NOTHING` so it is safe to call repeatedly "
            "and is idempotent. "
            "Call this after registering new samples or activating a new workflow version to ensure "
            "the dispatch pool is up to date."
        ),
    )
    async def reconcile_jobs():
        created = await reconcile_svc.reconcile_jobs()
        return {"jobs_created": created}

    return router
