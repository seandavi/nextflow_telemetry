export interface HealthResponse {
  message: string
  status: string
  database: string
}

export interface SampleResponse {
  id: number
  sample_id: string
  metadata: Record<string, unknown> | null
  collections: string[]
  created_at: string
  updated_at: string
}

export interface SampleListResponse {
  items: SampleResponse[]
  total: number
  limit: number
  offset: number
}

export interface CollectionFacetsResponse {
  total: number
  collections: Array<{ collection: string; count: number }>
}

export interface WorkflowResponse {
  id: number
  workflow_id: string
  version: string
  repository_url: string
  revision: string
  profile: string
  manifest_version: string | null
  max_retries: number
  status: 'active' | 'paused' | 'retired'
  description: string | null
  created_at: string
  updated_at: string
  job_stats?: JobStats
}

export interface JobStats {
  total: number
  pending: number
  claimed?: number
  submitted?: number
  running: number
  completed: number
  failed: number
  dead_letter?: number
}

export interface WorkflowRegisterRequest {
  workflow_id: string
  version: string
  repository_url: string
  revision: string
  profile: string
  max_retries: number
  description: string
}

export interface ProcessSummaryCards {
  process_completed_rows: number
  distinct_runs: number
  distinct_processes: number
  success_rows: number
  failure_rows: number
  failure_pct: number
  retried_rows: number
  retry_pct: number
  retry_success_pct: number
  memory_efficiency_pct: number
  latest_process_completed_utc: string | null
}

export interface EventMixRow {
  event: string
  rows: number
}

export interface TopFailureRow {
  process: string
  total_completed: number
  failed: number
  failure_pct: number
}

export interface TopRetryRow {
  process: string
  total_completed: number
  retried: number
  retried_pct: number
  retried_success: number
  retried_failed: number
}

export interface TopFailureExitCodeRow {
  exit_code: string
  failures: number
}

export interface ProcessSummaryResponse {
  generated_at_utc: string
  window_days: number | null
  cards: ProcessSummaryCards
  event_mix: EventMixRow[]
  top_failures: TopFailureRow[]
  top_retries: TopRetryRow[]
  top_failure_exit_codes: TopFailureExitCodeRow[]
}

export interface ProcessFailuresRow {
  process: string
  total_completed: number
  success: number
  failed: number
  failure_pct: number
  modal_failure_exit_code: string | null
  modal_error_action: string | null
}

export interface ProcessFailuresResponse {
  generated_at_utc: string
  window_days: number | null
  rows: ProcessFailuresRow[]
}

export interface RetrySummary {
  process_completed_rows: number
  retried_rows: number
  retried_pct: number
  retry_success_rows: number
  retry_failure_rows: number
  retry_success_pct: number
}

export interface RetryByAttemptRow {
  attempt: number
  rows: number
  success: number
  failed: number
}

export interface RetryByProcessRow {
  process: string
  total_completed: number
  retried: number
  retried_pct: number
  retried_success: number
  retried_failed: number
  max_attempt: number
}

export interface ProcessRetriesResponse {
  generated_at_utc: string
  window_days: number | null
  summary: RetrySummary
  by_attempt: RetryByAttemptRow[]
  by_process: RetryByProcessRow[]
}

export interface ResourceByAttemptRow {
  process: string
  attempt: number
  rows: number
  success: number
  failed: number
  avg_requested_cpus: number | null
  avg_requested_memory_gb: number | null
  avg_requested_time_min: number | null
  avg_pct_cpu: number | null
  p95_pct_cpu: number | null
  avg_cpu_efficiency_pct: number | null
  avg_pct_mem: number | null
  p95_pct_mem: number | null
  avg_memory_efficiency_pct: number | null
  avg_peak_rss_gb: number | null
  p95_peak_rss_gb: number | null
  avg_read_gb: number | null
  avg_write_gb: number | null
}

export interface ProcessResourcesByAttemptResponse {
  generated_at_utc: string
  window_days: number | null
  rows: ResourceByAttemptRow[]
}

export interface FailureSignatureRow {
  process: string
  exit_code: string
  error_action: string | null
  failures: number
}

export interface ProcessFailureSignaturesResponse {
  generated_at_utc: string
  window_days: number | null
  rows: FailureSignatureRow[]
}

export interface TimelineRow {
  bucket_start: string
  total: number
  success: number
  failed: number
  failure_pct: number
}

export interface ProcessTimelineResponse {
  generated_at_utc: string
  window_days: number | null
  bucket: string
  rows: TimelineRow[]
}

export interface RunningProcessRow {
  process: string
  running: number
  queued: number
}

export interface RunningProcessesResponse {
  generated_at_utc: string
  active_nf_runs: number
  total_running: number
  total_queued: number
  by_process: RunningProcessRow[]
}

export interface DispatchedJob {
  sample_id: string
}

export interface DispatchBatchResponse {
  run_name: string
  workflow_id: string
  workflow_version: string
  workflow_pk: number
  repository_url: string
  revision: string
  profile: string
  jobs: DispatchedJob[]
}

