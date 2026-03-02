from pydantic import BaseModel, Field
from typing import Any, List, Optional
import datetime
from enum import Enum


class NextFlowVersion(BaseModel):
    """Nextflow version model"""

    version: str
    build: int
    timestamp: datetime.datetime


class Trace(BaseModel):
    """Trace model"""

    task_id: str
    hash: str
    process: str
    name: str
    status: str


class Workflow(BaseModel):
    # start: datetime.datetime
    project_dir: str = Field(..., alias="projectDir")
    complete: Optional[Any]
    profile: Optional[str]
    homeDir: Optional[str]
    workDir: Optional[str]
    container: Optional[Any]
    commitId: Optional[str]
    errorMessage: Optional[str]
    repository: Optional[str]
    containerEngine: Optional[str]
    scriptFile: Optional[str]
    userName: Optional[str]
    launchDir: Optional[str]
    configFiles: Optional[list[str]]
    sessionId: Optional[str]
    errorReport: Optional[str]
    scriptId: Optional[str]
    revision: Optional[str]
    commandLine: Optional[str]
    nextflow: Optional[NextFlowVersion]


class Metadata(BaseModel):
    """Metadata model"""

    params: Optional[dict[str, Any]] = None
    workflow: Optional[Workflow]


class Telemetry(BaseModel):
    """Telemetry model"""

    run_id: str = Field(..., alias="runId")
    run_name: str = Field(..., alias="runName")
    event: str
    timestamp: datetime.datetime = Field(..., alias="utcTime")
    metadata: Optional[Any] # Optional[Metadata]
    trace: Optional[Any] # Optional[Trace]


class HealthResponse(BaseModel):
    message: str
    status: str
    database: str


class HealthErrorResponse(BaseModel):
    detail: HealthResponse


class ProcessSummaryCards(BaseModel):
    process_completed_rows: int
    distinct_runs: int
    distinct_processes: int
    success_rows: int
    failure_rows: int
    failure_pct: float
    retried_rows: int
    retry_pct: float
    retry_success_pct: float
    latest_process_completed_utc: Optional[datetime.datetime]


class EventMixRow(BaseModel):
    event: str
    rows: int


class TopFailureRow(BaseModel):
    process: str
    total_completed: int
    failed: int
    failure_pct: float


class TopRetryRow(BaseModel):
    process: str
    total_completed: int
    retried: int
    retried_pct: float
    retried_success: int
    retried_failed: int


class TopFailureExitCodeRow(BaseModel):
    exit_code: str
    failures: int


class ProcessSummaryResponse(BaseModel):
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    cards: ProcessSummaryCards
    event_mix: list[EventMixRow]
    top_failures: list[TopFailureRow]
    top_retries: list[TopRetryRow]
    top_failure_exit_codes: list[TopFailureExitCodeRow]


class RetrySummary(BaseModel):
    process_completed_rows: int
    retried_rows: int
    retried_pct: float
    retry_success_rows: int
    retry_failure_rows: int
    retry_success_pct: float


class RetryByAttemptRow(BaseModel):
    attempt: int
    rows: int
    success: int
    failed: int


class RetryByProcessRow(BaseModel):
    process: str
    total_completed: int
    retried: int
    retried_pct: float
    retried_success: int
    retried_failed: int
    max_attempt: int


class ProcessRetriesResponse(BaseModel):
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    summary: RetrySummary
    by_attempt: list[RetryByAttemptRow]
    by_process: list[RetryByProcessRow]


class ResourceByAttemptRow(BaseModel):
    process: str
    attempt: int
    rows: int
    success: int
    failed: int
    avg_requested_cpus: Optional[float]
    avg_requested_memory_gb: Optional[float]
    avg_requested_time_min: Optional[float]
    avg_pct_cpu: Optional[float]
    p95_pct_cpu: Optional[float]
    avg_pct_mem: Optional[float]
    p95_pct_mem: Optional[float]
    avg_peak_rss_gb: Optional[float]
    p95_peak_rss_gb: Optional[float]
    avg_read_gb: Optional[float]
    avg_write_gb: Optional[float]


class ProcessResourcesByAttemptResponse(BaseModel):
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[ResourceByAttemptRow]


class ProcessFailuresRow(BaseModel):
    process: str
    total_completed: int
    success: int
    failed: int
    failure_pct: float
    modal_failure_exit_code: Optional[str]


class ProcessFailuresResponse(BaseModel):
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[ProcessFailuresRow]


class FailureSignatureRow(BaseModel):
    process: str
    exit_code: str
    failures: int


class ProcessFailureSignaturesResponse(BaseModel):
    generated_at_utc: datetime.datetime
    window_days: Optional[int]
    rows: list[FailureSignatureRow]


# --- Samples ---

class SampleCreate(BaseModel):
    sample_id: str
    srr_accessions: Optional[List[str]] = None
    metadata_: Optional[dict[str, Any]] = None

    class Config:
        fields = {"metadata_": {"alias": "metadata"}}


class SampleUpdate(BaseModel):
    srr_accessions: Optional[List[str]] = None
    metadata_: Optional[dict[str, Any]] = None

    class Config:
        fields = {"metadata_": {"alias": "metadata"}}


class SampleResponse(BaseModel):
    id: int
    sample_id: str
    srr_accessions: Optional[List[str]] = None
    metadata_: Optional[dict[str, Any]] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    class Config:
        fields = {"metadata_": {"alias": "metadata"}}


class SampleListResponse(BaseModel):
    items: List[SampleResponse]
    total: int


# --- Pipelines ---

class PipelineCreate(BaseModel):
    pipeline_id: str
    repository: Optional[str] = None
    branch: Optional[str] = "main"
    description: Optional[str] = None
    default_params: Optional[dict[str, Any]] = None


class PipelineUpdate(BaseModel):
    repository: Optional[str] = None
    branch: Optional[str] = None
    description: Optional[str] = None
    default_params: Optional[dict[str, Any]] = None


class PipelineResponse(BaseModel):
    id: int
    pipeline_id: str
    repository: Optional[str] = None
    branch: Optional[str] = None
    description: Optional[str] = None
    default_params: Optional[dict[str, Any]] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class PipelineListResponse(BaseModel):
    items: List[PipelineResponse]
    total: int


# --- Jobs ---

class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failure = "failure"


class JobCreate(BaseModel):
    sample_id: str
    pipeline_id: str


class JobStatusUpdate(BaseModel):
    status: JobStatus


class JobResponse(BaseModel):
    id: int
    sample_id: str
    pipeline_id: str
    status: str
    submitted_at: datetime.datetime
    updated_at: datetime.datetime


class JobListResponse(BaseModel):
    items: List[JobResponse]
    total: int
