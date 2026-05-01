"""Pydantic models for the dispatch protocol."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DispatchedJob(BaseModel):
    sample_id: str
    metadata: dict[str, Any] = {}


class DispatchBatchResponse(BaseModel):
    """Full execution context returned by POST /dispatch/batch.

    The server supplies all workflow details so the client config no longer
    needs to carry repository / revision / profile per-workflow.
    """
    run_name: str
    workflow_id: str
    workflow_version: str
    workflow_pk: int
    repository_url: str
    revision: str
    profile: str
    jobs: list[DispatchedJob]


class SubmittedRequest(BaseModel):
    run_name: str
    executor_job_id: str | None = None
    sample_ids: list[str]
