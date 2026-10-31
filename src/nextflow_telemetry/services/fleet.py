"""The daemon fleet: heartbeat, list (with pruning) and delete (ADR-0013 #373, #209)."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..control_db import daemons_tbl
from ..control_models import DaemonHeartbeatRequest

ACTIVE_WINDOW = timedelta(minutes=2)
PRUNE_AFTER = timedelta(days=7)  # ponytail: fixed; make it a setting if 7 days ever needs changing


def with_activity(row: Mapping[Any, Any], now: datetime) -> dict[str, Any]:
    """Adds ``is_active``; a silent daemon reads ``stale`` (never stored)."""
    stopped = row["status"] == "stopped"
    is_active = not stopped and now - row["last_seen_at"] < ACTIVE_WINDOW
    return {**row, "is_active": is_active, "status": row["status"] if is_active or stopped else "stale"}


async def heartbeat(conn: AsyncConnection, req: DaemonHeartbeatRequest, *, now: datetime) -> dict[str, Any]:
    """Upsert the daemon. ``started_at`` resets when it comes back after a stop or a gap."""
    stmt = pg_insert(daemons_tbl).values(
        agent_id=req.agent_id,
        hostname=req.hostname,
        workflow_id=req.workflow_id,
        profile=req.profile,
        nf_client_version=req.nf_client_version,
        config_yaml=req.config_yaml,
        mode=req.mode,
        batch_size=req.batch_size,
        max_concurrent_runs=req.max_concurrent_runs,
        active_runs=req.active_runs or 0,
        status=req.status,
        last_seen_at=now,
        started_at=now,
    )
    restarted = or_(daemons_tbl.c.status == "stopped", daemons_tbl.c.last_seen_at < now - ACTIVE_WINDOW)
    updated: dict[str, Any] = {
        c: stmt.excluded[c]
        for c in (
            "hostname", "workflow_id", "profile", "nf_client_version", "config_yaml", "mode",
            "batch_size", "max_concurrent_runs", "active_runs", "status", "last_seen_at",
        )
    }
    updated["started_at"] = case((restarted, stmt.excluded.started_at), else_=daemons_tbl.c.started_at)
    row = (
        await conn.execute(stmt.on_conflict_do_update(index_elements=["agent_id"], set_=updated).returning(daemons_tbl))
    ).mappings().one()
    return with_activity(row, now)


async def list_daemons(conn: AsyncConnection, *, active_only: bool, now: datetime) -> list[dict[str, Any]]:
    """Newest first. Rows unseen for ``PRUNE_AFTER`` are deleted first."""
    await conn.execute(delete(daemons_tbl).where(daemons_tbl.c.last_seen_at < now - PRUNE_AFTER))
    rows = (await conn.execute(select(daemons_tbl).order_by(daemons_tbl.c.last_seen_at.desc()))).mappings()
    listed = [with_activity(r, now) for r in rows]
    return [d for d in listed if d["is_active"]] if active_only else listed


async def delete_daemon(conn: AsyncConnection, agent_id: str) -> bool:
    return (await conn.execute(delete(daemons_tbl).where(daemons_tbl.c.agent_id == agent_id))).rowcount > 0
