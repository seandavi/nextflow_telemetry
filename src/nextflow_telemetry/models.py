from typing import Annotated, Any, Optional
import datetime

from pydantic import BaseModel, ConfigDict, Field


class NextFlowVersion(BaseModel):
    """Nextflow engine version information embedded in weblog metadata."""
    version: str = Field(description="Nextflow version string, e.g. '24.10.0'.")
    build: int = Field(description="Nextflow build number.")
    timestamp: datetime.datetime = Field(description="Build timestamp.")


class Trace(BaseModel):
    """Per-task execution trace included in process-level weblog events."""
    task_id: str = Field(description="Nextflow internal task identifier.")
    hash: str = Field(description="Short hash used in the work directory path, e.g. 'ab/1234ef'.")
    process: str = Field(description="Fully-qualified process name, e.g. 'FETCH_READS' or 'main:MARK_COMPLETE'.")
    name: str = Field(description="Human-readable task name including tag, e.g. 'FETCH_READS (SRR123:run-abc)'.")
    status: str = Field(description="Task exit status: COMPLETED, FAILED, ABORTED, etc.")


class Workflow(BaseModel):
    """Workflow-level metadata included in the 'started' and 'completed' weblog events."""
    project_dir: str = Field(..., alias="projectDir", description="Absolute path to the pipeline directory on the executor.")
    complete: Optional[Any] = None
    profile: Optional[str] = None
    homeDir: Optional[str] = None
    workDir: Optional[str] = None
    container: Optional[Any] = None
    commitId: Optional[str] = None
    errorMessage: Optional[str] = None
    repository: Optional[str] = None
    containerEngine: Optional[str] = None
    scriptFile: Optional[str] = None
    userName: Optional[str] = None
    launchDir: Optional[str] = None
    configFiles: Optional[list[str]] = None
    sessionId: Optional[str] = None
    errorReport: Optional[str] = None
    scriptId: Optional[str] = None
    revision: Optional[str] = None
    commandLine: Optional[str] = None
    nextflow: Optional[NextFlowVersion] = None


class Metadata(BaseModel):
    """Top-level metadata object included in run-level weblog events (started/completed)."""
    params: Optional[dict[str, Any]] = None
    workflow: Optional[Workflow] = None


class Telemetry(BaseModel):
    """A single Nextflow weblog event as posted by the `-with-weblog` reporter.

    Nextflow sends one event per lifecycle transition: one `started` at run
    begin, one `process_submitted/started/completed` per task, and one
    `completed` at run end.  The `metadata` field is only present on run-level
    events; `trace` is only present on process-level events.
    """
    model_config = ConfigDict(populate_by_name=True)

    run_id: Annotated[str, Field(alias="runId", description="UUID assigned by Nextflow to this execution session.")]
    run_name: Annotated[str, Field(alias="runName", description="Human-readable run name; must match the value passed via `-name` to `nextflow run` and stored in `workflow_runs.run_name`.")]
    event: str = Field(description="Event type: `started`, `process_submitted`, `process_started`, `process_completed`, `error`, or `completed`.")
    timestamp: Annotated[datetime.datetime, Field(alias="utcTime", description="UTC timestamp when the event was emitted by Nextflow.")]
    metadata: Optional[Any] = Field(default=None, description="Run-level context (params, workflow info). Present only on `started` and `completed` events.")
    trace: Optional[Any] = Field(default=None, description="Per-task execution details (process name, status, resource usage). Present only on `process_*` events.")


class HealthResponse(BaseModel):
    """Successful health check response."""
    message: str = Field(description="Always 'App Started'.")
    status: str = Field(description="'Healthy' when the database is reachable.")
    database: str = Field(description="'Connected' when the database is reachable.")


class HealthErrorResponse(BaseModel):
    """Health check error wrapper returned with HTTP 503."""
    detail: HealthResponse


# ---------------------------------------------------------------------------
# Process-metrics response models (used by /metrics/processes/* endpoints)
# ---------------------------------------------------------------------------

