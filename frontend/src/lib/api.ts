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
  RunningProcessesResponse,
  DispatchBatchResponse,
  ReconcileResult,
  RequeueResult,
  SampleResponse,
  SampleRegisterRequest,
  SubmittedRequest,
} from '../types'

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
    summary:    (days?: number) => get<ProcessSummaryResponse>(`/metrics/processes/summary${days ? `?window_days=${days}` : ''}`),
    failures:   (days?: number) => get<ProcessFailuresResponse>(`/metrics/processes/failures${days ? `?window_days=${days}` : ''}`),
    retries:    (days?: number) => get<ProcessRetriesResponse>(`/metrics/processes/retries${days ? `?window_days=${days}` : ''}`),
    resources:  (days?: number) => get<ProcessResourcesByAttemptResponse>(`/metrics/processes/resources-by-attempt${days ? `?window_days=${days}` : ''}`),
    signatures: (days?: number) => get<ProcessFailureSignaturesResponse>(`/metrics/processes/failure-signatures${days ? `?window_days=${days}` : ''}`),
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
