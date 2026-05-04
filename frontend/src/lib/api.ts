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
  RunningProcessesResponse,
  DispatchBatchResponse,
  ReconcileResult,
  RequeueResult,
  SampleResponse,
  SampleRegisterRequest,
  SubmittedRequest,
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

const BASE = '/api'

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
}
