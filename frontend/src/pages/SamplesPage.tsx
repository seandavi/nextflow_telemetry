import { useState, useEffect, useCallback } from 'react'
import { T } from '../tokens'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { fmtNum, fmtDate, fmtAgo } from '../lib/format'
import { api } from '../lib/api'
import { useRole } from '../lib/auth'
import Btn from '../components/Btn'
import Badge from '../components/Badge'
import Input from '../components/Input'
import DataTable from '../components/DataTable'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { SampleResponse } from '../types'

const PAGE_SIZE = 50

function SampleFormModal({
  onClose, onSave,
}: {
  onClose: () => void
  onSave: (data: { sampleId: string; cohort: string; phenotype: string; source: string }) => void
}) {
  const [sampleId,  setSampleId]  = useState('')
  const [cohort,    setCohort]    = useState('')
  const [phenotype, setPhenotype] = useState('')
  const [source,    setSource]    = useState('')
  const valid = sampleId.trim().length > 0

  const activeWfs: { workflow_id: string; version: string }[] = []

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }} onClick={onClose}>
      <div style={{
        background: T.surface, border: `1px solid ${T.borderHi}`,
        borderRadius: 10, padding: 28, width: 440,
        display: 'flex', flexDirection: 'column', gap: 16,
      }} onClick={e => e.stopPropagation()}>
        <div style={{ fontSize: 16, fontWeight: 700, color: T.text }}>Register Sample</div>
        <Input label="Sample ID" value={sampleId} onChange={setSampleId} placeholder="SRR1234567" mono />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <Input label="Cohort (optional)"    value={cohort}    onChange={setCohort}    placeholder="IBD-PRISM" />
          <Input label="Phenotype (optional)" value={phenotype} onChange={setPhenotype} placeholder="CD" />
        </div>
        <Input label="Source (optional)" value={source} onChange={setSource} placeholder="stool" />
        <div style={{ background: T.elevated, borderRadius: 6, padding: '10px 14px' }}>
          <div style={{ fontSize: 11, color: T.muted, marginBottom: 6 }}>
            Will create pending jobs for {activeWfs.length} active workflow{activeWfs.length !== 1 ? 's' : ''}:
          </div>
          {activeWfs.map(w => (
            <div key={w.workflow_id} style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace', padding: '2px 0' }}>
              · {w.workflow_id} v{w.version}
            </div>
          ))}
          <div style={{ fontSize: 11, color: T.muted, marginTop: 8, lineHeight: 1.5 }}>
            Call{' '}
            <span style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>POST /admin/reconcile-jobs</span>
            {' '}after saving to materialise.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
          <Btn disabled={!valid} onClick={() => valid && onSave({ sampleId, cohort, phenotype, source })}>Register</Btn>
        </div>
      </div>
    </div>
  )
}

