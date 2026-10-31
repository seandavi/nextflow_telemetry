"""Workflow registry router (ADR-0013 #373; params are the output contract, ADR-0004, ADR-0010)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_models import (
    JobList,
    JobSummary,
    RegisterWorkflowRequest,
    Workflow,
    WorkflowRevisionRequest,
    WorkflowStatusRequest,
)
from ..deps import require_service
from ..services import registry
from .control_route import ControlRoute, clamp, utcnow


def _not_found(pk: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Workflow {pk} not found")


def create_workflows_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/workflows", tags=["workflows"], route_class=ControlRoute)

    @router.post(
        "",
        response_model=Workflow,
        status_code=201,
        dependencies=[Depends(require_service)],
        responses={409: {"description": "Registered with different params."}},
        summary="Register (upsert) a workflow version",
        description=(
            "A new version starts `active` and readset-keyed. Re-registering updates the repository, "
            "revision, retries, description and scope, but never `params`: a different `params` object is "
            "409, so register a new version instead (ADR-0004). An omitted `collections` keeps the "
            "existing scope; an explicit `null` widens it to every sample."
        ),
    )
    async def register_workflow(req: RegisterWorkflowRequest):
        async with engine.begin() as conn:
            workflow, conflict = await registry.register_workflow(conn, req, now=utcnow())
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{req.workflow_id} {req.version} is registered with params {json.dumps(workflow['params'])}; "
                    "params are the output contract, so register a new version to change them (ADR-0004)"
                ),
            )
        return workflow

    @router.get("", response_model=list[Workflow], summary="List workflow versions")
    async def list_workflows(status: str | None = None):
        async with engine.connect() as conn:
            return await registry.list_workflows(conn, status)

    @router.get("/{pk:int}", response_model=Workflow, summary="One workflow version by pk")
    async def get_workflow(pk: int):
        async with engine.connect() as conn:
            workflow = await registry.get_workflow(conn, pk)
        if workflow is None:
            raise _not_found(pk)
        return workflow

    @router.patch(
        "/{pk:int}/status",
        response_model=Workflow,
        dependencies=[Depends(require_service)],
        summary="Pause, resume or retire a version; in-flight runs finish",
        description="Retiring deletes the version's pending jobs.",
    )
    async def set_status(pk: int, req: WorkflowStatusRequest):
        async with engine.begin() as conn:
            workflow = await registry.set_workflow_status(conn, pk, req.status, now=utcnow())
        if workflow is None:
            raise _not_found(pk)
        return workflow

    @router.patch(
        "/{pk:int}/revision",
        response_model=Workflow,
        dependencies=[Depends(require_service)],
        summary="Move the git revision without forcing reprocessing (ADR-0004)",
    )
    async def set_revision(pk: int, req: WorkflowRevisionRequest):
        async with engine.begin() as conn:
            workflow = await registry.set_workflow_revision(conn, pk, req.revision, now=utcnow())
        if workflow is None:
            raise _not_found(pk)
        return workflow

    @router.get("/{pk:int}/job-summary", response_model=JobSummary, summary="Job counts by status")
    async def job_summary(pk: int):
        async with engine.connect() as conn:
            summary = await registry.job_summary(conn, pk)
        if summary is None:
            raise _not_found(pk)
        return summary

    @router.get(
        "/{pk:int}/jobs",
        response_model=JobList,
        summary="A registration's jobs in id order, keyset-paged",
        description="`?status=&after=<job_id>&limit=` (limit 1..5000, default 1000).",
    )
    async def list_jobs(pk: int, status: str | None = None, after: int = 0, limit: int = 1000):
        limit, after = clamp(limit, 1, 5000), max(0, after)
        async with engine.connect() as conn:
            if await registry.get_workflow(conn, pk) is None:
                raise _not_found(pk)
            items = await registry.list_jobs(conn, pk, status=status, after=after, limit=limit)
        return {"items": items, "after": after, "limit": limit}

    return router
