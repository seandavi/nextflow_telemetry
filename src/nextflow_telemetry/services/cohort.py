"""Cohort (collection) summary service.

A "cohort" is a collection — the existing collections / collection_samples
tables already provide a many-to-many sample grouping, so the cohort summary
view layers on top without new schema.

This service answers two questions:

1. *How is the cohort doing for a given workflow?* — total samples, job-status
   breakdown, completion percentage, and which Nextflow processes are
   producing the most failures.
2. *Which specific samples failed at a given process?* — drill-down for the
   click-through-to-inspect flow on the cohort summary page.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


_JOB_STATUSES = ("pending", "claimed", "submitted", "running", "completed", "failed")


@dataclass
class CohortService:
    engine: AsyncEngine

    async def list_cohorts(self) -> list[dict]:
        """Return all collections with sample counts, ordered newest first."""
        sql = text(
            """
            SELECT c.collection_id, c.source, c.label, c.created_at, c.updated_at,
                   COUNT(cs.sample_id) AS sample_count
            FROM collections c
            LEFT JOIN collection_samples cs USING (collection_id)
            GROUP BY c.collection_id, c.source, c.label, c.created_at, c.updated_at
            ORDER BY c.created_at DESC
            """
        )
        async with self.engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(sql)).mappings()]

    async def summary(
        self,
        collection_id: str,
        workflow_id: str | None,
        workflow_version: str | None,
    ) -> dict | None:
        """Return cohort summary or None if the collection does not exist."""
        async with self.engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        "SELECT collection_id, source, label "
                        "FROM collections WHERE collection_id = :cid"
                    ),
                    {"cid": collection_id},
                )
            ).mappings().first()
            if not exists:
                return None

            sample_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) AS n FROM collection_samples WHERE collection_id = :cid"
                    ),
                    {"cid": collection_id},
                )
            ).scalar() or 0

            params: dict = {"cid": collection_id}
            wf_filter = ""
            if workflow_id:
                wf_filter += " AND j.workflow_id = :workflow_id"
                params["workflow_id"] = workflow_id
            if workflow_version:
                wf_filter += " AND j.workflow_version = :workflow_version"
                params["workflow_version"] = workflow_version

            status_rows = (
                await conn.execute(
                    text(
                        f"""
                        SELECT j.status, COUNT(*) AS n
                        FROM jobs j
                        JOIN collection_samples cs ON cs.sample_id = j.sample_id
                        WHERE cs.collection_id = :cid
                          {wf_filter}
                        GROUP BY j.status
                        """
                    ),
                    params,
                )
            ).mappings().all()
            counts = {s: 0 for s in _JOB_STATUSES}
            for r in status_rows:
                if r["status"] in counts:
                    counts[r["status"]] = r["n"]
            total = sum(counts.values())
            completion_pct = (counts["completed"] / total * 100.0) if total > 0 else 0.0

            failure_rows = (
                await conn.execute(
                    text(
                        f"""
                        SELECT t.trace->>'process' AS process,
                               COUNT(*) AS failed_count,
                               COUNT(DISTINCT t.sample_id) AS sample_count
                        FROM telemetry t
                        JOIN collection_samples cs ON cs.sample_id = t.sample_id
                        WHERE cs.collection_id = :cid
                          AND t.event = 'process_completed'
                          AND t.trace->>'status' IN ('FAILED', 'ABORTED')
                          {("AND t.workflow_id = :workflow_id" if workflow_id else "")}
                          {("AND t.workflow_version = :workflow_version" if workflow_version else "")}
                          AND t.trace->>'process' IS NOT NULL
                        GROUP BY t.trace->>'process'
                        ORDER BY failed_count DESC, process
                        """
                    ),
                    params,
                )
            ).mappings().all()

        return {
            "collection_id": collection_id,
            "source": exists["source"],
            "label": exists["label"],
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "sample_count": sample_count,
            "job_status_counts": counts,
            "total_jobs": total,
            "completion_pct": round(completion_pct, 2),
            "failure_by_process": [dict(r) for r in failure_rows],
            "generated_at_utc": datetime.now(timezone.utc),
        }

    async def cohort_exists(self, collection_id: str) -> bool:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM collections WHERE collection_id = :cid"),
                    {"cid": collection_id},
                )
            ).first()
            return row is not None

    async def failures_for_process(
        self,
        collection_id: str,
        process: str,
        workflow_id: str | None,
        workflow_version: str | None,
        limit: int = 200,
    ) -> list[dict]:
        """Return failed task occurrences for a given (cohort, process).

        One row per process_completed FAILED/ABORTED event. Includes task_hash
        so the UI can link straight to the existing log viewer. Caller is
        responsible for checking that the cohort exists (use cohort_exists)
        — this method returns an empty list for unknown cohorts.
        """
        params: dict = {"cid": collection_id, "process": process, "limit": limit}
        wf_filter = ""
        if workflow_id:
            wf_filter += " AND t.workflow_id = :workflow_id"
            params["workflow_id"] = workflow_id
        if workflow_version:
            wf_filter += " AND t.workflow_version = :workflow_version"
            params["workflow_version"] = workflow_version

        sql = text(
            f"""
            SELECT t.id AS telemetry_id,
                   t.sample_id,
                   t.run_name,
                   t.utc_time,
                   t.trace->>'name'   AS task_name,
                   t.trace->>'hash'   AS task_hash,
                   t.trace->>'status' AS status,
                   t.trace->>'exit'   AS exit_code,
                   coalesce(nullif(t.trace->>'attempt',''),'0')::int AS attempt
            FROM telemetry t
            JOIN collection_samples cs ON cs.sample_id = t.sample_id
            WHERE cs.collection_id = :cid
              AND t.event = 'process_completed'
              AND t.trace->>'status' IN ('FAILED', 'ABORTED')
              AND t.trace->>'process' = :process
              {wf_filter}
            ORDER BY t.utc_time DESC
            LIMIT :limit
            """
        )
        async with self.engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(sql, params)).mappings()]
