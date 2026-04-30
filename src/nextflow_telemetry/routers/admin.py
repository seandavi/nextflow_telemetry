"""Admin router — operational endpoints for reconciliation and maintenance."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.reconcile import ReconcileService


def create_admin_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])
    reconcile_svc = ReconcileService(engine=engine)

    @router.post("/reconcile-jobs")
    async def reconcile_jobs():
        """Create pending jobs for any (sample × active-workflow) pair that lacks one.

        Safe to call repeatedly — uses ON CONFLICT DO NOTHING.
        Uses a Postgres advisory lock to prevent concurrent runs.
        """
        created = await reconcile_svc.reconcile_jobs()
        return {"jobs_created": created}

    return router
