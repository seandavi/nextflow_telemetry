"""Task log router: .command.* files and run logs, kept in the blob store (ADR-0013 #373)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from obstore.store import ObjectStore

from ..control_models import TaskLog, TaskLogs
from ..services import blobs
from .control_route import ControlRoute, utcnow

# Matches the client's default --max-size-kb (5 MB) so the two gates agree.
TASK_LOG_MAX_BYTES = 5 * 1024 * 1024
VALID_LOG_TYPES = {"command_sh", "command_out", "command_err"}


def short_hash(task_hash: str) -> str:
    """Nextflow's short work-dir hash: two-char dir + the first six characters."""
    parts = task_hash.split("/")[:2]
    return f"{parts[0]}/{parts[1][:6]}" if len(parts) == 2 and len(parts[1]) > 6 else task_hash


def key_id(key: str) -> int:
    """A stable int id from an object key (v1 rows had int ids the dashboard uses as React keys)."""
    h = 0
    for c in key:
        h = (31 * h + ord(c)) & 0xFFFFFFFF
    return abs(h - (1 << 32) if h >= 1 << 31 else h)


def create_task_logs_router(store: ObjectStore) -> APIRouter:
    router = APIRouter(prefix="/task-logs", tags=["task-logs"], route_class=ControlRoute)

    @router.post(
        "",
        response_model=TaskLog,
        status_code=201,
        summary="Upload a task log file",
        description=(
            "Multipart form: `run_name`, `task_hash`, `log_type` (`command_sh`, `command_out` or "
            "`command_err`) and the `content` file. Re-uploading the same key replaces it. Unauthenticated, "
            "as the afterScript carries no token."
        ),
    )
    async def upload_task_log(
        run_name: str = Form(""),
        task_hash: str = Form(""),
        log_type: str = Form(""),
        content: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        # Ad-hoc Nextflow runs stringify params.run_name=null; never file logs under it.
        if not run_name or run_name == "null":
            raise HTTPException(status_code=400, detail="run_name is required")
        if log_type not in VALID_LOG_TYPES:
            raise HTTPException(status_code=422, detail=f"log_type must be one of: {sorted(VALID_LOG_TYPES)}")
        if content is None:
            raise HTTPException(status_code=422, detail="content file is required")
        if (content.size or 0) > TASK_LOG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Content exceeds size limit.")
        text = (await content.read()).decode("utf-8", "replace").replace("\x00", "")
        short = short_hash(task_hash)
        key = blobs.TASK_LOG_KEY.format(run_name=run_name, task_hash=short, log_type=log_type)
        await blobs.put_text(store, key, text.encode())
        return {
            "id": key_id(key),
            "run_name": run_name,
            "task_hash": short,
            "log_type": log_type,
            "content": text,
            "uploaded_at": utcnow(),
        }

    @router.get(
        "/{run_name}/{task_hash:path}",
        response_model=TaskLogs,
        summary="Task logs for one work-dir hash (ab/cdef12 form)",
        description="`nextflow_log` as the hash returns the run's .nextflow.log and wrapper output.",
    )
    async def get_task_logs(run_name: str, task_hash: str) -> dict[str, Any]:
        run_logs = task_hash == "nextflow_log"
        prefix = f"nextflow-logs/{run_name}/" if run_logs else f"task-logs/{run_name}/{task_hash}/"
        logs = []
        for key, uploaded_at in await blobs.list_keys(store, prefix):
            text = await blobs.get_text(store, key)
            if text is None:
                continue
            name = key.rsplit("/", 1)[-1]
            logs.append(
                {
                    "id": key_id(key),
                    "run_name": run_name,
                    "task_hash": task_hash,
                    "log_type": name.removesuffix(".log") + "_log" if run_logs and name.endswith(".log") else name,
                    "content": text,
                    "uploaded_at": uploaded_at,
                }
            )
        return {"run_name": run_name, "task_hash": task_hash, "logs": logs}

    return router
