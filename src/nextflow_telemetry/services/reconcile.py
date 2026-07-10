"""Job reconciliation service.

reconcile_jobs() computes the cross-product of (samples × active workflows)
and creates a pending job for any combination that does not yet have one.
A Postgres advisory lock prevents concurrent reconciliations from racing.

Job/run status TRANSITIONS (claim, submit, complete, close, sweep, etc.)
live in services/lifecycle.py — this module only handles job *birth* (the
INSERT that creates new pending rows).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Arbitrary but stable lock id — must not collide with other advisory locks
_RECONCILE_LOCK_ID = 0x4A4F425F5245434F  # "JOB_RECO" in hex


@dataclass
class ReconcileService:
    engine: AsyncEngine

    async def reconcile_jobs(self) -> int:
        """Ensure jobs exist for every (sample, active-workflow) pair.

        Returns the number of new jobs created.
        Uses a Postgres advisory transaction lock to prevent concurrent runs.

        The cross-product is computed in Postgres via INSERT...SELECT so that
        the wire payload binds a single parameter regardless of sample count.
        Materialising the cross-product client-side hits asyncpg's 32767
        bound-parameter cap once samples × workflows × 7 columns exceeds it.
        """
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            # Acquire advisory lock for the duration of this transaction
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _RECONCILE_LOCK_ID},
            )

            result = await conn.execute(
                text(
                    """
                    INSERT INTO jobs (
                        sample_id, workflow_pk, workflow_id, workflow_version,
                        status, retry_count, created_at
                    )
                    SELECT s.sample_id, w.id, w.workflow_id, w.version,
                           'pending', 0, :now
                    FROM samples s CROSS JOIN workflows w
                    WHERE w.status = 'active'
                    ON CONFLICT ON CONSTRAINT uq_job_composite DO NOTHING
                    """
                ),
                {"now": now},
            )
            return result.rowcount or 0
