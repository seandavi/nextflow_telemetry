"""Daemon fleet router: heartbeat, list and delete (ADR-0013 #373)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_models import Daemon, DaemonHeartbeatRequest, DeletedDaemon
from ..deps import require_service
from ..services import fleet
from .control_route import ControlRoute, utcnow


def create_daemons_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/daemons", tags=["daemons"], route_class=ControlRoute)

    @router.put(
        "/heartbeat",
        response_model=Daemon,
        dependencies=[Depends(require_service)],
        summary="Daemon heartbeat",
        description=(
            "Upserts the daemon's row. `started_at` resets when the daemon returns after a `stopped` "
            "status or more than 2 minutes of silence."
        ),
    )
    async def heartbeat(req: DaemonHeartbeatRequest):
        async with engine.begin() as conn:
            return await fleet.heartbeat(conn, req, now=utcnow())

    @router.get(
        "",
        response_model=list[Daemon],
        summary="List daemons",
        description=(
            "Newest heartbeat first. Active means seen within 2 minutes and not `stopped`; a silent daemon "
            "reads `stale`. Rows unseen for 7 days are deleted when the list is read."
        ),
    )
    @router.get("/", response_model=list[Daemon], include_in_schema=False)
    async def list_daemons(active_only: bool = False):
        async with engine.begin() as conn:
            return await fleet.list_daemons(conn, active_only=active_only, now=utcnow())

    @router.delete(
        "/{agent_id:path}",
        response_model=DeletedDaemon,
        dependencies=[Depends(require_service)],
        summary="Remove a daemon row",
    )
    async def delete_daemon(agent_id: str):
        async with engine.begin() as conn:
            ok = await fleet.delete_daemon(conn, agent_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return {"deleted": agent_id}

    return router
