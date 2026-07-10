"""Telemetry ingest service.

Handles writing raw weblog events and updating workflow_runs / jobs state
based on the event type.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import telemetry_tbl, workflow_runs_tbl, task_executions_tbl
from ..models import Telemetry
from . import lifecycle
from .lifecycle import RunStatus


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


def _parse_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_int(v: Any, default: int = 1) -> int:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _parse_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


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
            telemetry_res = await conn.execute(
                insert(telemetry_tbl).returning(telemetry_tbl.c.id).values(
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
            telemetry_id = telemetry_res.scalar_one()

            # 1b. Populate task_executions for completed processes
            if event.event == "process_completed" and isinstance(event.trace, dict):
                trace = event.trace
                await conn.execute(
                    insert(task_executions_tbl).values(
                        telemetry_id=telemetry_id,
                        run_name=event.run_name,
                        run_id=event.run_id,
                        sample_id=sample_id,
                        workflow_id=workflow_id,
                        workflow_version=workflow_version,
                        utc_time=event.timestamp,
                        task_id=str(trace.get("task_id", "")),
                        task_hash=_parse_str(trace.get("hash")),
                        process=_parse_str(trace.get("process", "")),
                        name=_parse_str(trace.get("name")),
                        status=_parse_str(trace.get("status", "")),
                        attempt=_parse_int(trace.get("attempt")),
                        exit_code=_parse_str(trace.get("exit")),
                        error_action=_parse_str(trace.get("error_action")),
                        realtime_ms=_parse_float(trace.get("realtime")),
                        requested_cpus=_parse_float(trace.get("cpus")),
                        requested_memory_bytes=_parse_float(trace.get("memory")),
                        requested_time_ms=_parse_float(trace.get("time")),
                        pct_cpu=_parse_float(trace.get("%cpu")),
                        pct_mem=_parse_float(trace.get("%mem")),
                        peak_rss=_parse_float(trace.get("peak_rss")),
                        read_bytes=_parse_float(trace.get("read_bytes")),
                        write_bytes=_parse_float(trace.get("write_bytes")),
                        rchar=_parse_float(trace.get("rchar")),
                        wchar=_parse_float(trace.get("wchar")),
                    )
                )

            # 2. Run-level started: transition workflow_run + jobs to running
            if event.event == "started":
                await lifecycle.mark_running(conn, event.run_name, event.run_id, now)

            # 3. Per-sample completion via MARK_COMPLETE sentinel process
            elif (
                event.event == "process_completed"
                and sample_id
                and isinstance(event.trace, dict)
                and event.trace.get("process", "").endswith("MARK_COMPLETE")
                and event.trace.get("status") == "COMPLETED"
            ):
                await lifecycle.complete_sample(conn, event.run_name, sample_id, now)

            # 4. Run-level completed: close the run and sweep incomplete jobs.
            # close_run is idempotent — if the watchdog already marked this run
            # `failed` (walltime/zombie), a late `completed` weblog event will
            # NOT flip it back to `completed`. This is a deliberate change from
            # the old unconditional update, which could clobber a terminal state.
            elif event.event == "completed":
                await lifecycle.close_run(conn, event.run_name, RunStatus.completed, now)
                await lifecycle.sweep_incomplete(conn, event.run_name, now)
