"""Request and response models for the control-plane routes (ADR-0013 #373).

The field sets match ``contract/schemas.py``, which mirrors ``cf/src/schemas.ts``. Response
rows allow extra keys so every selected column reaches the wire, as v2's ``select *`` did.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .readset import normalize_runs

WorkflowStatus = Literal["active", "paused", "retired"]
WorkflowParams = dict[str, str | bool | int | float]


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Ignoring(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- requests


class ClaimRequest(Ignoring):
    limit: int = 50
    workflow_id: str | list[str] | None = None
    workflow_version: str | None = None


class SubmittedRequest(Ignoring):
    run_name: str = ""
    executor_job_id: str | None = None
    sample_ids: list[str] = []


class SampleRegistration(BaseModel):
    sample_id: str = Field(min_length=1)
    ncbi_accession: str = Field(min_length=1)
    biosample_id: str | None = None
    metadata: dict[str, Any] | None = None
    collection: str | None = None

    @field_validator("ncbi_accession")
    @classmethod
    def _has_runs(cls, value: str) -> str:
        if not normalize_runs(value):
            raise ValueError("no accessions given")
        return value


class RegisterWorkflowRequest(BaseModel):
    workflow_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    repository_url: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    manifest_version: str | None = None
    max_retries: int | None = Field(default=None, ge=0)
    status: WorkflowStatus | None = None
    description: str | None = None
    params: WorkflowParams | None = None
    collections: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, min_length=1)


class WorkflowStatusRequest(BaseModel):
    status: WorkflowStatus


class WorkflowRevisionRequest(BaseModel):
    revision: str = Field(min_length=1)


class DaemonHeartbeatRequest(Ignoring):
    agent_id: str
    hostname: str
    workflow_id: str | None = None
    profile: str | None = None
    nf_client_version: str | None = None
    config_yaml: str | None = None
    mode: str | None = None
    batch_size: int | None = None
    max_concurrent_runs: int | None = None
    active_runs: int | None = None
    status: Literal["idle", "running", "stopped"] = "idle"


class CloseRunRequest(BaseModel):
    run_name: str | None = None


class RequeueDeadLetterRequest(BaseModel):
    workflow_pk: int | None = None
    collection: str | None = None
    job_ids: list[int] | None = None


class JobPriorityRequest(BaseModel):
    collection_id: str = Field(min_length=1)
    priority: Annotated[StrictInt, Field(ge=-(2**31), le=2**31 - 1)]
    workflow_pk: StrictInt | None = None


# --------------------------------------------------------------- responses


class Sample(Loose):
    id: int
    sample_id: str
    readset_id: str | None
    ncbi_accession: str | None
    biosample_id: str | None
    metadata: dict[str, Any]
    collections: list[str]
    created_at: datetime
    updated_at: datetime


class SampleList(BaseModel):
    items: list[Sample]
    total: int
    limit: int
    offset: int


class CollectionFacet(BaseModel):
    collection: str
    count: int


class CollectionFacets(BaseModel):
    total: int
    collections: list[CollectionFacet]


class Workflow(Loose):
    id: int
    workflow_id: str
    version: str
    repository_url: str
    revision: str
    manifest_version: str | None
    max_retries: int
    status: WorkflowStatus
    description: str | None
    params: WorkflowParams
    collections: list[str] | None
    sample_key: Literal["sample_id", "readset_id"]
    created_at: datetime
    updated_at: datetime


class JobListItem(Loose):
    job_id: int
    sample_key: str | None
    status: str
    completed_at: datetime | None
    sample_id: str | None
    readset_id: str | None
    ncbi_accession: str | None
    collections: list[str]


class JobList(BaseModel):
    items: list[JobListItem]
    after: int
    limit: int


class JobSummary(BaseModel):
    workflow_pk: int
    workflow_id: str
    version: str
    total: int
    pending: int
    claimed: int
    submitted: int
    running: int
    completed: int
    failed: int
    dead_letter: int
    completion_pct: float


class ClaimedJob(Loose):
    sample_id: str
    ncbi_accession: str | None
    metadata: dict[str, Any]


class ClaimedBatch(Loose):
    run_name: str
    workflow_pk: int
    workflow_id: str
    workflow_version: str
    repository_url: str
    revision: str
    params: WorkflowParams
    jobs: list[ClaimedJob]


class Run(Loose):
    run_name: str
    run_id: str | None
    workflow_id: str | None
    workflow_version: str | None
    workflow_pk: int | None
    revision: str | None
    status: str
    executor_job_id: str | None
    claimed_at: datetime | None
    submitted_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    last_heartbeat_at: datetime | None
    wait_seconds: float | None
    wrapper_exit_code: int | None
    last_known_slurm_state: str | None
    slurm_reason: str | None
    nextflow_log_uploaded_at: datetime | None
    classification: str


class RunList(BaseModel):
    total: int
    runs: list[Run]
    limit: int
    offset: int


class FailedTask(Loose):
    process: str
    sample_id: str | None
    exit_code: str | None
    task_hash: str | None
    attempt: int | None
    error_action: str | None


class RunDetail(Run):
    job_status_counts: dict[str, int]
    nextflow_log_available: bool
    wrapper_output_log_available: bool
    task_status_counts: dict[str, int]
    failed_tasks: list[FailedTask]


class RunEventAccepted(BaseModel):
    run_name: str
    type: str
    nextflow_log_uploaded: bool
    wrapper_output_log_uploaded: bool


class Daemon(Loose):
    agent_id: str
    hostname: str | None
    workflow_id: str | None
    last_seen_at: datetime | None
    is_active: bool


class Stats(Loose):
    samples: int
    workflows: int
    jobs_by_status: dict[str, int]
    jobs_by_status_active: dict[str, int]
    runs_by_status: dict[str, int]
    dead_letter_unresolved: int


class TaskLog(BaseModel):
    id: int
    run_name: str
    task_hash: str
    log_type: str
    content: str
    uploaded_at: datetime


class TaskLogs(BaseModel):
    run_name: str
    task_hash: str
    logs: list[TaskLog]


class Reconciled(BaseModel):
    jobs_created: int


class RequeuedDeadLetter(BaseModel):
    requeued: int


class Submitted(BaseModel):
    run_name: str
    status: Literal["submitted"]


class RequeuedExpired(BaseModel):
    requeued_runs: int


class HeartbeatWatchdog(BaseModel):
    checked_at: datetime
    stale_after_minutes: int
    stale_runs_failed: int
    jobs_swept: int
    runs: list[Any]


class ExpiredStaleRuns(BaseModel):
    stale_runs_closed: int
    jobs_swept: int


class ClosedRun(BaseModel):
    run_name: str
    already_closed: bool
    swept: int


class ResetRunning(BaseModel):
    reset: int


class JobPriorityUpdated(BaseModel):
    updated: int


class StuckWorkflow(BaseModel):
    workflow_pk: int
    workflow_id: str
    pending: int
    reason: str


class Dispatchability(BaseModel):
    checked_at: datetime
    active_daemons: int
    stuck: list[StuckWorkflow]
    stuck_pending_total: int


class DeletedDaemon(BaseModel):
    deleted: str


class BulkRegistered(BaseModel):
    received: int
    samples_inserted: int
    samples_updated: int
    collections_created: int
    memberships_added: int
    readset_runs_added: int


class DeadLetterItem(BaseModel):
    id: int
    job_id: int
    workflow_pk: int
    workflow_id: str
    workflow_version: str
    sample_id: str
    readset_id: str | None
    collections: list[str]
    run_name: str | None
    reason: str
    created_at: datetime
    resolved_at: datetime | None
    job_status: str
    retry_count: int


class DeadLetterList(BaseModel):
    items: list[DeadLetterItem]
    total: int
    limit: int
    offset: int
