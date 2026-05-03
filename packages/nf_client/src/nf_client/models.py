"""Pydantic models for the dispatch protocol."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DispatchedJob(BaseModel):
    sample_id: str
    metadata: dict[str, Any] = {}


class DispatchBatchResponse(BaseModel):
    """Full execution context returned by POST /dispatch/batch.

    The server supplies workflow identity and repository details. The Nextflow
    profile is not included — it is execution-environment-specific and lives
    in the client config (ClientConfig.profile).
    """
    run_name: str
    workflow_id: str
    workflow_version: str
    workflow_pk: int
    repository_url: str
    revision: str
    jobs: list[DispatchedJob]


class SubmittedRequest(BaseModel):
    run_name: str
    executor_job_id: str | None = None
    sample_ids: list[str]
