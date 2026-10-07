import { useEffect, useMemo, useState } from 'react'
import { api, unless501, type NotAvailableValue, NOT_AVAILABLE } from './api'
import { fetchCatalog, joinStudies, summarizeStudies, type CatalogStudy } from './studies'
import type { CohortLeaderboardRow, WorkflowResponse } from '../types'

// Catalog CSV is fetched once per mount (it changes on curation-repo commits,
// not minute to minute); live counts and the active version refresh on `tick`.
export function useStudies(tick: number) {
  const [catalog, setCatalog] = useState<CatalogStudy[] | null>(null)
  const [catalogFailed, setCatalogFailed] = useState(false)
  const [live, setLive] = useState<CohortLeaderboardRow[] | NotAvailableValue | null>(null)
  const [liveFailed, setLiveFailed] = useState(false)
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([])

  useEffect(() => {
    fetchCatalog().then(setCatalog).catch(e => { console.error(e); setCatalogFailed(true) })
  }, [])

  useEffect(() => {
    unless501(api.cohorts.leaderboard())
      .then(r => { setLive(r); setLiveFailed(false) })
      .catch(e => { console.error(e); setLiveFailed(true) })
    api.workflows.list().then(setWorkflows).catch(console.error)
  }, [tick])

  const loading = (catalog === null && !catalogFailed) || (live === null && !liveFailed)
  const rows = useMemo(
    () => joinStudies(catalog ?? [], live && live !== NOT_AVAILABLE ? live : []),
    [catalog, live],
  )
  const summary = useMemo(() => summarizeStudies(rows), [rows])

  const active = workflows.filter(w => w.status === 'active')
  const pipeline = active.find(w => w.workflow_id === 'cmgd_nextflow') ?? active[0]
  const pipelineLabel = pipeline ? `${pipeline.workflow_id} ${pipeline.version}` : 'no active workflow'

  return {
    rows, summary, loading, pipelineLabel,
    catalogFailed, liveFailed, liveNotAvailable: live === NOT_AVAILABLE,
  }
}

export type StudiesState = ReturnType<typeof useStudies>
