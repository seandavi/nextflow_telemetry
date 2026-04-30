"""Job reconciliation service.

reconcile_jobs() computes the cross-product of (samples × active workflows)
and creates a pending job for any combination that does not yet have one.
A Postgres advisory lock prevents concurrent reconciliations from racing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, samples_tbl, workflows_tbl

# Arbitrary but stable lock id — must not collide with other advisory locks
_RECONCILE_LOCK_ID = 0x4A4F425F5245434F  # "JOB_RECO" in hex


@dataclass
class ReconcileService:
    engine: AsyncEngine

    async def reconcile_jobs(self) -> int:
        """Ensure jobs exist for every (sample, active-workflow) pair.

        Returns the number of new jobs created.
        Uses a Postgres advisory transaction lock to prevent concurrent runs.
        """
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            # Acquire advisory lock for the duration of this transaction
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _RECONCILE_LOCK_ID},
            )

            # Fetch all sample IDs
            samples_result = await conn.execute(
                select(samples_tbl.c.sample_id)
            )
            sample_ids = [r[0] for r in samples_result]

            # Fetch all active workflows
            workflows_result = await conn.execute(
                select(
                    workflows_tbl.c.id,
                    workflows_tbl.c.workflow_id,
                    workflows_tbl.c.version,
                ).where(workflows_tbl.c.status == "active")
            )
            workflows = workflows_result.mappings().all()

            if not sample_ids or not workflows:
                return 0

            # Build cross-product rows
            rows = [
                {
                    "sample_id": sid,
                    "workflow_pk": wf["id"],
                    "workflow_id": wf["workflow_id"],
                    "workflow_version": wf["version"],
                    "status": "pending",
                    "retry_count": 0,
                    "created_at": now,
                }
                for sid in sample_ids
                for wf in workflows
            ]

            # Upsert: skip pairs that already have a job
            result = await conn.execute(
                pg_insert(jobs_tbl)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_job_composite")
                .returning(jobs_tbl.c.id)
            )
            return len(result.fetchall())