export default function SamplesPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [page,     setPage]     = useState(0)
  const [search,   setSearch]   = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [cohort,   setCohort]   = useState('')
  const [items,    setItems]    = useState<SampleResponse[]>([])
  const [total,    setTotal]    = useState(0)
  const [facets,   setFacets]   = useState<{ total: number; cohorts: Array<{ cohort: string; count: number }> }>({ total: 0, cohorts: [] })
  const [showForm, setShowForm] = useState(false)
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)
  const isAdmin = useRole('admin')

  // Debounce the search box so we don't fire a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search), 250)
    return () => clearTimeout(id)
  }, [search])

  // Server-side page fetch — filters + pagination happen in Postgres, so the
  // catalog stays correct past the old 1000-row client ceiling (#118).
  useEffect(() => {
    let ignore = false
    api.samples.list(page * PAGE_SIZE, PAGE_SIZE, debouncedSearch || undefined, cohort || undefined)
      .then(r => { if (!ignore) { setItems(r.items); setTotal(r.total) } })
      .catch(console.error)
    return () => { ignore = true }
  }, [page, debouncedSearch, cohort, tick])

  // Cohort chips + grand total come from a whole-catalog facet query, so they
  // don't shift as the user pages or filters.
  useEffect(() => {
    api.samples.cohortFacets().then(setFacets).catch(console.error)
  }, [tick])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const rows = items

  const handleSearch = useCallback((v: string) => {
    setSearch(v)
    setPage(0)
  }, [])

  const handleCohort = useCallback((c: string) => {
    setCohort(prev => prev === c ? '' : c)
    setPage(0)
  }, [])

  return (
    <PageWrap>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Sample Catalog</div>
          <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
            <span style={{ color: T.text, fontFamily: 'DM Mono, monospace', fontWeight: 600 }}>
              {facets.total.toLocaleString()}
            </span>{' '}total samples registered
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {lastUpdated && <span style={{ fontSize: 11, color: T.muted }}>{fmtUpdated(lastUpdated)}</span>}
          <button onClick={refresh} style={{ background: T.elevated, border: `1px solid ${T.border}`, color: T.muted, fontSize: 11, cursor: 'pointer', borderRadius: 4, padding: '3px 8px' }}>↻</button>
          {isAdmin && <Btn onClick={() => setShowForm(true)}>+ Register Sample</Btn>}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10 }}>
        {facets.cohorts.map(({ cohort: c, count }) => (
          <button key={c} onClick={() => handleCohort(c)} style={{
            background: cohort === c ? T.accentDim : T.surface,
            border: `1px solid ${cohort === c ? T.accent : T.border}`,
            borderRadius: 6, padding: '10px 14px', cursor: 'pointer',
            textAlign: 'left', transition: 'all 0.15s',
          }}>
            <div style={{ fontSize: 11, color: cohort === c ? T.accent : T.muted,
              fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase' }}>{c}</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: T.text, marginTop: 4,
              fontFamily: 'DM Mono, monospace' }}>{fmtNum(count)}</div>
          </button>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 220, maxWidth: 360 }}>
          <Input label="Search by sample ID" value={search} onChange={handleSearch} placeholder="SRR…" mono />
        </div>
        {(search || cohort) && (
          <Btn variant="ghost" small onClick={() => { setSearch(''); setCohort(''); setPage(0) }}>
            Clear filters
          </Btn>
        )}
      </div>

      <Panel style={{ padding: 0 }}>
        <DataTable<SampleResponse>
          columns={[
            { key: 'id',         label: '#',         align: 'right', mono: true,
              render: v => <span style={{ color: T.muted }}>{(v as number).toLocaleString()}</span> },
            { key: 'sample_id',  label: 'Sample ID', mono: true,
              render: v => <span style={{ color: T.accent }}>{v as string}</span> },
            { key: 'metadata',   label: 'Cohort',
              render: v => <Badge label={(v as Record<string,string>)['cohort'] ?? ''} variant="neutral" /> },
            { key: 'metadata',   label: 'Phenotype', mono: true,
              render: v => (v as Record<string,string>)['phenotype'] ?? '—' },
            { key: 'metadata',   label: 'Source',    mono: true,
              render: v => (v as Record<string,string>)['source'] ?? '—' },
            { key: 'metadata',   label: 'Reads',     align: 'right', mono: true,
              render: v => <span style={{ color: T.muted }}>{fmtNum((v as Record<string,number>)['read_count'] ?? 0)}</span> },
            { key: 'created_at', label: 'Registered', mono: true, render: v => fmtDate(v as string) },
            { key: 'updated_at', label: 'Updated',    mono: true, render: v => fmtAgo(v as string) },
          ]}
          rows={rows}
        />
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '12px 16px', borderTop: `1px solid ${T.border}`,
        }}>
          <span style={{ fontSize: 12, color: T.muted }}>
            Showing{' '}
            <span style={{ color: T.text, fontFamily: 'DM Mono, monospace' }}>
              {total === 0 ? 0 : (page * PAGE_SIZE + 1).toLocaleString()}–{Math.min((page + 1) * PAGE_SIZE, total).toLocaleString()}
            </span>{' '}of{' '}
            <span style={{ color: T.text, fontFamily: 'DM Mono, monospace' }}>
              {total.toLocaleString()}
            </span>
          </span>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <Btn variant="ghost" small disabled={page === 0} onClick={() => setPage(0)}>«</Btn>
            <Btn variant="ghost" small disabled={page === 0} onClick={() => setPage(p => p - 1)}>‹ Prev</Btn>
            <span style={{ fontSize: 12, color: T.muted, padding: '0 8px', fontFamily: 'DM Mono, monospace' }}>
              {page + 1} / {totalPages.toLocaleString()}
            </span>
            <Btn variant="ghost" small disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)}>Next ›</Btn>
            <Btn variant="ghost" small disabled={page >= totalPages - 1} onClick={() => setPage(totalPages - 1)}>»</Btn>
          </div>
        </div>
      </Panel>

      {showForm && (
        <SampleFormModal
          onClose={() => setShowForm(false)}
          onSave={({ sampleId, cohort: c, phenotype, source }) => {
            const metadata: Record<string, string> = { cohort: c }
            if (phenotype) metadata['phenotype'] = phenotype
            if (source)    metadata['source']    = source
            api.samples.create({ sample_id: sampleId, metadata })
              .then(() => {
                api.admin.reconcile().catch(console.error)
                setShowForm(false)
                refresh()  // refetch page + facets to include the new sample
              })
              .catch(console.error)
          }}
        />
      )}
    </PageWrap>
  )
}
