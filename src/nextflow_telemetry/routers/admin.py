"""Admin router — operational endpoints for reconciliation and maintenance."""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import daemon_agents_tbl, dead_letter_tbl, jobs_tbl, samples_tbl, workflow_runs_tbl, workflows_tbl
from ..services.reconcile import ReconcileService, sweep_run_incomplete

# Keep in sync with routers/daemons.ACTIVE_THRESHOLD — a daemon is "active" if
# its last heartbeat is within this window.
_DAEMON_ACTIVE_THRESHOLD = datetime.timedelta(minutes=2)


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
        "/reset-running",
        summary="Reset stuck running jobs to pending",
        description=(
            "Resets all jobs in 'running' or 'failed' state back to 'pending' for a given "
            "workflow, and clears their run_name and retry_count. Use after infrastructure "
            "failures where Nextflow crashed without sending a completion weblog event and you "
            "want to give the samples a clean slate regardless of retry budget."
        ),
    )
    async def reset_running(workflow_pk: int):
        async with engine.begin() as conn:
            result = await conn.execute(
                update(jobs_tbl)
                .where(
                    jobs_tbl.c.workflow_pk == workflow_pk,
                    jobs_tbl.c.status.in_(["running", "failed"]),
                )
                .values(
                    status="pending",
                    run_name=None,
                    retry_count=0,
                    failed_at=None,
                    failure_reason=None,
                )
            )
        return {"reset": result.rowcount}

    @router.post(
        "/close-run",
        summary="Close a workflow run and sweep incomplete jobs",
        description=(
            "Marks a workflow run as `completed` (if not already in a terminal state) and "
            "sweeps any jobs still in `running` or `claimed` state: jobs within their retry "
            "budget are reset to `pending`; exhausted jobs are failed to the dead-letter queue. "
            "Idempotent — safe to call even if the run already received a Nextflow `completed` "
            "weblog event. Called unconditionally from the SLURM submit script after Nextflow "
            "exits so that crashes and hard kills are always cleaned up."
        ),
    )
    async def close_run(run_name: str):
        now = datetime.datetime.now(datetime.timezone.utc)
        async with engine.begin() as conn:
            # Check the run exists and get its current status
            row = (await conn.execute(
                select(workflow_runs_tbl.c.status)
                .where(workflow_runs_tbl.c.run_name == run_name)
            )).first()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"No workflow run with name '{run_name}'",
                )

            already_closed = row[0] in ("completed", "failed", "expired")

            if not already_closed:
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == run_name)
                    .values(status="completed", completed_at=now)
                )

            swept = await sweep_run_incomplete(conn, run_name, now)

        return {
            "run_name": run_name,
            "already_closed": already_closed,
            "swept": swept,
        }

    @router.post(
        "/expire-stale-runs",
        summary="Close workflow runs stuck in a non-terminal state",
        description=(
            "Finds workflow runs that have been in `running` or `submitted` state for longer "
            "than `older_than_hours` hours and closes them, sweeping any associated jobs "
            "through the retry/dead-letter logic. Use this to recover from batches of runs "
            "that crashed without sending a Nextflow `completed` event before the SLURM "
            "close-run hook was in place."
        ),
    )
    async def expire_stale_runs(older_than_hours: float = 2.0):
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=older_than_hours)
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        total_swept = 0

        async with engine.begin() as conn:
            stale = (await conn.execute(
                select(workflow_runs_tbl.c.run_name)
                .where(
                    workflow_runs_tbl.c.status.in_(["running", "submitted"]),
                    workflow_runs_tbl.c.claimed_at < cutoff,
                )
            )).scalars().all()

            for run_name in stale:
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == run_name)
                    .values(status="completed", completed_at=now)
                )
                total_swept += await sweep_run_incomplete(conn, run_name, now)

        return {"stale_runs_closed": len(stale), "jobs_swept": total_swept}

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
            from sqlalchemy import select
            rows = (await conn.execute(
                select(dead_letter_tbl.c.id, dead_letter_tbl.c.job_id)
                .where(dead_letter_tbl.c.resolved_at.is_(None))
            )).all()

            if not rows:
                return {"requeued": 0}

            job_ids = [r.job_id for r in rows]
            dlq_ids = [r.id for r in rows]

            await conn.execute(
                update(jobs_tbl)
                .where(jobs_tbl.c.id.in_(job_ids))
                .values(status="pending", retry_count=0, run_name=None,
                        failed_at=None, failure_reason=None)
            )
            await conn.execute(
                update(dead_letter_tbl)
                .where(dead_letter_tbl.c.id.in_(dlq_ids))
                .values(resolved_at=now)
            )

        return {"requeued": len(job_ids)}

    @router.get(
        "/dispatchability",
        summary="Find pending work that no active daemon will claim",
        description=(
            "Cross-checks pending jobs on `active` workflows against the set of currently "
            "active daemons (heartbeat within the last 2 minutes) and their `workflow_id` "
            "claim filters. Returns the workflows that have pending jobs but no active daemon "
            "configured to claim them — i.e. work that will silently sit forever. This is the "
            "fast answer to 'why isn't anything running?' when a daemon has died."
        ),
    )
    async def dispatchability():
        now = datetime.datetime.now(datetime.timezone.utc)
        async with engine.begin() as conn:
            pending_rows = (await conn.execute(
                select(
                    jobs_tbl.c.workflow_pk,
                    jobs_tbl.c.workflow_id,
                    func.count().label("pending"),
                )
                .select_from(jobs_tbl.join(workflows_tbl, jobs_tbl.c.workflow_pk == workflows_tbl.c.id))
                .where(jobs_tbl.c.status == "pending", workflows_tbl.c.status == "active")
                .group_by(jobs_tbl.c.workflow_pk, jobs_tbl.c.workflow_id)
            )).all()

            daemon_rows = (await conn.execute(
                select(daemon_agents_tbl.c.workflow_id, daemon_agents_tbl.c.last_seen_at)
            )).all()

        # An active daemon claims a workflow if its filter is empty (claims any)
        # or the workflow_id is in its comma-separated filter list.
        active_filters: list[set[str] | None] = []
        for d in daemon_rows:
            last_seen = d.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
            if (now - last_seen) >= _DAEMON_ACTIVE_THRESHOLD:
                continue
            wf = (d.workflow_id or "").strip()
            active_filters.append({x.strip() for x in wf.split(",") if x.strip()} if wf else None)

        def claimed_by_active(workflow_id: str) -> bool:
            return any(f is None or workflow_id in f for f in active_filters)

        stuck = [
            {
                "workflow_id": r.workflow_id,
                "workflow_pk": r.workflow_pk,
                "pending": r.pending,
                "reason": "no active daemon claims this workflow",
            }
            for r in pending_rows
            if not claimed_by_active(r.workflow_id)
        ]

        return {
            "checked_at": now,
            "active_daemons": len(active_filters),
            "stuck": stuck,
            "stuck_pending_total": sum(s["pending"] for s in stuck),
        }

    @router.get(
        "/stats",
        summary="Summary counts across the catalog and dispatch tables",
        description=(
            "Returns total sample/workflow counts, jobs grouped by status, "
            "workflow runs grouped by status, and the count of unresolved "
            "dead-letter entries. Lightweight — used by `nf-client stats` "
            "to give operators a one-shot system overview."
        ),
    )
    async def stats():
        async with engine.begin() as conn:
            samples_total = (await conn.execute(
                select(func.count()).select_from(samples_tbl)
            )).scalar_one()
            workflows_total = (await conn.execute(
                select(func.count()).select_from(workflows_tbl)
            )).scalar_one()
            jobs_rows = (await conn.execute(
                select(jobs_tbl.c.status, func.count())
                .group_by(jobs_tbl.c.status)
            )).all()
            runs_rows = (await conn.execute(
                select(workflow_runs_tbl.c.status, func.count())
                .group_by(workflow_runs_tbl.c.status)
            )).all()
            dlq_unresolved = (await conn.execute(
                select(func.count()).select_from(dead_letter_tbl)
                .where(dead_letter_tbl.c.resolved_at.is_(None))
            )).scalar_one()

        return {
            "samples": samples_total,
            "workflows": workflows_total,
            "jobs_by_status": {status: count for status, count in jobs_rows},
            "runs_by_status": {status: count for status, count in runs_rows},
            "dead_letter_unresolved": dlq_unresolved,
        }

    return router
