"""Telemetry ingest service.

Handles writing raw weblog events and updating workflow_runs / jobs state
based on the event type.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, telemetry_tbl, workflow_runs_tbl
from ..models import Telemetry
from .reconcile import sweep_run_incomplete


def _parse_tag(tag: str | None) -> str | None:
    """Extract sample_id from a Nextflow process tag.

    The pipeline tags every process with a bare ``"${meta.sample}"`` (the sample
    id, no suffix), so the sample id is the whole tag. We also tolerate a
    historical ``"sample_id:run_name"`` form and take the part before the first
    colon — the run is already disambiguated by ``run_name`` everywhere we match
    on the sample.

    Returns None only if the tag is absent.

    NOTE: requiring the colon form here was a latent bug — it returned None for
    the bare tags the pipeline actually emits, so MARK_COMPLETE never set
    sample_id and per-sample completion never fired (jobs swept to the DLQ
    despite clean runs). Sample ids do not contain ':'.
    """
    if not tag:
        return None
    return tag.split(":", 1)[0]


@dataclass
class TelemetryService:
    engine: AsyncEngine

    async def ingest(self, event: Telemetry) -> None:
        """Persist a weblog event and update execution state."""
        now = datetime.now(timezone.utc)

        tag: str | None = None
        if isinstance(event.trace, dict):
            tag = event.trace.get("tag")
        sample_id = _parse_tag(tag)

        async with self.engine.begin() as conn:
            # Resolve workflow_id/version from the catalog via run_name — more
            # robust than reading from metadata.params, which requires the pipeline
            # to pass those values explicitly.
            run_row = await conn.execute(
                select(workflow_runs_tbl.c.workflow_id, workflow_runs_tbl.c.workflow_version)
                .where(workflow_runs_tbl.c.run_name == event.run_name)
            )
            _run = run_row.mappings().first()
            workflow_id: str | None = _run["workflow_id"] if _run else None
            workflow_version: str | None = _run["workflow_version"] if _run else None

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
                # Jobs reach `running` from either `submitted` (the
                # normal flow once /dispatch/submitted has fired) or
                # `claimed` (defensive: events out of order, or pre-#73
                # jobs that haven't transitioned through submitted yet).
                await conn.execute(
                    update(jobs_tbl)
                    .where(
                        jobs_tbl.c.run_name == event.run_name,
                        jobs_tbl.c.status.in_(["claimed", "submitted"]),
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

            # 4. Run-level completed: close the run and sweep incomplete jobs
            elif event.event == "completed":
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == event.run_name)
                    .values(status="completed", completed_at=now)
                )
                await sweep_run_incomplete(conn, event.run_name, now)
