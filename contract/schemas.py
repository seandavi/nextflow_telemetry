"""Pydantic mirrors of cf/src/schemas.ts responses (#372); change both together."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Iso = str
WorkflowStatus = Literal["active", "paused", "retired"]
SampleKey = Literal["sample_id", "readset_id"]
JobStatus = Literal["pending", "claimed", "submitted", "running", "completed", "failed"]
RunStatus = Literal["claimed", "submitted", "running", "completed", "expired", "failed"]
RunEventType = Literal["wrapper_started", "pre_nextflow", "heartbeat", "wrapper_exited", "slurm_state"]
WorkflowParams = dict[str, str | int | float | bool]
WorkflowCollections = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Envelope(BaseModel):
    """Zod z.object: unknown keys are stripped, not rejected."""

    model_config = ConfigDict(extra="ignore")


class Detail(Envelope):
    """Zod `Detail`."""

    detail: str


class Sample(Loose):
    """Zod `Sample`."""

    id: int
    sample_id: str
    readset_id: str | None
    ncbi_accession: str | None
    biosample_id: str | None
    metadata: dict[str, Any]
    collections: list[str]
    created_at: Iso
    updated_at: Iso


class SampleList(Envelope):
    """Zod `SampleList`."""

    items: list[Sample]
    total: int
    limit: int
    offset: int


class CollectionFacet(Envelope):
    """Inline object in Zod `CollectionFacets.collections`."""

    collection: str
    count: int


class CollectionFacets(Envelope):
    """Zod `CollectionFacets`."""

    total: int
    collections: list[CollectionFacet]


class Workflow(Loose):
    """Zod `Workflow`."""

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
    collections: WorkflowCollections | None
    sample_key: SampleKey
    created_at: Iso
    updated_at: Iso


class JobListItem(Loose):
    """Zod `JobListItem`."""

    job_id: int
    sample_key: str
    status: str
    completed_at: Iso | None
    sample_id: str | None
    readset_id: str | None
    ncbi_accession: str | None
    collections: list[str]


class JobList(Envelope):
    """Zod `JobList`."""

    items: list[JobListItem]
    after: int
    limit: int


class JobSummary(Envelope):
    """Zod `JobSummary`."""

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
    """Zod `ClaimedBatch.jobs` item, without `id` (departure 4)."""

    sample_id: str
    ncbi_accession: str | None
    metadata: dict[str, Any]


class ClaimedBatch(Loose):
    """Zod `ClaimedBatch`."""

    run_name: str
    workflow_pk: int
    workflow_id: str
    workflow_version: str
    repository_url: str
    revision: str
    params: WorkflowParams
    jobs: list[ClaimedJob]


class Run(Loose):
    """Zod `Run`."""

    run_name: str
    run_id: str | None
    workflow_id: str | None
    workflow_version: str | None
    workflow_pk: int | None
    revision: str | None
    status: RunStatus
    executor_job_id: str | None
    claimed_at: Iso | None
    submitted_at: Iso | None
    started_at: Iso | None
    completed_at: Iso | None
    last_heartbeat_at: Iso | None
    wait_seconds: float | None
    wrapper_exit_code: int | None
    last_known_slurm_state: str | None
    slurm_reason: str | None
    nextflow_log_uploaded_at: Iso | None
    classification: str


class RunList(Envelope):
    """Zod `RunList`."""

    total: int
    runs: list[Run]
    limit: int
    offset: int


class FailedTask(Loose):
    """Zod `RunDetail.failed_tasks` item."""

    process: str
    sample_id: str | None
    exit_code: str | None
    task_hash: str | None
    attempt: int | None
    error_action: str | None


class RunDetail(Run):
    """Zod `RunDetail`."""

    job_status_counts: dict[str, int]
    nextflow_log_available: bool
    wrapper_output_log_available: bool
    task_status_counts: dict[str, int]
    failed_tasks: list[FailedTask]


class RunEventAccepted(Envelope):
    """Zod `RunEventAccepted`."""

    run_name: str
    type: RunEventType
    nextflow_log_uploaded: bool
    wrapper_output_log_uploaded: bool


class Cohort(Loose):
    """Zod `Cohort`."""

    collection_id: str
    source: str
    label: str | None
    sample_count: int


class CohortSummary(Loose):
    """Zod `CohortSummary`."""

    collection_id: str
    sample_count: int
    samples_completed: int
    total_jobs: int
    job_status_counts: dict[str, int]
    completion_pct: float
    generated_at_utc: Iso


class Daemon(Loose):
    """Zod `Daemon`."""

    agent_id: str
    hostname: str | None
    workflow_id: str | None
    last_seen_at: Iso | None
    is_active: bool


class Stats(Loose):
    """Zod `Stats`; `storage` is accepted but not required (departure 5)."""

    samples: int
    workflows: int
    jobs_by_status: dict[str, int]
    jobs_by_status_active: dict[str, int]
    runs_by_status: dict[str, int]
    dead_letter_unresolved: int
    storage: dict[str, float] | None = None


class RunningProcess(Loose):
    """Zod `RunningProcesses.by_process` item."""

    process: str


class RunningProcesses(Loose):
    """Zod `RunningProcesses`."""

    generated_at_utc: Iso
    active_nf_runs: int
    total_running: int
    total_queued: int
    by_process: list[RunningProcess]


class TaskLog(Envelope):
    """Zod `TaskLogs.logs` item."""

    id: int
    run_name: str
    task_hash: str
    log_type: str
    content: str
    uploaded_at: Iso


class TaskLogs(Envelope):
    """Zod `TaskLogs`."""

    run_name: str
    task_hash: str
    logs: list[TaskLog]


class TaskLogUploaded(TaskLog):
    """Response of `POST task-logs`: one TaskLog, no Zod schema."""

    model_config = ConfigDict(extra="allow")


class Health(Loose):
    """`GET health`; no Zod schema (frontend `HealthResponse`)."""

    message: str
    status: str
    database: str


class Reconciled(Loose):
    """`POST admin/reconcile-jobs`; no Zod schema."""

    jobs_created: int


class RequeuedDeadLetter(Loose):
    """`POST admin/requeue-dead-letter`; no Zod schema."""

    requeued: int


class Submitted(Loose):
    """`POST dispatch/submitted`; no Zod schema."""

    run_name: str
    status: Literal["submitted"]


class RequeuedExpired(Loose):
    """`POST dispatch/requeue-expired`; no Zod schema."""

    requeued_runs: int


class HeartbeatWatchdog(Loose):
    """`POST admin/heartbeat-watchdog`; no Zod schema (frontend)."""

    stale_runs_failed: int
    jobs_swept: int


class ClosedRun(Loose):
    """`POST admin/close-run`; no Zod schema."""

    run_name: str
    already_closed: bool
    swept: int


class ResetRunning(Loose):
    """`POST admin/reset-running`; no Zod schema."""

    reset: int


class Dispatchability(Loose):
    """`GET admin/dispatchability`; no Zod schema."""

    checked_at: str
    active_daemons: int
    stuck: list[dict[str, Any]]
    stuck_pending_total: int


class DeletedDaemon(Loose):
    """`DELETE daemons/{agent_id}`; no Zod schema."""

    deleted: str
