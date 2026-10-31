from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from .. import models
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
        return asdict(await archive.archive(workflow_pk))

    return router
