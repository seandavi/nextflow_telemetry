import { useState, useEffect, useCallback } from 'react'
import type { MetricsFilters } from './api'

function filtersToParams(f: MetricsFilters): URLSearchParams {
  const p = new URLSearchParams()
  if (f.workflowId)      p.set('workflow_id',      f.workflowId)
  if (f.workflowVersion) p.set('workflow_version',  f.workflowVersion)
  if (f.windowHours)     p.set('window_hours',      String(f.windowHours))
  else if (f.windowDays) p.set('window_days',       String(f.windowDays))
  if (f.since)           p.set('since',             f.since)
  if (f.until)           p.set('until',             f.until)
  if (f.runName)         p.set('run_name',          f.runName)
  if (f.sampleId)        p.set('sample_id',         f.sampleId)
  return p
}

function paramsToFilters(p: URLSearchParams): MetricsFilters {
  const f: MetricsFilters = {}
  if (p.get('workflow_id'))      f.workflowId      = p.get('workflow_id')!
  if (p.get('workflow_version')) f.workflowVersion = p.get('workflow_version')!
  if (p.get('window_hours'))     f.windowHours     = Number(p.get('window_hours'))
  if (p.get('window_days'))      f.windowDays      = Number(p.get('window_days'))
  if (p.get('since'))            f.since           = p.get('since')!
  if (p.get('until'))            f.until           = p.get('until')!
  if (p.get('run_name'))         f.runName         = p.get('run_name')!
  if (p.get('sample_id'))        f.sampleId        = p.get('sample_id')!
  return f
}

export function useUrlFilters(defaults: MetricsFilters = {}): [MetricsFilters, (f: MetricsFilters) => void] {
  const [filters, setFiltersState] = useState<MetricsFilters>(() => {
    const p = new URLSearchParams(window.location.search)
    const fromUrl = paramsToFilters(p)
    // Use URL params if any are present, otherwise fall back to defaults
    return Object.keys(fromUrl).length > 0 ? fromUrl : defaults
  })

  // Keep URL in sync whenever filters change
  useEffect(() => {
    const params = filtersToParams(filters)
    const search = params.toString()
    const next = search ? `?${search}` : window.location.pathname
    history.replaceState(null, '', next)
  }, [filters])

  const setFilters = useCallback((f: MetricsFilters) => {
    setFiltersState(f)
  }, [])

  return [filters, setFilters]
}
