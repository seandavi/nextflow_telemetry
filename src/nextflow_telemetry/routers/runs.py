"""Run-lifecycle event router (issues #62, #63).

Clients (the wrapper, the pipeline, the daemon) POST events to
`/api/runs/{run_name}/event` to describe what is happening *outside* of the
weblog stream — wrapper start/exit, heartbeats, scheduler state, the
Nextflow log itself.

Events are stored two ways:

1. Raw, append-only, in `telemetry_tbl` — same shape as weblog events so
   downstream queries that read the table see one continuous stream.
2. Denormalised summary fields on `workflow_runs` — fast dashboard reads
   without scanning the raw event log.

The .nextflow.log (when attached to `wrapper_exited`) is stored in
`task_logs_tbl` with a sentinel hash so the existing log viewer can serve it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Path, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..models import (
    HeartbeatEvent,
    PreNextflowEvent,
    RunEvent,
    RunEventResponse,
    SlurmStateEvent,
    WorkflowOnCompleteEvent,
    WorkflowOnErrorEvent,
    WrapperExitedEvent,
    WrapperLogEvent,
    WrapperStartedEvent,
)
from ..db import telemetry_tbl, workflow_runs_tbl


_MAX_NEXTFLOW_LOG_BYTES = 16 * 1024 * 1024  # 16 MB
_NEXTFLOW_LOG_SENTINEL_HASH = "nextflow_log"
_NEXTFLOW_LOG_TYPE = "nextflow_log"
# Wrapper log: captured stdout+stderr of the wrapper's nextflow subprocess.
# Smaller cap than nextflow_log because we tail-truncate at the wrapper to
# the last N lines (failure context is at the end), so the upload is
# already bounded.
_MAX_WRAPPER_LOG_BYTES = 4 * 1024 * 1024  # 4 MB
# Distinct from the WrapperLogEvent *event* type (which carries per-line
# log lines as telemetry rows) — this is the captured-on-exit attachment.
_WRAPPER_LOG_SENTINEL_HASH = "wrapper_output_log"
_WRAPPER_LOG_TYPE = "wrapper_output_log"

_run_event_adapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def create_runs_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/runs", tags=["runs"])

    @router.post(
        "/{run_name}/event",
        response_model=RunEventResponse,
        status_code=201,
        summary="Record a run-lifecycle event",
        description=(
            "Append a wrapper / pipeline-hook / daemon event for a Nextflow run. "
            "The event body is a JSON-encoded string posted as the `event` form "
            "field; its `type` discriminator selects the variant. An optional "
            "`nextflow_log` file attachment is only meaningful for `wrapper_exited`; "
            "if present it is stored under the run's task_logs with a sentinel hash "
            "and `nextflow_log_uploaded_at` is set on `workflow_runs`. Re-uploading "
            "the same log is idempotent. Events for an unknown `run_name` are still "
            "appended to the raw `telemetry` table so nothing is dropped, but no "
            "`workflow_runs` row is created — summary fields only update for runs "
            "already on record (typically created by the dispatch claim). "
            "A `nextflow_log` attachment requires a known `run_name`; otherwise the "
            "request fails with 404 to avoid orphan log rows."
        ),
    )
    async def post_run_event(
        run_name: Annotated[str, Path(description="Nextflow run name (matches workflow_runs.run_name).")],
        event: Annotated[str, Form(description="JSON-encoded run event; `type` selects the variant.")],
        nextflow_log: Annotated[UploadFile | None, File(description="Optional .nextflow.log; honoured on `wrapper_exited`.")] = None,
        wrapper_output_log: Annotated[UploadFile | None, File(description="Optional captured stdout+stderr of the wrapper's nextflow subprocess; honoured on `wrapper_exited`. Captures the pre-Nextflow failure surface (`.nextflow.log` may be empty or missing if Nextflow never started). Named distinctly from the `wrapper_log` *event type* (per-line telemetry rows) to avoid confusion.")] = None,
    ) -> RunEventResponse:
        try:
            payload = json.loads(event)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail=f"event field is not valid JSON: {e}")

        try:
            parsed: RunEvent = _run_event_adapter.validate_python(payload)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors())

        # Read and validate the optional .nextflow.log *outside* the DB transaction
        # so we don't hold a connection / locks while decoding a 16 MB payload.
        # Reject attachments on non-wrapper_exited events outright — clients
        # should never attach a log to a heartbeat or slurm_state.
        log_content_str: str | None = None
        if nextflow_log is not None:
            if not isinstance(parsed, WrapperExitedEvent):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "nextflow_log attachment is only valid on wrapper_exited events; "
                        f"received it with type={parsed.type}."
                    ),
                )
            raw = await nextflow_log.read()
            if len(raw) > _MAX_NEXTFLOW_LOG_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"nextflow_log exceeds {_MAX_NEXTFLOW_LOG_BYTES} byte limit.",
                )
            log_content_str = raw.decode("utf-8", errors="replace")

        wrapper_log_content_str: str | None = None
        if wrapper_output_log is not None:
            if not isinstance(parsed, WrapperExitedEvent):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "wrapper_output_log attachment is only valid on wrapper_exited events; "
                        f"received it with type={parsed.type}."
                    ),
                )
            raw = await wrapper_output_log.read()
            if len(raw) > _MAX_WRAPPER_LOG_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"wrapper_output_log exceeds {_MAX_WRAPPER_LOG_BYTES} byte limit.",
                )
            wrapper_log_content_str = raw.decode("utf-8", errors="replace")

        log_uploaded = False
        wrapper_output_log_uploaded = False
        now = datetime.now(timezone.utc)

        async with engine.begin() as conn:
            # 1. Resolve workflow_id / version / run_id from the existing
            # workflow_runs row if known. run_id is Nextflow's own UUID which
            # we don't have at wrapper time — we copy whatever has been recorded
            # from a prior weblog 'started' event so events join cleanly.
            existing = (
                await conn.execute(
                    select(
                        workflow_runs_tbl.c.run_id,
                        workflow_runs_tbl.c.workflow_id,
                        workflow_runs_tbl.c.workflow_version,
                    ).where(workflow_runs_tbl.c.run_name == run_name)
                )
            ).mappings().first()
            # run_id is NOT NULL on telemetry. When the run is known we copy
            # Nextflow's UUID; when it isn't (events arriving before the
            # weblog 'started' event), we fall back to a unique-per-run
            # sentinel so events for distinct runs never share a run_id.
            existing_run_id = existing["run_id"] if existing else None
            run_id = existing_run_id or f"pre-weblog:{run_name}"
            workflow_id = existing["workflow_id"] if existing else None
            workflow_version = existing["workflow_version"] if existing else None

            # If any log attachment was provided but no workflow_runs row exists,
            # 404 — storing it would orphan the row and the response would
            # falsely claim the upload succeeded against a known run. Same
            # rule applies to both .nextflow.log and the captured wrapper log.
            if (log_content_str is not None or wrapper_log_content_str is not None) and existing is None:
                missing = "nextflow_log" if log_content_str is not None else "wrapper_output_log"
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No workflow_runs row for '{run_name}'; refusing to store "
                        f"{missing} attachment as an orphan."
                    ),
                )

            # 2. Append raw event to telemetry (same shape as weblog rows).
            await conn.execute(
                insert(telemetry_tbl).values(
                    run_id=run_id,
                    run_name=run_name,
                    event=f"run_{parsed.type}",
                    utc_time=parsed.utc_time,
                    sample_id=None,
                    workflow_id=workflow_id,
                    workflow_version=workflow_version,
                    metadata_=parsed.model_dump(mode="json"),
                    trace=None,
                )
            )

            # 3. Update workflow_runs summary columns by event variant
            await _apply_summary_update(conn, run_name, parsed, now)

            # 4. Optional .nextflow.log attachment (only on wrapper_exited).
            # Existence of the run row was verified above.
            if log_content_str is not None:
                await conn.execute(
                    text(
                        """
                        INSERT INTO task_logs (run_name, task_hash, log_type, content, uploaded_at)
                        VALUES (:run_name, :task_hash, :log_type, :content, :uploaded_at)
                        ON CONFLICT ON CONSTRAINT uq_task_log
                        DO UPDATE SET content = EXCLUDED.content, uploaded_at = EXCLUDED.uploaded_at
                        """
                    ),
                    {
                        "run_name": run_name,
                        "task_hash": _NEXTFLOW_LOG_SENTINEL_HASH,
                        "log_type": _NEXTFLOW_LOG_TYPE,
                        "content": log_content_str,
                        "uploaded_at": now,
                    },
                )
                await conn.execute(
                    update(workflow_runs_tbl)
                    .where(workflow_runs_tbl.c.run_name == run_name)
                    .values(nextflow_log_uploaded_at=now)
                )
                log_uploaded = True

            # 5. Optional wrapper_output_log attachment. Same upsert pattern as
            # the nextflow_log block above, but with the wrapper-output-log
            # sentinel hash so the two coexist in task_logs without conflict.
            # No summary column on workflow_runs — operators query task_logs
            # directly (the existing log viewer renders it the same way).
            if wrapper_log_content_str is not None:
                await conn.execute(
                    text(
                        """
                        INSERT INTO task_logs (run_name, task_hash, log_type, content, uploaded_at)
                        VALUES (:run_name, :task_hash, :log_type, :content, :uploaded_at)
                        ON CONFLICT ON CONSTRAINT uq_task_log
                        DO UPDATE SET content = EXCLUDED.content, uploaded_at = EXCLUDED.uploaded_at
                        """
                    ),
                    {
                        "run_name": run_name,
                        "task_hash": _WRAPPER_LOG_SENTINEL_HASH,
                        "log_type": _WRAPPER_LOG_TYPE,
                        "content": wrapper_log_content_str,
                        "uploaded_at": now,
                    },
                )
                wrapper_output_log_uploaded = True

        return RunEventResponse(
            run_name=run_name,
            type=parsed.type,
            nextflow_log_uploaded=log_uploaded,
            wrapper_output_log_uploaded=wrapper_output_log_uploaded,
        )

    return router


async def _apply_summary_update(conn, run_name: str, parsed: RunEvent, now: datetime) -> None:
    """Apply event-type-specific updates to workflow_runs summary columns.

    No-ops if the variant carries no summary-relevant fields. Out-of-order
    events do not roll back forward-looking state (e.g. a late `pre_nextflow`
    after `wrapper_exited` does not reopen the run); each variant only writes
    its own narrow fields.

    Raw events are always recorded in telemetry_tbl by the caller. If no
    workflow_runs row exists yet (e.g. a wrapper raced ahead of the claim
    record), summary updates are silently skipped — operators can still
    query telemetry_tbl directly to recover the event stream.
    """
    if isinstance(parsed, WrapperStartedEvent):
        # Mark the run as "submitted" only if it's still in the earlier "claimed" state.
        # We don't want to roll status back from running/completed.
        await conn.execute(
            update(workflow_runs_tbl)
            .where(
                workflow_runs_tbl.c.run_name == run_name,
                workflow_runs_tbl.c.status == "claimed",
            )
            .values(status="submitted", submitted_at=now)
        )
        return

    values: dict = {}
    if isinstance(parsed, PreNextflowEvent):
        if parsed.wait_seconds is not None:
            values["wait_seconds"] = parsed.wait_seconds
    elif isinstance(parsed, WrapperExitedEvent):
        values["wrapper_exit_code"] = parsed.exit_code
    elif isinstance(parsed, HeartbeatEvent):
        # Use server receipt time, not the client-supplied utc_time. Heartbeat
        # staleness is "how recently did we hear from this wrapper?" — answering
        # that with a clock the wrapper itself controls is unreliable when its
        # clock drifts or events arrive out of order.
        values["last_heartbeat_at"] = now
    elif isinstance(parsed, SlurmStateEvent):
        # `slurm_reason` is reset on every state event (including to NULL when
        # the new event omits it) so the column always reflects the *current*
        # scheduler state, not a stale reason from an earlier transition.
        values["last_known_slurm_state"] = parsed.state
        values["slurm_reason"] = parsed.reason
    # WorkflowOnComplete/OnError/WrapperLog have no summary columns — only the raw row matters.

    if not values:
        return

    await conn.execute(
        update(workflow_runs_tbl)
        .where(workflow_runs_tbl.c.run_name == run_name)
        .values(**values)
    )
