import type {
  HealthResponse,
  WorkflowResponse,
  WorkflowRegisterRequest,
  WorkflowJobSummary,
  ProcessSummaryResponse,
  ProcessFailuresResponse,
  ProcessRetriesResponse,
  ProcessResourcesByAttemptResponse,
  ProcessFailureSignaturesResponse,
  ProcessTimelineResponse,
  TasksResponse,
  TaskLogsResponse,
  RunningProcessesResponse,
  DispatchBatchResponse,
  ReconcileResult,
  RequeueResult,
  SampleResponse,
  SampleRegisterRequest,
  SubmittedRequest,
  DaemonAgentResponse,
  CohortListItem,
  CohortSummaryResponse,
  CohortFailuresResponse,
} from '../types'

export interface MetricsFilters {
  workflowId?: string
  workflowVersion?: string
  windowHours?: number
  windowDays?: number
  since?: string
  until?: string
  runName?: string
  sampleId?: string
}

function metricsParams(f: MetricsFilters, extra?: Record<string, string | number>): string {
  const p = new URLSearchParams()
  if (f.workflowId)      p.set('workflow_id',      f.workflowId)
  if (f.workflowVersion) p.set('workflow_version',  f.workflowVersion)
  if (f.windowHours)     p.set('window_hours',      String(f.windowHours))
  else if (f.windowDays) p.set('window_days',       String(f.windowDays))
  if (f.since)           p.set('since',             f.since)
  if (f.until)           p.set('until',             f.until)
  if (f.runName)         p.set('run_name',          f.runName)
  if (f.sampleId)        p.set('sample_id',         f.sampleId)
  if (extra) Object.entries(extra).forEach(([k, v]) => p.set(k, String(v)))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const API_BASE = (import.meta.env.VITE_API_URL ?? '') + '/api'
const BASE = API_BASE

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`)
  return res.json() as Promise<T>
}

const get  = <T>(path: string) => req<T>('GET', path)
const post = <T>(path: string, body?: unknown) => req<T>('POST', path, body)
const patch = <T>(path: string, body?: unknown) => req<T>('PATCH', path, body)

export const api = {
  health: () => get<HealthResponse>('/health'),

  workflows: {
    list:       ()                           => get<WorkflowResponse[]>('/workflows'),
    create:     (body: WorkflowRegisterRequest) => post<WorkflowResponse>('/workflows', body),
    setStatus:  (id: number, status: string) =>
      patch<WorkflowResponse>(`/workflows/${id}/status`, { status }),
    jobSummary: (workflowPk: number)         => get<WorkflowJobSummary>(`/workflows/${workflowPk}/job-summary`),
  },

  samples: {
    list:   (page: number, size: number, search?: string, cohort?: string) => {
      const params = new URLSearchParams({ skip: String(page * size), limit: String(size) })
      if (search) params.set('search', search)
      if (cohort) params.set('cohort', cohort)
      return get<SampleResponse[]>(`/samples?${params}`)
    },
    create: (body: SampleRegisterRequest) => post<SampleResponse>('/samples', body),
  },

  metrics: {
    running:    () => get<RunningProcessesResponse>('/metrics/processes/running'),
    summary:    (f: MetricsFilters = {}) => get<ProcessSummaryResponse>(`/metrics/processes/summary${metricsParams(f)}`),
    failures:   (f: MetricsFilters = {}) => get<ProcessFailuresResponse>(`/metrics/processes/failures${metricsParams(f)}`),
    retries:    (f: MetricsFilters = {}) => get<ProcessRetriesResponse>(`/metrics/processes/retries${metricsParams(f)}`),
    resources:  (f: MetricsFilters = {}) => get<ProcessResourcesByAttemptResponse>(`/metrics/processes/resources-by-attempt${metricsParams(f)}`),
    signatures: (f: MetricsFilters = {}) => get<ProcessFailureSignaturesResponse>(`/metrics/processes/failure-signatures${metricsParams(f)}`),
    timeline:   (f: MetricsFilters = {}, bucket: 'hour' | 'day' | 'week' = 'hour') =>
      get<ProcessTimelineResponse>(`/metrics/processes/timeline${metricsParams(f, { bucket })}`),
    taskLogs: (runName: string, taskHash: string) =>
      get<TaskLogsResponse>(`/task-logs/${encodeURIComponent(runName)}/${taskHash}`),
    tasks: (f: MetricsFilters = {}, extra: { process?: string; status?: string; limit?: number; offset?: number } = {}) => {
      const e: Record<string, string | number> = {}
      if (extra.process) e['process'] = extra.process
      if (extra.status)  e['status']  = extra.status
      if (extra.limit  != null) e['limit']  = extra.limit
      if (extra.offset != null) e['offset'] = extra.offset
      return get<TasksResponse>(`/metrics/processes/tasks${metricsParams(f, e)}`)
    },
  },

  dispatch: {
    batch:          (workflow_id?: string, limit?: number) =>
      post<DispatchBatchResponse>('/dispatch/batch', { workflow_id, limit }),
    submitted:      (body: SubmittedRequest) => post<void>('/dispatch/submitted', body),
    requeueExpired: () => post<RequeueResult>('/dispatch/requeue-expired'),
  },

  admin: {
    reconcile: () => post<ReconcileResult>('/admin/reconcile-jobs'),
  },

  daemons: {
    list: (activeOnly = false) =>
      get<DaemonAgentResponse[]>(`/daemons/${activeOnly ? '?active_only=true' : ''}`),
  },

  cohorts: {
    list: () => get<CohortListItem[]>('/cohorts'),
    summary: (collectionId: string, opts: { workflowId?: string; workflowVersion?: string } = {}) => {
      const p = new URLSearchParams()
      if (opts.workflowId)      p.set('workflow_id',      opts.workflowId)
      if (opts.workflowVersion) p.set('workflow_version', opts.workflowVersion)
      const q = p.toString()
      return get<CohortSummaryResponse>(`/cohorts/${encodeURIComponent(collectionId)}/summary${q ? `?${q}` : ''}`)
    },
    failures: (collectionId: string, process: string, opts: { workflowId?: string; workflowVersion?: string; limit?: number } = {}) => {
      const p = new URLSearchParams({ process })
      if (opts.workflowId)      p.set('workflow_id',      opts.workflowId)
      if (opts.workflowVersion) p.set('workflow_version', opts.workflowVersion)
      if (opts.limit != null)   p.set('limit',            String(opts.limit))
      return get<CohortFailuresResponse>(`/cohorts/${encodeURIComponent(collectionId)}/failures?${p}`)
    },
  },
}
