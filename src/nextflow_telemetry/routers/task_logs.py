"""Task log upload and retrieval — .command.sh, .command.out and .command.err from Nextflow work dirs."""
from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Path, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .. import models
from ..db import task_logs_tbl

# Matches the client's default --max-size-kb (5 MB) so the two gates agree.
# Anything bigger is almost certainly a kraken2 .command.out streaming per-read
# classifications to stdout — that's data, not a log, so we drop it.
_MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB per log file
_VALID_LOG_TYPES = {"command_sh", "command_out", "command_err"}


def create_task_logs_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/task-logs", tags=["task-logs"])

    @router.post(
        "",
        response_model=models.TaskLogEntry,
        status_code=201,
        summary="Upload a task log file",
        description=(
            "Upload the content of a .command.sh, .command.out or .command.err file for a specific "
            "Nextflow task via multipart form data. Identified by (run_name, task_hash, log_type). "
            "Idempotent: re-uploading the same (run_name, task_hash, log_type) "
            "replaces the previous content."
        ),
    )
    async def upload_task_log(
        run_name: Annotated[str, Form()],
        task_hash: Annotated[str, Form()],
        log_type: Annotated[str, Form()],
        content: Annotated[UploadFile, File()],
    ) -> models.TaskLogEntry:
        if log_type not in _VALID_LOG_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"log_type must be one of: {sorted(_VALID_LOG_TYPES)}",
            )
        raw = await content.read()
        if len(raw) > _MAX_CONTENT_BYTES:
            raise HTTPException(status_code=413, detail="Content exceeds 1 MB limit.")
        content_str = raw.decode("utf-8", errors="replace")

        # Normalize to Nextflow's short hash format (ab/cdef12) so it matches
        # the hash stored in telemetry. The afterScript derives the hash from
        # the full work dir path, which gives the complete hex string.
        parts = task_hash.split("/", 1)
        if len(parts) == 2 and len(parts[1]) > 6:
            task_hash = f"{parts[0]}/{parts[1][:6]}"

        now = datetime.datetime.now(datetime.timezone.utc)

        upsert_sql = text(
            """
            insert into task_logs (run_name, task_hash, log_type, content, uploaded_at)
            values (:run_name, :task_hash, :log_type, :content, :uploaded_at)
            on conflict on constraint uq_task_log
            do update set content = excluded.content, uploaded_at = excluded.uploaded_at
            returning id, run_name, task_hash, log_type, content, uploaded_at
            """
        )
        async with engine.begin() as conn:
            row = (await conn.execute(upsert_sql, {
                "run_name": run_name,
                "task_hash": task_hash,
                "log_type": log_type,
                "content": content_str,
                "uploaded_at": now,
            })).mappings().one()

        return models.TaskLogEntry(**dict(row))

    @router.get(
        "/{run_name}/{task_hash:path}",
        response_model=models.TaskLogsResponse,
        summary="Retrieve task logs",
        description=(
            "Returns all uploaded log files (command_sh, command_out and command_err) for a specific "
            "task, identified by run_name and the Nextflow work dir hash (e.g. 'ab/1234ef')."
        ),
    )
    async def get_task_logs(
        run_name: Annotated[str, Path(description="Nextflow run name.")],
        task_hash: Annotated[str, Path(description="Nextflow work dir hash, e.g. 'ab/1234ef'.")],
    ) -> models.TaskLogsResponse:
        select_sql = text(
            """
            select id, run_name, task_hash, log_type, content, uploaded_at
            from task_logs
            where run_name = :run_name and task_hash = :task_hash
            order by log_type
            """
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(select_sql, {
                "run_name": run_name,
                "task_hash": task_hash,
            })).mappings().all()

        return models.TaskLogsResponse(
            run_name=run_name,
            task_hash=task_hash,
            logs=[models.TaskLogEntry(**dict(r)) for r in rows],
        )

    return router
