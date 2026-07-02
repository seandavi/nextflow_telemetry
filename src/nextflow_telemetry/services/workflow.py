"""Workflow registry service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, workflows_tbl

VALID_STATUSES = {"active", "paused", "retired"}

logger = logging.getLogger(__name__)


@dataclass
class WorkflowService:
    engine: AsyncEngine

    async def _retire_other_active(
        self, conn, workflow_id: str, now, *,
        keep_pk: int | None = None, keep_version: str | None = None,
    ) -> int:
        """Retire every *other* active version of ``workflow_id`` and purge its
        pending jobs, upholding the one-active-version-per-workflow invariant
        (docs/study-sample-version-identity.md). Returns the number retired.

        Called before a version is made active (register / promote) so the
        partial unique index `uq_one_active_version_per_workflow` is never
        violated. Purging pending jobs mirrors the manual-retire rule (#114).
        """
        conds = [
            workflows_tbl.c.workflow_id == workflow_id,
            workflows_tbl.c.status == "active",
        ]
        if keep_pk is not None:
            conds.append(workflows_tbl.c.id != keep_pk)
        if keep_version is not None:
            conds.append(workflows_tbl.c.version != keep_version)
        others = (await conn.execute(
            select(workflows_tbl.c.id).where(*conds)
        )).scalars().all()
        if not others:
            return 0
        await conn.execute(
            delete(jobs_tbl).where(
                jobs_tbl.c.workflow_pk.in_(others),
                jobs_tbl.c.status == "pending",
            )
        )
        await conn.execute(
            update(workflows_tbl)
            .where(workflows_tbl.c.id.in_(others))
            .values(status="retired", updated_at=now)
        )
        logger.info(
            "auto-retired %d prior active version(s) of workflow_id=%s",
            len(others), workflow_id,
        )
        return len(others)

    async def register(
        self,
        *,
        workflow_id: str,
        version: str,
        repository_url: str,
        revision: str,
        manifest_version: str | None = None,
        max_retries: int = 3,
        description: str | None = None,
    ) -> dict:
        """Insert a new workflow or update its mutable fields if already present.

        Registering a version implies it is the one to run, so any *other*
        active version of the same workflow_id is auto-retired first (its pending
        jobs purged) — the release-flow's "retire the prior revision" step is now
        automatic and the one-active-version invariant is upheld.
        """
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            await self._retire_other_active(conn, workflow_id, now, keep_version=version)
            stmt = (
                pg_insert(workflows_tbl)
                .values(
                    workflow_id=workflow_id,
                    version=version,
                    repository_url=repository_url,
                    revision=revision,
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
        """Transition workflow lifecycle: active → paused → retired.

        Retiring is permanent and excludes the workflow from reconciliation and
        dispatch, so its still-`pending` jobs are orphans that would never run —
        they are purged here (the actionable half of #114, so the "298 pending"
        illusion is fixed at the source, not just hidden in stats). In-flight
        jobs (claimed/submitted/running) and completed/failed history are left
        untouched; pausing is reversible and purges nothing.
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            # Promoting to active must first retire any other active version of
            # the same workflow_id, or the partial unique index would reject it.
            if status == "active":
                target = (await conn.execute(
                    select(workflows_tbl.c.workflow_id).where(workflows_tbl.c.id == workflow_pk)
                )).scalar_one_or_none()
                if target is not None:
                    await self._retire_other_active(conn, target, now, keep_pk=workflow_pk)

            result = await conn.execute(
                update(workflows_tbl)
                .where(workflows_tbl.c.id == workflow_pk)
                .values(status=status, updated_at=now)
                .returning(*workflows_tbl.c)
            )
            row = result.mappings().one_or_none()
            if not row:
                return None
            purged = 0
            if status == "retired":
                purge = await conn.execute(
                    delete(jobs_tbl).where(
                        jobs_tbl.c.workflow_pk == workflow_pk,
                        jobs_tbl.c.status == "pending",
                    )
                )
                purged = purge.rowcount or 0
                if purged:
                    logger.info(
                        "retired workflow_pk=%s purged %d pending jobs",
                        workflow_pk, purged,
                    )
            out = dict(row)
            out["purged_pending_jobs"] = purged
            return out

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
