"""Run router: wrapper and daemon lifecycle events, run list and detail (ADR-0013 #373, #62, #63, #118)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from obstore.store import ObjectStore
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_db import runs_tbl
from ..control_models import RunDetail, RunEventAccepted, RunList
from ..services import blobs, control_reads, transitions
from .control_route import ControlRoute, clamp, utcnow

NEXTFLOW_LOG_MAX = 16 * 1024 * 1024
WRAPPER_LOG_MAX = 4 * 1024 * 1024


def create_runs_router(engine: AsyncEngine, store: ObjectStore) -> APIRouter:
    router = APIRouter(prefix="/runs", tags=["runs"], route_class=ControlRoute)

    @router.post(
        "/{run_name}/event",
        response_model=RunEventAccepted,
        status_code=201,
        summary="Run-lifecycle event from the SLURM wrapper (multipart; unauthenticated)",
        description=(
            "`event` is a JSON object with a `type`: `wrapper_started`, `pre_nextflow`, `heartbeat`, "
            "`slurm_state` or `wrapper_exited`. `nextflow_log` and `wrapper_output_log` may be attached to "
            "`wrapper_exited` only. Any wrapper sign of life pushes the run's liveness deadline out; "
            "`wrapper_exited` closes the run."
        ),
    )
    async def run_event(
        run_name: str,
        event: str = Form(""),
        nextflow_log: UploadFile | None = File(None),
        wrapper_output_log: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(event)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=f"event field is not valid JSON: {err}") from err
        if not isinstance(parsed, dict) or not parsed.get("type"):
            raise HTTPException(status_code=422, detail="event.type is required")
        kind = parsed["type"]

        attached = nextflow_log is not None or wrapper_output_log is not None
        if attached and kind != "wrapper_exited":
            raise HTTPException(
                status_code=422,
                detail=f"log attachments are only valid on wrapper_exited events; received type={kind}.",
            )
        if attached:
            async with engine.connect() as conn:
                known = (await conn.execute(select(runs_tbl.c.run_name).where(runs_tbl.c.run_name == run_name))).first()
            if known is None:
                raise HTTPException(status_code=404, detail=f"No run '{run_name}'; refusing to store attachments as orphan.")
        if nextflow_log is not None and (nextflow_log.size or 0) > NEXTFLOW_LOG_MAX:
            raise HTTPException(status_code=413, detail=f"nextflow_log exceeds {NEXTFLOW_LOG_MAX} byte limit.")
        if wrapper_output_log is not None and (wrapper_output_log.size or 0) > WRAPPER_LOG_MAX:
            raise HTTPException(status_code=413, detail=f"wrapper_output_log exceeds {WRAPPER_LOG_MAX} byte limit.")

        if nextflow_log is not None:
            await blobs.put_text(store, blobs.NEXTFLOW_LOG_KEY.format(run_name=run_name), await nextflow_log.read())
        if wrapper_output_log is not None:
            await blobs.put_text(store, blobs.WRAPPER_LOG_KEY.format(run_name=run_name), await wrapper_output_log.read())

        now = utcnow()
        async with engine.begin() as conn:
            if nextflow_log is not None:
                await transitions.mark_log_uploaded(conn, run_name, now=now)
            await transitions.apply_run_event(conn, run_name, parsed, now=now)
        return {
            "run_name": run_name,
            "type": kind,
            "nextflow_log_uploaded": nextflow_log is not None,
            "wrapper_output_log_uploaded": wrapper_output_log is not None,
        }

    @router.get("", response_model=RunList, summary="List runs, newest claim first")
    async def list_runs(status: str | None = None, workflow_id: str | None = None, limit: int = 50, offset: int = 0):
        limit, offset = clamp(limit, 1, 500), max(0, offset)
        async with engine.connect() as conn:
            runs, total = await control_reads.list_runs(
                conn, status=status, workflow_id=workflow_id, limit=limit, offset=offset
            )
        return {"total": total, "runs": runs, "limit": limit, "offset": offset}

    @router.get(
        "/{run_name}",
        response_model=RunDetail,
        summary="One run with job counts, classification and log availability",
    )
    async def get_run(run_name: str) -> dict[str, Any]:
        async with engine.connect() as conn:
            run = await control_reads.get_run(conn, run_name, utcnow())
        if run is None:
            raise HTTPException(status_code=404, detail=f"No workflow run with name '{run_name}'")
        logged = {key.rsplit("/", 1)[-1] for key, _ in await blobs.list_keys(store, f"nextflow-logs/{run_name}/")}
        return {
            **run,
            "nextflow_log_available": "nextflow.log" in logged,
            "wrapper_output_log_available": "wrapper_output.log" in logged,
        }

    return router