class ProcessSummaryCards(BaseModel):
    """Aggregate KPIs across all process_completed events in the query window."""
    process_completed_rows: int = Field(description="Total number of process_completed events.")
    distinct_runs: int = Field(description="Number of distinct Nextflow runs.")
    distinct_processes: int = Field(description="Number of distinct process names.")
    success_rows: int = Field(description="Events where the task exited successfully.")
    failure_rows: int = Field(description="Events where the task failed.")
    failure_pct: float = Field(description="Failure rate as a percentage (0–100).")
    retried_rows: int = Field(description="Events that were retried at least once (Nextflow attempt > 1).")
    retry_pct: float = Field(description="Retry rate as a percentage (0–100).")
    retry_success_pct: float = Field(description="Percentage of Nextflow-retried tasks that eventually succeeded.")
    memory_efficiency_pct: float = Field(description="Average memory efficiency (peak_rss / requested_memory × 100). Low values indicate over-provisioned memory requests.")
    latest_process_completed_utc: Optional[datetime.datetime] = Field(description="Timestamp of the most recent process_completed event.")


class EventMixRow(BaseModel):
    """Count of events broken down by event type."""
    event: str = Field(description="Nextflow event type string.")
    rows: int = Field(description="Number of events of this type in the window.")


class TopFailureRow(BaseModel):
    """Failure rate for a single process, ranked by failure count."""
    process: str = Field(description="Fully-qualified Nextflow process name.")
    total_completed: int = Field(description="Total completed events for this process.")
    failed: int = Field(description="Number that failed.")
    failure_pct: float = Field(description="Failure rate as a percentage.")


class TopRetryRow(BaseModel):
    """Retry statistics for a single process."""
    process: str
    total_completed: int
    retried: int
    retried_pct: float
    retried_success: int
    retried_failed: int


class TopFailureExitCodeRow(BaseModel):
    """Exit code frequency across all failed tasks."""
    exit_code: str = Field(description="Process exit code as a string (may be null/empty for signal-killed tasks).")
    failures: int = Field(description="Number of failures with this exit code.")


class ProcessSummaryResponse(BaseModel):
    """Response from GET /metrics/processes/summary."""
    generated_at_utc: datetime.datetime = Field(description="Server time when the query ran.")
    window_days: Optional[int] = Field(description="Query window in days; null means all-time.")
    cards: ProcessSummaryCards
    event_mix: list[EventMixRow]
    top_failures: list[TopFailureRow]
    top_retries: list[TopRetryRow]
    top_failure_exit_codes: list[TopFailureExitCodeRow]


class RetrySummary(BaseModel):
    """Aggregate retry statistics across all processes in the query window."""
    process_completed_rows: int
    retried_rows: int
    retried_pct: float
    retry_success_rows: int
    retry_failure_rows: int
    retry_success_pct: float


class RetryByAttemptRow(BaseModel):
    """Retry outcome broken down by attempt number."""
    attempt: int = Field(description="Attempt number (1 = first try, 2 = first retry, etc.).")
    rows: int
    success: int
    failed: int


class RetryByProcessRow(BaseModel):
    """Retry statistics for a single process."""
    process: str
    total_completed: int
    retried: int
    retried_pct: float
    retried_success: int
    retried_failed: int
    max_attempt: int = Field(description="Highest attempt number observed for this process.")


class ProcessRetriesResponse(BaseModel):
    """Response from GET /metrics/processes/retries."""
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    summary: RetrySummary
    by_attempt: list[RetryByAttemptRow]
    by_process: list[RetryByProcessRow]


