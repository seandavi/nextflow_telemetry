"""Pydantic models for the dispatch protocol."""
from __future__ import annotations

from pydantic import BaseModel


class DispatchedJob(BaseModel):
    sample_id: str
    workflow_id: str
    workflow_version: str


class DispatchBatchResponse(BaseModel):
    run_name: str
    jobs: list[DispatchedJob]


class SubmittedRequest(BaseModel):
    run_name: str
    executor_job_id: str | None = None
    sample_ids: list[str]
