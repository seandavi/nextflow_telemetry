"""Workflow registry service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import workflows_tbl

VALID_STATUSES = {"active", "paused", "retired"}


@dataclass
class WorkflowService:
    engine: AsyncEngine

    async def register(
        self,
        *,
        workflow_id: str,
        version: str,
        repository_url: str,
        revision: str,
        profile: str = "standard",
        manifest_version: str | None = None,
        max_retries: int = 3,
        description: str | None = None,
    ) -> dict:
        """Insert a new workflow or update its mutable fields if already present."""
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            stmt = (
                pg_insert(workflows_tbl)
                .values(
                    workflow_id=workflow_id,
                    version=version,
                    repository_url=repository_url,
                    revision=revision,
                    profile=profile,
                    manifest_version=manifest_version,
                    max_retries=max_retries,
                    status="active",
                    description=description,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_workflow_id_version",
                    set_={
                        "repository_url": repository_url,
                        "revision": revision,
                        "profile": profile,
                        "manifest_version": manifest_version,
                        "max_retries": max_retries,
                        "description": description,
                        "updated_at": now,
                    },
                )
                .returning(*workflows_tbl.c)
            )
            result = await conn.execute(stmt)
            return dict(result.mappings().one())

    async def update_status(self, workflow_pk: int, status: str) -> dict | None:
        """Transition workflow lifecycle: active → paused → retired."""
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(workflows_tbl)
                .where(workflows_tbl.c.id == workflow_pk)
                .values(status=status, updated_at=now)
                .returning(*workflows_tbl.c)
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None

    async def update_revision(self, workflow_pk: int, revision: str) -> dict | None:
        """Update the git revision for a workflow without forcing reruns."""
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(workflows_tbl)
                .where(workflows_tbl.c.id == workflow_pk)
                .values(revision=revision, updated_at=now)
                .returning(*workflows_tbl.c)
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None

    async def list_workflows(self, status: str | None = None) -> list[dict]:
        async with self.engine.connect() as conn:
            q = select(workflows_tbl)
            if status:
                q = q.where(workflows_tbl.c.status == status)
            result = await conn.execute(q.order_by(workflows_tbl.c.workflow_id,
                                                   workflows_tbl.c.version))
            return [dict(r) for r in result.mappings()]

    async def get_by_pk(self, workflow_pk: int) -> dict | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(workflows_tbl).where(workflows_tbl.c.id == workflow_pk)
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None
