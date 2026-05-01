"""Admin router — operational endpoints for reconciliation and maintenance."""
from __future__ import annotations

import datetime

from fastapi import APIRouter
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import dead_letter_tbl, jobs_tbl
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

    @router.post(
        "/requeue-dead-letter",
        summary="Requeue dead-letter jobs",
        description=(
            "Moves all unresolved dead-letter entries back to `pending` by resetting the "
            "associated job status and retry_count, then marks the dead_letter row as resolved. "
            "Use this to recover samples that were dead-lettered due to infrastructure failures "
            "rather than application errors."
        ),
    )
    async def requeue_dead_letter():
        now = datetime.datetime.now(datetime.timezone.utc)
        async with engine.begin() as conn:
            # Find unresolved dead_letter job_ids
            from sqlalchemy import select
            rows = (await conn.execute(
                select(dead_letter_tbl.c.id, dead_letter_tbl.c.job_id)
                .where(dead_letter_tbl.c.resolved_at.is_(None))
            )).all()

            if not rows:
                return {"requeued": 0}

            job_ids = [r.job_id for r in rows]
            dlq_ids = [r.id for r in rows]

            # Reset jobs to pending
            await conn.execute(
                update(jobs_tbl)
                .where(jobs_tbl.c.id.in_(job_ids))
                .values(status="pending", retry_count=0, run_name=None,
                        failed_at=None, failure_reason=None)
            )
            # Mark dead_letter rows resolved
            await conn.execute(
                update(dead_letter_tbl)
                .where(dead_letter_tbl.c.id.in_(dlq_ids))
                .values(resolved_at=now)
            )

        return {"requeued": len(job_ids)}

    return router
