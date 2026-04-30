import { useState, useEffect, useCallback } from 'react'
import { T } from '../tokens'
import { fmtNum, fmtDate, fmtAgo } from '../lib/format'
import {
  MOCK_SAMPLE_TOTAL, MOCK_SAMPLE_COHORTS, MOCK_WORKFLOWS,
  genSamplePage,
} from '../lib/mock-data'
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

  const activeWfs = MOCK_WORKFLOWS.filter(w => w.status === 'active')

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
            <div key={w.id} style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace', padding: '2px 0' }}>
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

export default function SamplesPage() {
  const [page,       setPage]       = useState(0)
  const [search,     setSearch]     = useState('')
  const [cohort,     setCohort]     = useState('')
  const [rows,       setRows]       = useState<SampleResponse[]>([])
  const [showForm,   setShowForm]   = useState(false)
  const [addedCount, setAddedCount] = useState(0)

  const totalInCatalog = MOCK_SAMPLE_TOTAL + addedCount
  const cohortSize     = Math.floor(MOCK_SAMPLE_TOTAL / MOCK_SAMPLE_COHORTS.length)
  const filteredTotal  = cohort ? cohortSize : totalInCatalog
  const totalPages     = Math.ceil(filteredTotal / PAGE_SIZE)

  useEffect(() => {
    setRows(genSamplePage(page, PAGE_SIZE, search, cohort))
  }, [page, search, cohort])

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
              {totalInCatalog.toLocaleString()}
            </span>{' '}total samples registered
          </div>
        </div>
        <Btn onClick={() => setShowForm(true)}>+ Register Sample</Btn>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10 }}>
        {MOCK_SAMPLE_COHORTS.map(c => (
          <button key={c} onClick={() => handleCohort(c)} style={{
            background: cohort === c ? T.accentDim : T.surface,
            border: `1px solid ${cohort === c ? T.accent : T.border}`,
            borderRadius: 6, padding: '10px 14px', cursor: 'pointer',
            textAlign: 'left', transition: 'all 0.15s',
          }}>
            <div style={{ fontSize: 11, color: cohort === c ? T.accent : T.muted,
              fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase' }}>{c}</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: T.text, marginTop: 4,
              fontFamily: 'DM Mono, monospace' }}>{fmtNum(cohortSize)}</div>
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
              {(page * PAGE_SIZE + 1).toLocaleString()}–{Math.min((page + 1) * PAGE_SIZE, filteredTotal).toLocaleString()}
            </span>{' '}of{' '}
            <span style={{ color: T.text, fontFamily: 'DM Mono, monospace' }}>
              {filteredTotal.toLocaleString()}
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
          onSave={() => { setAddedCount(n => n + 1); setShowForm(false) }}
        />
      )}
    </PageWrap>
  )
}
