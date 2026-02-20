/** Common filter parameters shared across endpoints */
export interface MetricsFilters {
  window_days?: number;
  limit?: number;
  min_samples?: number;
}

/** GET /metrics/processes/summary */
export interface SummaryCards {
  process_completed_rows: number;
  distinct_runs: number;
  distinct_processes: number;
  success_rows: number;
  failure_rows: number;
  failure_pct: number;
  retried_rows: number;
  retry_pct: number;
  retry_success_pct: number;
  latest_process_completed_utc: string;
}

export interface EventMixRow {
  event: string;
  rows: number;
}

export interface TopFailureRow {
  process: string;
  total_completed: number;
  failed: number;
  failure_pct: number;
}

export interface TopRetryRow {
  process: string;
  total_completed: number;
  retried: number;
  retried_pct: number;
  retried_success: number;
  retried_failed: number;
}

export interface TopExitCodeRow {
  exit_code: string;
  failures: number;
}

export interface SummaryResponse {
  generated_at_utc: string;
  window_days: number | null;
  cards: SummaryCards;
  event_mix: EventMixRow[];
  top_failures: TopFailureRow[];
  top_retries: TopRetryRow[];
  top_failure_exit_codes: TopExitCodeRow[];
}

/** GET /metrics/processes/retries */
export interface RetrySummary {
  process_completed_rows: number;
  retried_rows: number;
  retried_pct: number;
  retry_success_rows: number;
  retry_failure_rows: number;
  retry_success_pct: number;
}

export interface RetryByAttemptRow {
  attempt: number;
  rows: number;
  success: number;
  failed: number;
}

export interface RetryByProcessRow {
  process: string;
  total_completed: number;
  retried: number;
  retried_pct: number;
  retried_success: number;
  retried_failed: number;
  max_attempt: number;
}

export interface RetriesResponse {
  generated_at_utc: string;
  window_days: number | null;
  summary: RetrySummary;
  by_attempt: RetryByAttemptRow[];
  by_process: RetryByProcessRow[];
}

/** GET /metrics/processes/resources-by-attempt */
export interface ResourceRow {
  process: string;
  attempt: number;
  rows: number;
  success: number;
  failed: number;
  avg_requested_cpus: number | null;
  avg_requested_memory_gb: number | null;
  avg_requested_time_min: number | null;
  avg_pct_cpu: number | null;
  p95_pct_cpu: number | null;
  avg_pct_mem: number | null;
  p95_pct_mem: number | null;
  avg_peak_rss_gb: number | null;
  p95_peak_rss_gb: number | null;
  avg_read_gb: number | null;
  avg_write_gb: number | null;
}

export interface ResourcesResponse {
  generated_at_utc: string;
  window_days: number | null;
  rows: ResourceRow[];
}

/** GET /metrics/processes/failures */
export interface FailureRow {
  process: string;
  total_completed: number;
  success: number;
  failed: number;
  failure_pct: number;
  modal_failure_exit_code: string | null;
}

export interface FailuresResponse {
  generated_at_utc: string;
  window_days: number | null;
  rows: FailureRow[];
}

/** GET /metrics/processes/failure-signatures */
export interface FailureSignatureRow {
  process: string;
  exit_code: string;
  failures: number;
}

export interface FailureSignaturesResponse {
  generated_at_utc: string;
  window_days: number | null;
  rows: FailureSignatureRow[];
}
