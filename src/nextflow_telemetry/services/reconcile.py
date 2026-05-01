"""Job reconciliation service.

reconcile_jobs() computes the cross-product of (samples × active workflows)
and creates a pending job for any combination that does not yet have one.
A Postgres advisory lock prevents concurrent reconciliations from racing.

sweep_run_incomplete() is a shared helper used by both the telemetry ingest
path (on receipt of a Nextflow 'completed' event) and the admin close-run
endpoint (called from the SLURM script after Nextflow exits, regardless of
exit code). It retries jobs within budget or routes them to the dead-letter
queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncConnection

from ..db import dead_letter_tbl, jobs_tbl, samples_tbl, workflows_tbl

# Arbitrary but stable lock id — must not collide with other advisory locks
_RECONCILE_LOCK_ID = 0x4A4F425F5245434F  # "JOB_RECO" in hex


async def sweep_run_incomplete(conn: AsyncConnection, run_name: str, now: datetime) -> int:
    """Sweep non-completed jobs for a run: retry within budget or send to DLQ.

    Jobs where retry_count < max_retries are reset to 'pending' with
    run_name=NULL so they re-enter the dispatch pool. Jobs that have
    exhausted retries are marked 'failed' and written to the dead-letter
    table. Returns the number of jobs swept.

    Idempotent: jobs already in a terminal state (completed, failed) are
    not touched.
    """
    max_retries_subq = (
        select(workflows_tbl.c.max_retries)
        .where(workflows_tbl.c.id == jobs_tbl.c.workflow_pk)
        .scalar_subquery()
    )
    has_retries = jobs_tbl.c.retry_count < max_retries_subq

    result = await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.status.in_(["running", "claimed"]),
        )
        .values(
            retry_count=jobs_tbl.c.retry_count + 1,
            status=case((has_retries, "pending"), else_="failed"),
            run_name=case((has_retries, None), else_=jobs_tbl.c.run_name),
            failed_at=case((has_retries, None), else_=now),
            failure_reason=case(
                (has_retries, None),
                else_="run completed without MARK_COMPLETE",
            ),
        )
        .returning(
            jobs_tbl.c.id,
            jobs_tbl.c.sample_id,
            jobs_tbl.c.workflow_id,
            jobs_tbl.c.workflow_version,
            jobs_tbl.c.status,
        )
    )
    swept = result.mappings().all()

    dlq_rows = [r for r in swept if r["status"] == "failed"]
    if dlq_rows:
        await conn.execute(
            insert(dead_letter_tbl),
            [
                {
                    "job_id": row["id"],
                    "run_name": run_name,
                    "sample_id": row["sample_id"],
                    "workflow_id": row["workflow_id"],
                    "workflow_version": row["workflow_version"],
                    "reason": "run completed without MARK_COMPLETE",
                    "created_at": now,
                }
                for row in dlq_rows
            ],
        )

    return len(swept)


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
