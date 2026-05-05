"""Daemons router — heartbeat registration and fleet listing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import daemon_agents_tbl
from ..models import DaemonAgentResponse, DaemonHeartbeat

ACTIVE_THRESHOLD = timedelta(minutes=2)


def _row_to_response(row: dict) -> DaemonAgentResponse:
    now = datetime.now(tz=timezone.utc)
    last_seen = row["last_seen_at"]
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return DaemonAgentResponse(
        **{k: v for k, v in row.items()},
        is_active=(now - last_seen) < ACTIVE_THRESHOLD,
    )


def create_daemons_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/daemons", tags=["daemons"])

    @router.put(
        "/heartbeat",
        response_model=DaemonAgentResponse,
        summary="Register or refresh a daemon agent heartbeat",
        description=(
            "Upserted by nf-client on every poll cycle. Creates the agent record on first call; "
            "updates `last_seen_at`, `active_runs`, and `status` on subsequent calls. "
            "`started_at` is only set on insert — it is not overwritten on update."
        ),
    )
    async def heartbeat(body: DaemonHeartbeat) -> DaemonAgentResponse:
        now = datetime.now(tz=timezone.utc)
        values = {
            "agent_id": body.agent_id,
            "hostname": body.hostname,
            "workflow_id": body.workflow_id,
            "profile": body.profile,
            "nf_client_version": body.nf_client_version,
            "config_yaml": body.config_yaml,
            "mode": body.mode,
            "batch_size": body.batch_size,
            "max_concurrent_runs": body.max_concurrent_runs,
            "active_runs": body.active_runs,
            "status": body.status,
            "last_seen_at": now,
            "started_at": now,
        }
        stmt = (
            insert(daemon_agents_tbl)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["agent_id"],
                set_={
                    "hostname": body.hostname,
                    "workflow_id": body.workflow_id,
                    "profile": body.profile,
                    "nf_client_version": body.nf_client_version,
                    "config_yaml": body.config_yaml,
                    "mode": body.mode,
                    "batch_size": body.batch_size,
                    "max_concurrent_runs": body.max_concurrent_runs,
                    "active_runs": body.active_runs,
                    "status": body.status,
                    "last_seen_at": now,
                    # started_at intentionally NOT updated — preserves original start time
                },
            )
            .returning(daemon_agents_tbl)
        )
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.mappings().one()
        return _row_to_response(dict(row))

    @router.get(
        "/",
        response_model=list[DaemonAgentResponse],
        summary="List registered daemon agents",
        description=(
            "Returns all known daemon agents ordered by `last_seen_at` descending. "
            "Pass `?active_only=true` to filter to agents seen within the last 2 minutes."
        ),
    )
    async def list_daemons(active_only: bool = False) -> list[DaemonAgentResponse]:
        stmt = select(daemon_agents_tbl).order_by(daemon_agents_tbl.c.last_seen_at.desc())
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        agents = [_row_to_response(dict(r)) for r in rows]
        if active_only:
            agents = [a for a in agents if a.is_active]
        return agents

    return router
