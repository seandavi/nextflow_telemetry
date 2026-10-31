from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from .. import models
from ..config import _as_bool
from ..deps import require_role_or_token
from ..services.retired_archive import RetiredJobArchive


def create_retired_archive_router(archive: RetiredJobArchive) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post(
        "/archive-retired",
        response_model=models.ArchiveRetiredResponse,
        summary="Archive retired versions' jobs to R2 now instead of at the daily 06:00 UTC run",
        dependencies=[Depends(require_role_or_token("admin"))],
    )
    async def archive_retired(workflow_pk: int | None = None) -> dict[str, int]:
        # Read per request, like the archive URL: the route must not write into the
        # archive (v2's live prefix by default) before cutover turns the archive on (#399).
        if not _as_bool(os.environ.get("RETIRED_ARCHIVE_ENABLED", "0")):
            raise HTTPException(503, "retired-job archive is off (RETIRED_ARCHIVE_ENABLED=0)")
        return asdict(await archive.archive(workflow_pk))

    return router
