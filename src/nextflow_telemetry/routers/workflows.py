"""Workflow registry router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from ..services.workflow import WorkflowService


class WorkflowRegisterRequest(BaseModel):
    workflow_id: str
    version: str
    repository_url: str
    revision: str
    profile: str = "standard"
    manifest_version: str | None = None
    max_retries: int = Field(default=3, ge=0, le=10)
    description: str | None = None


class WorkflowStatusRequest(BaseModel):
    status: str  # active | paused | retired


class WorkflowRevisionRequest(BaseModel):
    revision: str


class WorkflowResponse(BaseModel):
    id: int
    workflow_id: str
    version: str
    repository_url: str
    revision: str
    profile: str
    manifest_version: str | None
    max_retries: int
    status: str
    description: str | None
    created_at: Any
    updated_at: Any


def _to_response(row: dict) -> WorkflowResponse:
    return WorkflowResponse(**row)


def create_workflows_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/workflows", tags=["workflows"])
    svc = WorkflowService(engine=engine)

    @router.post("", response_model=WorkflowResponse, status_code=201)
    async def register_workflow(req: WorkflowRegisterRequest):
        """Register a workflow (insert or update mutable fields)."""
        row = await svc.register(
            workflow_id=req.workflow_id,
            version=req.version,
            repository_url=req.repository_url,
            revision=req.revision,
            profile=req.profile,
            manifest_version=req.manifest_version,
            max_retries=req.max_retries,
            description=req.description,
        )
        return _to_response(row)

    @router.get("", response_model=list[WorkflowResponse])
    async def list_workflows(status: str | None = None):
        rows = await svc.list_workflows(status=status)
        return [_to_response(r) for r in rows]

    @router.get("/{workflow_pk}", response_model=WorkflowResponse)
    async def get_workflow(workflow_pk: int):
        row = await svc.get_by_pk(workflow_pk)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    @router.patch("/{workflow_pk}/status", response_model=WorkflowResponse)
    async def update_status(workflow_pk: int, req: WorkflowStatusRequest):
        """Transition workflow lifecycle: active → paused → retired."""
        try:
            row = await svc.update_status(workflow_pk, req.status)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    @router.patch("/{workflow_pk}/revision", response_model=WorkflowResponse)
    async def update_revision(workflow_pk: int, req: WorkflowRevisionRequest):
        """Update the git revision without changing the version or forcing reruns."""
        row = await svc.update_revision(workflow_pk, req.revision)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_pk} not found")
        return _to_response(row)

    return router
