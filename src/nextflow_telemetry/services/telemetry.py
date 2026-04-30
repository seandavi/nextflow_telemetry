"""Telemetry ingest service.

Handles writing raw weblog events and updating workflow_executions /
workflow_runs state based on the event type.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import dead_letter_tbl, telemetry_tbl, workflow_executions_tbl, workflow_runs_tbl
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

            # 2. Update workflow_runs when we see the run-level started event
            if event.event == "started":
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == event.run_name)
                    .values(run_id=event.run_id, status="running", started_at=now)
                )

            # 3. Per-sample completion: MARK_COMPLETE process_completed event
            elif (
                event.event == "process_completed"
                and sample_id
                and isinstance(event.trace, dict)
                and event.trace.get("process", "").endswith("MARK_COMPLETE")
                and event.trace.get("status") == "COMPLETED"
            ):
                await conn.execute(
                    update(workflow_executions_tbl)
                    .where(
                        workflow_executions_tbl.c.run_name == event.run_name,
                        workflow_executions_tbl.c.sample_id == sample_id,
                    )
                    .values(status="completed", completed_at=now)
                )

            # 4. Run-level completion: sweep any non-completed samples to failed
            elif event.event == "completed":
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == event.run_name)
                    .values(status="completed", completed_at=now)
                )
                await self._sweep_incomplete_to_failed(conn, event.run_name, now)

    async def _sweep_incomplete_to_failed(self, conn, run_name: str, now: datetime) -> None:
        """Mark all non-completed executions for this run as failed and enqueue DLQ."""
        result = await conn.execute(
            update(workflow_executions_tbl)
            .where(
                workflow_executions_tbl.c.run_name == run_name,
                workflow_executions_tbl.c.status.in_(["pending", "running"]),
            )
            .values(
                status="failed",
                failed_at=now,
                failure_reason="run completed without MARK_COMPLETE",
            )
            .returning(
                workflow_executions_tbl.c.id,
                workflow_executions_tbl.c.sample_id,
                workflow_executions_tbl.c.workflow_id,
                workflow_executions_tbl.c.workflow_version,
            )
        )
        failed_rows = result.mappings().all()

        if failed_rows:
            await conn.execute(
                insert(dead_letter_tbl),
                [
                    {
                        "execution_id": row["id"],
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
