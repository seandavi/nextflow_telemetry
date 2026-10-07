// Studies view (#197 Phase 3 prototype): the curation repo's study catalog
// joined with live v2 leaderboard counts. Pure functions only (no React, no
// Vite imports) so scripts/check-studies.mjs can run them under plain node.
import type { CohortLeaderboardRow } from '../types'

// ponytail: client-side join against the curation repo's master CSV. Moves
// server-side into GET /api/studies (catalog `studies` table) in Phase 3.2.
export const CATALOG_URL =
  'https://raw.githubusercontent.com/waldronlab/curatedMetagenomicDataCuration/master/inst/extdata/studies_status.csv'

export type StudyStatus = 'processed' | 'partial' | 'in_progress' | 'not_started'

export interface CatalogStudy {
  study_name: string
  study_id: string
  n_samples: number
  primary_disease: string[]
  body_site: string[]
  country: string[]
  sequencing_platform: string[]
  pmid: string
  curation_available: boolean
  notes: string
}

export interface StudyRow {
  study_name: string
  catalog: CatalogStudy | null   // null: v2 collection with no catalog row (or CSV fetch failed)
  n_curated: number | null
  registered: number
  completed: number
  running: number
  failed: number
  status: StudyStatus
}

// Minimal RFC 4180 parser: quoted fields, doubled quotes, CRLF.
export function parseCsv(text: string): Record<string, string>[] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++ }
      else if (c === '"') quoted = false
      else field += c
    } else if (c === '"') quoted = true
    else if (c === ',') { row.push(field); field = '' }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = '' }
    else if (c !== '\r') field += c
  }
  if (field || row.length) { row.push(field); rows.push(row) }
  const [header = [], ...body] = rows
  return body
    .filter(r => r.some(v => v !== ''))
    .map(r => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ''])))
}

const list = (s: string) => s.split(';').map(v => v.trim()).filter(Boolean)

// Only the catalog-ish columns. n_samples_processed / processing_status in the
// CSV are not used: they currently count registered, not completed, samples.
export function toCatalog(rows: Record<string, string>[]): CatalogStudy[] {
  return rows.filter(r => r.study_name).map(r => ({
    study_name: r.study_name,
    study_id: r.study_id ?? '',
    n_samples: Number(r.n_samples) || 0,
    primary_disease: list(r.primary_disease ?? ''),
    body_site: list(r.body_site ?? ''),
    country: list(r.country ?? ''),
    sequencing_platform: list(r.sequencing_platform ?? ''),
    pmid: r.pmid ?? '',
    curation_available: (r.curation_available ?? '').toUpperCase() === 'TRUE',
    notes: r.notes ?? '',
  }))
}

export async function fetchCatalog(): Promise<CatalogStudy[]> {
  const res = await fetch(CATALOG_URL)
  if (!res.ok) throw new Error(`catalog CSV → ${res.status}`)
  return toCatalog(parseCsv(await res.text()))
}

// The one processing-status rule, shared by the Studies page and Overview.
//   processed   : completed ≥ curated sample count, and > 0. Needs a catalog row:
//                 without a curated count we cannot claim a study is done.
//   in_progress : not processed, at least one sample claimed/submitted/running
//   partial     : some completed, nothing active
//   not_started : no v2 collection, or registered with nothing completed and
//                 nothing active (all pending or failed)
// "Active" is the leaderboard's samples_running (claimed/submitted/running jobs
// under an active workflow); pending-only work is not counted as active.
export function studyStatus(nCurated: number | null, live: CohortLeaderboardRow | undefined): StudyStatus {
  if (!live || live.sample_count === 0) return 'not_started'
  if (nCurated != null && live.samples_completed > 0 && live.samples_completed >= nCurated) return 'processed'
  if (live.samples_running > 0) return 'in_progress'
  if (live.samples_completed > 0) return 'partial'
  return 'not_started'
}

// Join on collection_id == study_name (true for studies registered via
// `nf-client add-cmd`). v2 collections missing from the catalog are kept.
export function joinStudies(catalog: CatalogStudy[], live: CohortLeaderboardRow[]): StudyRow[] {
  const byId = new Map(live.map(r => [r.collection_id, r]))
  const row = (c: CatalogStudy | null, name: string): StudyRow => {
    const l = byId.get(name)
    const nCurated = c ? c.n_samples : null
    return {
      study_name: name,
      catalog: c,
      n_curated: nCurated,
      registered: l?.sample_count ?? 0,
      completed: l?.samples_completed ?? 0,
      running: l?.samples_running ?? 0,
      failed: l?.samples_failed ?? 0,
      status: studyStatus(nCurated, l),
    }
  }
  const names = new Set(catalog.map(c => c.study_name))
  return [
    ...catalog.map(c => row(c, c.study_name)),
    ...live.filter(l => !names.has(l.collection_id)).map(l => row(null, l.collection_id)),
  ]
}

export interface StudySummary {
  counts: Record<StudyStatus, number>
  completed: number
  curated: number
}

export function summarizeStudies(rows: StudyRow[]): StudySummary {
  const counts: Record<StudyStatus, number> = { processed: 0, partial: 0, in_progress: 0, not_started: 0 }
  let completed = 0
  let curated = 0
  for (const r of rows) {
    counts[r.status]++
    completed += r.completed
    curated += r.n_curated ?? 0
  }
  return { counts, completed, curated }
}

export const STATUS_ORDER: StudyStatus[] = ['in_progress', 'partial', 'not_started', 'processed']
export const STATUS_LABEL: Record<StudyStatus, string> = {
  processed: 'Processed', partial: 'Partial', in_progress: 'In progress', not_started: 'Not started',
}
