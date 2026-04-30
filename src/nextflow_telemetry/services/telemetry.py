"""Telemetry ingest service.

Handles writing raw weblog events and updating workflow_runs / jobs state
based on the event type.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import dead_letter_tbl, jobs_tbl, telemetry_tbl, workflow_runs_tbl
from ..models import Telemetry


def _parse_tag(tag: str | None) -> str | None:
    """Extract sample_id from a tag of the form 'sample_id:run_name'.

    Returns None if the tag is absent or does not match the convention.
    """
    if not tag:
        return None
    parts = tag.split(":", 1)
    return parts[0] if len(parts) == 2 else None


@dataclass
class TelemetryService:
    engine: AsyncEngine

    async def ingest(self, event: Telemetry) -> None:
        """Persist a weblog event and update execution state."""
        now = datetime.now(timezone.utc)

        # Extract sample_id from the trace tag field
        tag: str | None = None
        if isinstance(event.trace, dict):
            tag = event.trace.get("tag")
        sample_id = _parse_tag(tag)

        # Derive workflow identity from metadata params when present
        workflow_id: str | None = None
        workflow_version: str | None = None
        if isinstance(event.metadata, dict):
            params = event.metadata.get("params") or {}
            workflow_id = params.get("workflow_id")
            workflow_version = params.get("workflow_version")

        async with self.engine.begin() as conn:
            # 1. Append raw event
            await conn.execute(
                insert(telemetry_tbl).values(
                    run_id=event.run_id,
                    run_name=event.run_name,
                    event=event.event,
                    utc_time=event.timestamp,
                    sample_id=sample_id,
                    workflow_id=workflow_id,
                    workflow_version=workflow_version,
                    metadata_=event.metadata,
                    trace=event.trace,
                )
            )

            # 2. Run-level started: transition workflow_run + jobs to running
            if event.event == "started":
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == event.run_name)
                    .values(run_id=event.run_id, status="running", started_at=now)
                )
                await conn.execute(
                    update(jobs_tbl)
                    .where(
                        jobs_tbl.c.run_name == event.run_name,
                        jobs_tbl.c.status == "claimed",
                    )
                    .values(status="running")
                )

            # 3. Per-sample completion via MARK_COMPLETE sentinel process
            elif (
                event.event == "process_completed"
                and sample_id
                and isinstance(event.trace, dict)
                and event.trace.get("process", "").endswith("MARK_COMPLETE")
                and event.trace.get("status") == "COMPLETED"
            ):
                await conn.execute(
                    update(jobs_tbl)
                    .where(
                        jobs_tbl.c.run_name == event.run_name,
                        jobs_tbl.c.sample_id == sample_id,
                    )
                    .values(status="completed", completed_at=now)
                )

            # 4. Run-level completed: sweep non-completed jobs to failed + DLQ
            elif event.event == "completed":
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == event.run_name)
                    .values(status="completed", completed_at=now)
                )
                await self._sweep_incomplete_to_failed(conn, event.run_name, now)

    async def _sweep_incomplete_to_failed(self, conn, run_name: str, now: datetime) -> None:
        """Mark all non-completed jobs for this run as failed and enqueue DLQ."""
        result = await conn.execute(
            update(jobs_tbl)
            .where(
                jobs_tbl.c.run_name == run_name,
                jobs_tbl.c.status.in_(["pending", "running", "claimed"]),
            )
            .values(
                status="failed",
                failed_at=now,
                failure_reason="run completed without MARK_COMPLETE",
                retry_count=jobs_tbl.c.retry_count + 1,
            )
            .returning(
                jobs_tbl.c.id,
                jobs_tbl.c.sample_id,
                jobs_tbl.c.workflow_id,
                jobs_tbl.c.workflow_version,
            )
        )
        failed_rows = result.mappings().all()

        if failed_rows:
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
                    for row in failed_rows
                ],
            )