export interface JobTotals extends JobStats {
  sparkline: number[]
}

export interface SampleRegisterRequest {
  sample_id: string
  metadata?: Record<string, unknown>
  collection?: string
}

export interface SubmittedRequest {
  run_name: string
  executor_job_id?: string
}

export interface ReconcileResult {
  jobs_created: number
}

export interface RequeueResult {
  requeued_runs: number
}

export interface RequeueDlqResult {
  requeued: number
}

export interface WatchdogResult {
  checked_at: string
  stale_after_minutes: number
  stale_runs_failed: number
  jobs_swept: number
  runs: Array<{ run_name: string; last_signal_at: string | null; jobs_swept: number }>
}

export interface RunListItem {
  run_name: string
  workflow_id: string
  workflow_version: string
  status: string
  classification: string
  claimed_at: string | null
  submitted_at: string | null
  started_at: string | null
  completed_at: string | null
  wrapper_exit_code: number | null
  last_known_slurm_state: string | null
  slurm_reason: string | null
}

export interface RunsListResponse {
  total: number
  limit: number
  offset: number
  runs: RunListItem[]
}

export interface FailedTask {
  process: string
  sample_id: string | null
  exit_code: string | null
  task_hash: string | null
  attempt: number
  error_action: string | null
}

export interface RunDetail extends RunListItem {
  run_id: string | null
  revision: string | null
  executor_job_id: string | null
  last_heartbeat_at: string | null
  nextflow_log_uploaded_at: string | null
  wait_seconds: number | null
  task_status_counts: Record<string, number>
  job_status_counts?: Record<string, number>
  failed_tasks?: FailedTask[]
  nextflow_log_available: boolean
  wrapper_output_log_available: boolean
}

export interface StuckWorkflow {
  workflow_id: string
  workflow_pk: number
  pending: number
  reason: string
}

export interface DispatchabilityResult {
  checked_at: string
  active_daemons: number
  stuck: StuckWorkflow[]
  stuck_pending_total: number
}

export interface TaskLogEntry {
  id: number
  run_name: string
  task_hash: string
  log_type: string
  content: string
  uploaded_at: string
}

export interface TaskLogsResponse {
  run_name: string
  task_hash: string
  logs: TaskLogEntry[]
}

export interface TaskRow {
  telemetry_id: number
  run_name: string
  run_id: string | null
  sample_id: string | null
  workflow_id: string | null
  workflow_version: string | null
  utc_time: string
  process: string
  name: string | null
  status: string
  attempt: number
  task_hash: string | null
  exit_code: string | null
  error_action: string | null
  realtime_ms: number | null
  requested_cpus: number | null
  requested_memory_gb: number | null
  pct_cpu: number | null
  pct_mem: number | null
  peak_rss_gb: number | null
  read_gb: number | null
  write_gb: number | null
}

export interface TasksResponse {
  generated_at_utc: string
  window_days: number | null
  total: number
  limit: number
  offset: number
  rows: TaskRow[]
}

export interface WorkflowJobSummary {
  workflow_pk: number
  workflow_id: string
  version: string
  total: number
  pending: number
  claimed: number
  submitted: number
  running: number
  completed: number
  failed: number
  dead_letter: number
  completion_pct: number
}

export interface DaemonAgentResponse {
  agent_id: string
  hostname: string
  workflow_id: string | null
  profile: string | null
  nf_client_version: string | null
  config_yaml: string | null
  mode: string
  batch_size: number
  max_concurrent_runs: number | null
  active_runs: number
  status: 'idle' | 'running'
  last_seen_at: string
  started_at: string
  is_active: boolean
}

// ---------------------------------------------------------------------------
// Cohorts (issue #36)
// ---------------------------------------------------------------------------

export interface CohortListItem {
  collection_id: string
  source: string
  label: string | null
  sample_count: number
  created_at: string
  updated_at: string
}

export interface CohortJobStatusCounts {
  pending: number
  claimed: number
  submitted: number
  running: number
  completed: number
  failed: number
}

export interface CohortFailureByProcessRow {
  process: string
  failed_count: number
  sample_count: number
}

export interface CohortSummaryResponse {
  collection_id: string
  source: string
  label: string | null
  workflow_id: string | null
  workflow_version: string | null
  sample_count: number
  samples_completed: number
  total_jobs: number
  job_status_counts: CohortJobStatusCounts
  completion_pct: number
  failure_by_process: CohortFailureByProcessRow[]
  generated_at_utc: string
}

export interface CohortLeaderboardRow {
  collection_id: string
  source: string
  label: string | null
  sample_count: number
  samples_completed: number
  samples_failed: number
  samples_running: number
  samples_remaining: number
  completion_pct: number
  last_completed_at: string | null
}

export interface CohortFailureRow {
  telemetry_id: number
  sample_id: string | null
  run_name: string
  utc_time: string
  task_name: string | null
  task_hash: string | null
  status: string
  exit_code: string | null
  attempt: number
}

export interface CohortFailuresResponse {
  collection_id: string
  process: string
  rows: CohortFailureRow[]
}
