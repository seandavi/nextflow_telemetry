import { useMemo, useState } from 'react'
import { T } from '../tokens'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { useStudies } from '../lib/useStudies'
import { CATALOG_URL, STATUS_LABEL, STATUS_ORDER, type StudyRow, type StudyStatus } from '../lib/studies'
import PageWrap from '../components/PageWrap'
import Panel from '../components/Panel'
import Btn from '../components/Btn'
import Input from '../components/Input'
import DataTable from '../components/DataTable'
import MiniBar from '../components/MiniBar'
import Badge, { type BadgeVariant } from '../components/Badge'
import StudyHeadline from '../components/StudyHeadline'

const STATUS_BADGE: Record<StudyStatus, BadgeVariant> = {
  processed: 'success', in_progress: 'active', partial: 'paused', not_started: 'neutral',
}
const STATUS_COLOR: Record<StudyStatus, string> = {
  processed: T.green, in_progress: T.blue, partial: T.amber, not_started: T.muted,
}

const link = { color: T.accent, textDecoration: 'none' }

function Clip({ text, width = 180 }: { text: string; width?: number }) {
  return (
    <div title={text} style={{ maxWidth: width, overflow: 'hidden', textOverflow: 'ellipsis' }}>
      {text || <span style={{ color: T.muted }}>—</span>}
    </div>
  )
}

function Chip({ label, on, onClick }: { label: string; on: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on} style={{
      font: 'inherit', fontSize: 11, cursor: 'pointer', borderRadius: 12, padding: '3px 10px',
      background: on ? T.accentDim : 'transparent', color: on ? T.text : T.muted,
      border: `1px solid ${on ? T.accent : T.border}`,
    }}>{label}</button>
  )
}

const pctDone = (r: StudyRow) => {
  const target = r.n_curated ?? r.registered
  return target > 0 ? (100 * r.completed) / target : 0
}

export default function StudiesPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)
  const s = useStudies(tick)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<StudyStatus | ''>('')
  const [site, setSite] = useState('')

  const sites = useMemo(() => {
    const n = new Map<string, number>()
    for (const r of s.rows) for (const b of r.catalog?.body_site ?? []) n.set(b, (n.get(b) ?? 0) + 1)
    return [...n].sort((a, b) => b[1] - a[1])
  }, [s.rows])

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    return s.rows
      .filter(r => !status || r.status === status)
      .filter(r => !site || r.catalog?.body_site.includes(site))
      .filter(r => !q || [r.study_name, r.catalog?.study_id ?? '', ...(r.catalog?.primary_disease ?? []), ...(r.catalog?.body_site ?? [])]
        .some(v => v.toLowerCase().includes(q)))
      .sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || a.study_name.localeCompare(b.study_name))
  }, [s.rows, search, status, site])

  return (
    <PageWrap>
      <StudyHeadline s={s} actions={
        <>
          <span style={{ fontSize: 11, color: T.muted, alignSelf: 'center' }}>{fmtUpdated(lastUpdated)}</span>
          <Btn variant="ghost" onClick={refresh}>Refresh</Btn>
        </>
      } />

      <Panel>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 16 }}>
          <div style={{ maxWidth: 360 }}>
            <Input value={search} onChange={setSearch} placeholder="Search study, disease, body site, BioProject" />
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em', width: 70 }}>Status</span>
            <Chip label="All" on={!status} onClick={() => setStatus('')} />
            {STATUS_ORDER.map(st => (
              <Chip key={st} label={`${STATUS_LABEL[st]} (${s.summary.counts[st]})`} on={status === st} onClick={() => setStatus(status === st ? '' : st)} />
            ))}
          </div>
          {sites.length > 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em', width: 70 }}>Body site</span>
              <Chip label="All" on={!site} onClick={() => setSite('')} />
              {sites.map(([b, n]) => (
                <Chip key={b} label={`${b} (${n})`} on={site === b} onClick={() => setSite(site === b ? '' : b)} />
              ))}
            </div>
          )}
          <div style={{ fontSize: 11, color: T.muted }}>
            {shown.length} of {s.rows.length} studies. Catalog columns from{' '}
            <a href={CATALOG_URL} target="_blank" rel="noreferrer" style={link}>studies_status.csv</a>{' '}
            (master); registered / completed / running / failed are live samples under the active workflow version.
          </div>
        </div>

        <DataTable<StudyRow>
          rows={shown}
          emptyMsg={s.loading ? 'Loading…' : 'No studies match'}
          columns={[
            { key: 'study_name', label: 'Study', mono: true,
              render: (v, r) => <span title={r.catalog?.notes || (r.catalog ? undefined : 'v2 collection not in the study catalog')}>{v as string}{!r.catalog && <span style={{ color: T.muted }}> *</span>}</span> },
            { key: 'status', label: 'Status', render: (_, r) => <Badge label={STATUS_LABEL[r.status]} variant={STATUS_BADGE[r.status]} /> },
            { key: 'progress', label: 'Progress', render: (_, r) => <MiniBar pct={pctDone(r)} color={STATUS_COLOR[r.status]} /> },
            { key: 'n_curated', label: 'Curated', align: 'right', mono: true, render: v => v == null ? '—' : (v as number).toLocaleString() },
            { key: 'registered', label: 'Registered', align: 'right', mono: true, render: v => (v as number).toLocaleString() },
            { key: 'completed', label: 'Completed', align: 'right', mono: true,
              render: v => <span style={{ color: (v as number) ? T.green : T.muted }}>{(v as number).toLocaleString()}</span> },
            { key: 'running', label: 'Run / Fail', align: 'right', mono: true, render: (_, r) => (
              <span>
                <span style={{ color: r.running ? T.amber : T.muted }}>{r.running.toLocaleString()}</span>
                <span style={{ color: T.muted }}> / </span>
                <span style={{ color: r.failed ? T.red : T.muted }}>{r.failed.toLocaleString()}</span>
              </span>
            ) },
            { key: 'bioproject', label: 'BioProject', mono: true, render: (_, r) => {
              const id = r.catalog?.study_id ?? ''
              return id.startsWith('PRJ')
                ? <a href={`https://www.ncbi.nlm.nih.gov/bioproject/${id}`} target="_blank" rel="noreferrer" style={link}>{id}</a>
                : (id || <span style={{ color: T.muted }}>—</span>)
            } },
            { key: 'disease', label: 'Disease', render: (_, r) => <Clip text={r.catalog?.primary_disease.join('; ') ?? ''} /> },
            { key: 'site', label: 'Body site', render: (_, r) => <Clip text={r.catalog?.body_site.join('; ') ?? ''} width={120} /> },
            { key: 'country', label: 'Country', render: (_, r) => <Clip text={r.catalog?.country.join('; ') ?? ''} width={120} /> },
            { key: 'platform', label: 'Platform', render: (_, r) => <Clip text={r.catalog?.sequencing_platform.join('; ') ?? ''} width={120} /> },
            { key: 'pmid', label: 'PMID', mono: true, render: (_, r) => r.catalog?.pmid
              ? <a href={`https://pubmed.ncbi.nlm.nih.gov/${r.catalog.pmid}/`} target="_blank" rel="noreferrer" style={link}>{r.catalog.pmid}</a>
              : <span style={{ color: T.muted }}>—</span> },
          ]}
        />
        {s.rows.some(r => !r.catalog) && (
          <div style={{ fontSize: 11, color: T.muted, marginTop: 10 }}>* v2 collection with no row in the study catalog.</div>
        )}
      </Panel>
    </PageWrap>
  )
}