class ResourceByAttemptRow(BaseModel):
    """CPU, memory, and I/O statistics for a process broken down by attempt number."""
    process: str
    attempt: int
    rows: int
    success: int
    failed: int
    avg_requested_cpus: Optional[float]
    avg_requested_memory_gb: Optional[float]
    avg_requested_time_min: Optional[float]
    avg_pct_cpu: Optional[float] = Field(description="Average CPU utilisation as a percentage of requested CPUs.")
    p95_pct_cpu: Optional[float] = Field(description="95th-percentile CPU utilisation.")
    avg_pct_mem: Optional[float] = Field(description="Average memory utilisation as a percentage of requested memory.")
    p95_pct_mem: Optional[float]
    avg_peak_rss_gb: Optional[float] = Field(description="Average peak RSS (resident set size) in GB.")
    p95_peak_rss_gb: Optional[float]
    avg_read_gb: Optional[float] = Field(description="Average bytes read from disk, in GB.")
    avg_write_gb: Optional[float] = Field(description="Average bytes written to disk, in GB.")


class ProcessResourcesByAttemptResponse(BaseModel):
    """Response from GET /metrics/processes/resources-by-attempt."""
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[ResourceByAttemptRow]


class ProcessFailuresRow(BaseModel):
    """Success/failure breakdown for a single process."""
    process: str
    total_completed: int
    success: int
    failed: int
    failure_pct: float
    modal_failure_exit_code: Optional[str] = Field(description="Most common exit code among failed tasks for this process.")
    modal_error_action: Optional[str] = Field(default=None, description="Most common error_action (RETRY/FINISH/IGNORE) among failed tasks for this process.")


class ProcessFailuresResponse(BaseModel):
    """Response from GET /metrics/processes/failures."""
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[ProcessFailuresRow]


class FailureSignatureRow(BaseModel):
    """Count of failures grouped by (process, exit_code, error_action) triple."""
    process: str
    exit_code: str
    error_action: Optional[str] = Field(default=None, description="Nextflow error strategy action taken: RETRY, FINISH, or IGNORE.")
    failures: int


class ProcessFailureSignaturesResponse(BaseModel):
    """Response from GET /metrics/processes/failure-signatures."""
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[FailureSignatureRow]


class TimelineRow(BaseModel):
    """Success/failure counts for a single time bucket."""
    bucket_start: datetime.datetime
    total: int
    success: int
    failed: int
    failure_pct: float


class ProcessTimelineResponse(BaseModel):
    """Response from GET /metrics/processes/timeline."""
    generated_at_utc: datetime.datetime
    bucket: str
    rows: list[TimelineRow]


# ---------------------------------------------------------------------------
# Workflow job-summary model
# ---------------------------------------------------------------------------

class RunningProcessRow(BaseModel):
    """In-flight task count for a single process."""
    process: str
    running: int = Field(description="Tasks with process_started but no process_completed.")
    queued: int = Field(description="Tasks with process_submitted but not yet process_started.")


class RunningProcessesResponse(BaseModel):
    """Response from GET /metrics/processes/running."""
    generated_at_utc: datetime.datetime
    active_nf_runs: int = Field(description="Number of workflow_runs currently in 'running' state.")
    total_running: int = Field(description="Total tasks actively executing across all runs.")
    total_queued: int = Field(description="Total tasks submitted to SLURM but not yet started.")
    by_process: list[RunningProcessRow]


class WorkflowJobSummary(BaseModel):
    """Job status breakdown for a single workflow, including dead-letter count.

    Returned by GET /workflows/{workflow_pk}/job-summary.
    """
    workflow_pk: int = Field(description="Database primary key of the workflow.")
    workflow_id: str = Field(description="Logical workflow name.")
    version: str = Field(description="Workflow version string.")
    total: int = Field(description="Total number of jobs for this workflow.")
    pending: int = Field(description="Jobs waiting to be dispatched.")
    claimed: int = Field(description="Jobs claimed by a dispatcher but not yet submitted.")
    running: int = Field(description="Jobs currently executing in Nextflow.")
    completed: int = Field(description="Jobs that completed successfully.")
    failed: int = Field(description="Jobs that exhausted retries and are marked failed.")
    dead_letter: int = Field(description="Jobs that were routed to the dead-letter queue.")
    completion_pct: float = Field(description="Percentage of jobs completed (0–100). Zero when total is 0.")
