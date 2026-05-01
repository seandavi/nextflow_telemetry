export interface HealthResponse {
  message: string
  status: string
  database: string
}

export interface SampleResponse {
  id: number
  sample_id: string
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
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
  avg_pct_mem: number | null
  p95_pct_mem: number | null
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
  failures: number
}

export interface ProcessFailureSignaturesResponse {
  generated_at_utc: string
  window_days: number | null
  rows: FailureSignatureRow[]
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

export interface WorkflowJobSummary {
  workflow_pk: number
  workflow_id: string
  version: string
  total: number
  pending: number
  claimed: number
  running: number
  completed: number
  failed: number
  dead_letter: number
  completion_pct: number
}
