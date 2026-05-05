import { useState, useEffect } from 'react'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { useUrlFilters } from '../lib/useUrlFilters'
import { T } from '../tokens'
import { fmtNum, fmtPct } from '../lib/format'
import { api, type MetricsFilters } from '../lib/api'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import DataTable from '../components/DataTable'
import MiniBar from '../components/MiniBar'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import Btn from '../components/Btn'
import Select from '../components/Select'
import type {
  ProcessSummaryResponse,
  ProcessFailuresResponse,
  ProcessRetriesResponse,
  ProcessResourcesByAttemptResponse,
  ProcessFailureSignaturesResponse,
  ProcessTimelineResponse,
  ProcessFailuresRow,
  RetryByAttemptRow,
  RetryByProcessRow,
  ResourceByAttemptRow,
  TimelineRow,
} from '../types'

// ── Window presets ────────────────────────────────────────────────────────────
const WINDOW_PRESETS = [
  { label: 'Last 30 min', windowHours: undefined, windowMinutes: 30 },
  { label: 'Last 1 h',    windowHours: 1  },
  { label: 'Last 6 h',    windowHours: 6  },
  { label: 'Last 24 h',   windowHours: 24 },
  { label: 'Last 7 d',    windowDays: 7   },
  { label: 'Last 30 d',   windowDays: 30  },
  { label: 'All time',    windowDays: undefined },
] as const

function windowLabel(f: MetricsFilters): string {
  if (!f.windowHours && !f.windowDays) return 'All time'
  if (f.windowHours) return `Last ${f.windowHours} h`
  return `Last ${f.windowDays} d`
}

// ── Filter bar ────────────────────────────────────────────────────────────────
function FilterBar({
  filters, onChange, workflows,
}: {
  filters: MetricsFilters
  onChange: (f: MetricsFilters) => void
  workflows: string[]
}) {
  const presetOptions = WINDOW_PRESETS.map(p => ({ value: p.label, label: p.label }))
  const currentPresetLabel = windowLabel(filters)

  function applyPreset(label: string) {
    const preset = WINDOW_PRESETS.find(p => p.label === label)
    if (!preset) return
    const { windowHours, windowDays } = preset as { windowHours?: number; windowDays?: number }
    onChange({ ...filters, windowHours, windowDays })
  }

  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', padding: '8px 0' }}>
      <div style={{ width: 140 }}>
        <Select
          label="Window"
          value={currentPresetLabel}
          onChange={applyPreset}
          options={presetOptions}
        />
      </div>
      <div style={{ width: 200 }}>
        <Select
          label="Workflow"
          value={filters.workflowId ?? ''}
          onChange={v => onChange({ ...filters, workflowId: v || undefined, workflowVersion: undefined })}
          options={[{ value: '', label: 'All workflows' }, ...workflows.map(w => ({ value: w, label: w }))]}
        />
      </div>
      {(filters.workflowId || filters.windowHours || filters.windowDays) && (
        <Btn variant="ghost" small onClick={() => onChange({})}>Clear filters</Btn>
      )}
    </div>
  )
}

const ERROR_ACTION_COLOR: Record<string, string> = {
  RETRY:  '#2563eb',
  FINISH: '#dc2626',
  IGNORE: '#d97706',
}

function ErrorActionBadge({ action }: { action: string | null }) {
  if (!action) return <span style={{ color: T.border }}>—</span>
  const color = ERROR_ACTION_COLOR[action] ?? T.muted
  return (
    <span style={{
      display: 'inline-block', padding: '1px 6px', borderRadius: 3,
      fontSize: 10, fontWeight: 700, letterSpacing: '0.04em',
      background: color + '22', color, border: `1px solid ${color}44`,
    }}>{action}</span>
  )
}

function FailuresTab({ data }: { data: ProcessFailuresResponse }) {
  return (
    <Panel>
      <SectionHeader title="Failure Rate by Process" sub="Ranked by total failures · error_action = what Nextflow did on failure" />
      {data.rows.length === 0
        ? <div style={{ color: T.muted, fontSize: 13 }}>No failures recorded.</div>
        : (
          <DataTable<ProcessFailuresRow>
            columns={[
              { key: 'process',         label: 'Process',      mono: true },
              { key: 'total_completed', label: 'Total',        align: 'right', mono: true, render: v => fmtNum(v as number) },
              { key: 'success',         label: 'Success',      align: 'right', mono: true,
                render: v => <span style={{ color: T.green }}>{fmtNum(v as number)}</span> },
              { key: 'failed',          label: 'Failed',       align: 'right', mono: true,
                render: v => <span style={{ color: T.red }}>{fmtNum(v as number)}</span> },
              { key: 'failure_pct',     label: 'Failure Rate',
                render: v => {
                  const pct = v as number
                  return <MiniBar pct={pct} color={pct > 6 ? T.red : pct > 3 ? T.amber : T.green} />
                }},
              { key: 'modal_failure_exit_code', label: 'Modal Exit', align: 'right', mono: true,
                render: v => v != null
                  ? <span style={{ color: T.muted, fontSize: 12 }}>exit {v as string}</span>
                  : <span style={{ color: T.border }}>—</span> },
              { key: 'modal_error_action', label: 'NF Action',
                render: v => <ErrorActionBadge action={v as string | null} /> },
            ]}
            rows={data.rows}
          />
        )
      }
    </Panel>
  )
}

function RetriesTab({ data }: { data: ProcessRetriesResponse }) {
  const { summary, by_attempt, by_process } = data
  return (
    <>
      <div style={{ fontSize: 11, color: T.muted, padding: '4px 0 8px' }}>
        These are <strong>Nextflow-level retries</strong> (attempt &gt; 1 within a single pipeline run),
        not pipeline re-queues managed by this server.
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px,1fr))', gap: 12 }}>
        <KPICard label="NF-Retried Tasks"  value={fmtNum(summary.retried_rows)}
          sub={`${fmtPct(summary.retried_pct)} of completions`} accent={T.amber} />
        <KPICard label="Retry Recoveries" value={fmtNum(summary.retry_success_rows)} sub="Retried → succeeded"  accent={T.green} />
        <KPICard label="Retry Exhausted"  value={fmtNum(summary.retry_failure_rows)} sub="Retried → still failed" accent={T.red} />
        <KPICard label="Retry Win Rate"   value={fmtPct(summary.retry_success_pct)}  sub="Success after retry"   accent={T.green} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: 16 }}>
        <Panel>
          <SectionHeader title="By Attempt" sub="Outcomes per attempt number" />
          <DataTable<RetryByAttemptRow>
            columns={[
              { key: 'attempt', label: '#',       align: 'center', mono: true },
              { key: 'rows',    label: 'Total',   align: 'right',  mono: true, render: v => fmtNum(v as number) },
              { key: 'success', label: 'Success', align: 'right',  mono: true,
                render: v => <span style={{ color: T.green }}>{fmtNum(v as number)}</span> },
              { key: 'failed',  label: 'Failed',  align: 'right',  mono: true,
                render: v => <span style={{ color: T.red }}>{fmtNum(v as number)}</span> },
            ]}
            rows={by_attempt}
          />
        </Panel>
        <Panel>
          <SectionHeader title="By Process" sub="Retry breakdown per process name" />
          {by_process.length === 0
            ? <div style={{ color: T.muted, fontSize: 13 }}>No retries recorded.</div>
            : (
              <DataTable<RetryByProcessRow>
                columns={[
                  { key: 'process',         label: 'Process',    mono: true },
                  { key: 'retried',         label: 'Retried',    align: 'right', mono: true, render: v => fmtNum(v as number) },
                  { key: 'retried_pct',     label: 'Retry Rate', render: v => <MiniBar pct={v as number} color={T.amber} /> },
                  { key: 'retried_success', label: 'Recovered',  align: 'right', mono: true,
                    render: v => <span style={{ color: T.green }}>{fmtNum(v as number)}</span> },
                  { key: 'retried_failed',  label: 'Exhausted',  align: 'right', mono: true,
                    render: v => <span style={{ color: T.red }}>{fmtNum(v as number)}</span> },
                  { key: 'max_attempt',     label: 'Max Att.',   align: 'center', mono: true },
                ]}
                rows={by_process}
              />
            )
          }
        </Panel>
      </div>
    </>
  )
}

function ResourcesTab({ data, summary }: { data: ProcessResourcesByAttemptResponse; summary: ProcessSummaryResponse | null }) {
  const [filterProcess, setFilterProcess] = useState('')
  const processes = [...new Set(data.rows.map(r => r.process))]
  const rows = data.rows.filter(r => !filterProcess || r.process === filterProcess)

  const memEff = summary?.cards.memory_efficiency_pct ?? null

  return (
    <>
      {memEff !== null && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px,1fr))', gap: 12, marginBottom: 4 }}>
          <KPICard
            label="Memory Efficiency"
            value={`${memEff.toFixed(1)}%`}
            sub="avg peak_rss / requested — low = over-provisioned"
            accent={memEff < 20 ? T.amber : memEff < 50 ? T.accent : T.green}
          />
        </div>
      )}
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end' }}>
        <div style={{ width: 280 }}>
          <Select label="Filter by process" value={filterProcess} onChange={setFilterProcess}
            options={[{ value: '', label: 'All processes' }, ...processes.map(p => ({ value: p, label: p }))]}
          />
        </div>
        {filterProcess && <Btn variant="ghost" small onClick={() => setFilterProcess('')}>Clear</Btn>}
      </div>
      <Panel>
        <SectionHeader title="CPU & Memory by Process + Attempt"
          sub="Average and P95 utilisation as % of requested resources" />
        {rows.length === 0
          ? <div style={{ color: T.muted, fontSize: 13 }}>No resource data recorded.</div>
          : (
            <DataTable<ResourceByAttemptRow>
              columns={[
                { key: 'process',                label: 'Process',       mono: true },
                { key: 'attempt',                label: 'Att',           align: 'center', mono: true },
                { key: 'rows',                   label: 'Samples',       align: 'right', mono: true, render: v => fmtNum(v as number) },
                { key: 'avg_requested_cpus',     label: 'Req CPU',       align: 'right', mono: true },
                { key: 'avg_requested_memory_gb', label: 'Req Mem (GB)', align: 'right', mono: true,
                  render: v => (v as number | null)?.toFixed(0) ?? '—' },
                { key: 'avg_pct_cpu',            label: 'CPU avg',
                  render: v => {
                    const n = v as number | null
                    return n != null
                      ? <MiniBar pct={n} color={n > 90 ? T.red : n > 70 ? T.amber : T.accent} height={5} />
                      : <span style={{color:T.muted}}>—</span>
                  }},
                { key: 'p95_pct_cpu',            label: 'CPU p95',       align: 'right', mono: true,
                  render: v => {
                    const n = v as number | null
                    return n != null ? <span style={{ color: n > 100 ? T.red : T.muted }}>{n.toFixed(0)}%</span> : '—'
                  }},
                { key: 'avg_pct_mem',            label: 'Mem avg',
                  render: v => {
                    const n = v as number | null
                    return n != null
                      ? <MiniBar pct={n} color={n > 90 ? T.red : n > 70 ? T.amber : T.blue} height={5} />
                      : <span style={{color:T.muted}}>—</span>
                  }},
                { key: 'avg_peak_rss_gb',        label: 'RSS avg (GB)', align: 'right', mono: true,
                  render: v => (v as number | null)?.toFixed(1) ?? '—' },
                { key: 'avg_read_gb',            label: 'Read (GB)',    align: 'right', mono: true,
                  render: v => (v as number | null)?.toFixed(1) ?? '—' },
                { key: 'avg_write_gb',           label: 'Write (GB)',   align: 'right', mono: true,
                  render: v => (v as number | null)?.toFixed(1) ?? '—' },
              ]}
              rows={rows}
            />
          )
        }
      </Panel>
    </>
  )
}

function SignaturesTab({ data }: { data: ProcessFailureSignaturesResponse }) {
  const { rows } = data
  if (rows.length === 0) {
    return (
      <Panel>
        <SectionHeader title="Failure Signatures"
          sub="(process × exit_code) frequency — darker cells = more failures" />
        <div style={{ color: T.muted, fontSize: 13 }}>No failure signatures recorded.</div>
      </Panel>
    )
  }

  const max = Math.max(...rows.map(r => r.failures))
  // Key heatmap columns on "exit_code|error_action" so RETRY and FINISH variants are distinct
  const colKey = (r: FailureSignatureRow) => `${r.exit_code}|${r.error_action ?? ''}`
  type ProcessMap = Record<string, Record<string, number>>
  const byProcess = rows.reduce<ProcessMap>((acc, r) => {
    if (!acc[r.process]) acc[r.process] = {}
    acc[r.process]![colKey(r)] = r.failures
    return acc
  }, {})
  const cols = [...new Set(rows.map(colKey))].sort()
  const procs = Object.keys(byProcess)

  const cellColor = (v: number): string => {
    const pct = v / max
    if (pct > 0.6) return 'oklch(0.55 0.18 22 / 0.9)'
    if (pct > 0.3) return 'oklch(0.62 0.18 22 / 0.6)'
    if (pct > 0.1) return 'oklch(0.72 0.14 50 / 0.6)'
    return 'oklch(0.72 0.14 50 / 0.25)'
  }

  return (
    <Panel>
      <SectionHeader title="Failure Signatures"
        sub="(process × exit_code) frequency — darker cells = more failures" />
      <div style={{ overflowX: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: 11, fontFamily: 'DM Mono, monospace' }}>
          <thead>
            <tr>
              <th style={{ padding: '6px 12px', textAlign: 'left', color: T.muted, fontWeight: 600,
                borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap' }}>Process</th>
              {cols.map(col => {
                const [ec, action] = col.split('|')
                return (
                  <th key={col} style={{ padding: '6px 10px', textAlign: 'center', color: T.muted,
                    fontWeight: 600, borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap' }}>
                    <div>exit {ec}</div>
                    {action && <ErrorActionBadge action={action} />}
                  </th>
                )
              })}
              <th style={{ padding: '6px 10px', textAlign: 'right', color: T.muted, fontWeight: 600,
                borderBottom: `1px solid ${T.border}` }}>Total</th>
            </tr>
          </thead>
          <tbody>
            {procs.map(p => {
              const total = Object.values(byProcess[p]!).reduce((a, b) => a + b, 0)
              return (
                <tr key={p} style={{ borderBottom: `1px solid ${T.border}` }}
                  onMouseEnter={e => (e.currentTarget.style.background = T.elevated)}
                  onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                  <td style={{ padding: '8px 12px', color: T.text, whiteSpace: 'nowrap' }}>{p}</td>
                  {cols.map(col => {
                    const v = byProcess[p]![col]
                    return (
                      <td key={col} style={{ padding: '6px 10px', textAlign: 'center' }}>
                        {v ? (
                          <span style={{
                            display: 'inline-block', minWidth: 52, padding: '2px 6px',
                            background: cellColor(v), borderRadius: 3, color: T.text, fontSize: 11,
                          }}>{fmtNum(v)}</span>
                        ) : (
                          <span style={{ color: T.border }}>—</span>
                        )}
                      </td>
                    )
                  })}
                  <td style={{ padding: '8px 10px', textAlign: 'right', color: T.muted }}>{fmtNum(total)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function TimelineTab({ data, bucket, onBucket }: {
  data: ProcessTimelineResponse
  bucket: 'hour' | 'day' | 'week'
  onBucket: (b: 'hour' | 'day' | 'week') => void
}) {
  const fmtBucket = (s: string) => {
    const d = new Date(s)
    if (bucket === 'hour') return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }

  return (
    <Panel>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <SectionHeader title="Failure Trend Over Time" sub="Success/failure counts per time bucket" />
        <div style={{ display: 'flex', gap: 4 }}>
          {(['hour', 'day', 'week'] as const).map(b => (
            <button key={b} onClick={() => onBucket(b)} style={{
              background: bucket === b ? T.accent : T.elevated,
              border: `1px solid ${bucket === b ? T.accent : T.border}`,
              color: bucket === b ? '#fff' : T.muted,
              borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer',
            }}>{b}</button>
          ))}
        </div>
      </div>
      {data.rows.length === 0
        ? <div style={{ color: T.muted, fontSize: 13 }}>No data for this window.</div>
        : (
          <DataTable<TimelineRow>
            columns={[
              { key: 'bucket_start', label: 'Time',        render: v => fmtBucket(v as string) },
              { key: 'total',        label: 'Total',        align: 'right', mono: true, render: v => fmtNum(v as number) },
              { key: 'success',      label: 'Success',      align: 'right', mono: true,
                render: v => <span style={{ color: T.green }}>{fmtNum(v as number)}</span> },
              { key: 'failed',       label: 'Failed',       align: 'right', mono: true,
                render: v => <span style={{ color: (v as number) > 0 ? T.red : T.muted }}>{fmtNum(v as number)}</span> },
              { key: 'failure_pct',  label: 'Failure Rate',
                render: (v, row) => {
                  const pct = v as number
                  const total = (row as TimelineRow).total
                  void total  // used for proportional bar width via CSS flex
                  return (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <div style={{ width: 120, height: 6, background: T.border, borderRadius: 3, overflow: 'hidden' }}>
                        <div style={{ display: 'flex', height: '100%' }}>
                          <div style={{ width: `${100 - pct}%`, background: T.green }} />
                          <div style={{ width: `${pct}%`, background: pct > 20 ? T.red : T.amber }} />
                        </div>
                      </div>
                      <span style={{ fontSize: 11, color: T.muted, minWidth: 36 }}>{fmtPct(pct)}</span>
                    </div>
                  )
                }},
            ]}
            rows={data.rows}
          />
        )
      }
    </Panel>
  )
}

type Tab = 'failures' | 'retries' | 'resources' | 'signatures' | 'timeline'
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'failures',   label: 'Failures'   },
  { id: 'retries',    label: 'Retries'    },
  { id: 'resources',  label: 'Resources'  },
  { id: 'signatures', label: 'Signatures' },
  { id: 'timeline',   label: 'Timeline'   },
]

export default function MetricsPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [tab, setTab]         = useState<Tab>('failures')
  const [filters, setFilters] = useUrlFilters({ windowDays: 30 })
  const [bucket, setBucket]   = useState<'hour' | 'day' | 'week'>('hour')
  const [workflows, setWorkflows] = useState<string[]>([])

  const [summary,    setSummary]    = useState<ProcessSummaryResponse | null>(null)
  const [failures,   setFailures]   = useState<ProcessFailuresResponse | null>(null)
  const [retries,    setRetries]    = useState<ProcessRetriesResponse | null>(null)
  const [resources,  setResources]  = useState<ProcessResourcesByAttemptResponse | null>(null)
  const [signatures, setSignatures] = useState<ProcessFailureSignaturesResponse | null>(null)
  const [timeline,   setTimeline]   = useState<ProcessTimelineResponse | null>(null)

  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  // Load workflow names once for the filter dropdown
  useEffect(() => {
    api.workflows.list()
      .then(wfs => setWorkflows([...new Set(wfs.map(w => w.workflow_id))]))
      .catch(console.error)
  }, [])

  useEffect(() => {
    setSummary(null); setFailures(null); setRetries(null); setResources(null); setSignatures(null); setTimeline(null)
    api.metrics.summary(filters).then(setSummary).catch(console.error)
    api.metrics.failures(filters).then(setFailures).catch(console.error)
    api.metrics.retries(filters).then(setRetries).catch(console.error)
    api.metrics.resources(filters).then(setResources).catch(console.error)
    api.metrics.signatures(filters).then(setSignatures).catch(console.error)
    api.metrics.timeline(filters, bucket).then(setTimeline).catch(console.error)
  }, [tick, filters, bucket])

  const windowDesc = filters.windowHours
    ? `last ${filters.windowHours}h`
    : filters.windowDays
      ? `last ${filters.windowDays}d`
      : 'all time'
  const filterDesc = filters.workflowId ? ` · ${filters.workflowId}` : ''

  return (
    <PageWrap>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Process Metrics</div>
          <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
            Task-level telemetry across all Nextflow runs · {windowDesc}{filterDesc}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 4 }}>
          {lastUpdated && <span style={{ fontSize: 11, color: T.muted }}>{fmtUpdated(lastUpdated)}</span>}
          <button onClick={refresh} style={{ background: T.elevated, border: `1px solid ${T.border}`, color: T.muted, fontSize: 11, cursor: 'pointer', borderRadius: 4, padding: '3px 8px' }}>↻</button>
        </div>
      </div>
      <FilterBar filters={filters} onChange={setFilters} workflows={workflows} />
      <div style={{ display: 'flex', gap: 2, borderBottom: `1px solid ${T.border}` }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            background: 'none', border: 'none', cursor: 'pointer',
            padding: '8px 18px', fontSize: 13, fontWeight: 600,
            color: tab === t.id ? T.accent : T.muted,
            borderBottom: tab === t.id ? `2px solid ${T.accent}` : '2px solid transparent',
            marginBottom: -1, fontFamily: 'DM Sans, sans-serif',
          }}>{t.label}</button>
        ))}
      </div>
      {(tab !== 'timeline' && (!failures || !retries || !resources || !signatures)) ||
       (tab === 'timeline' && !timeline)
        ? <div style={{ color: T.muted, fontSize: 14, padding: '32px 0' }}>Loading…</div>
        : (
          <>
            {tab === 'failures'   && <FailuresTab   data={failures!}   />}
            {tab === 'retries'    && <RetriesTab    data={retries!}    />}
            {tab === 'resources'  && <ResourcesTab  data={resources!} summary={summary} />}
            {tab === 'signatures' && <SignaturesTab  data={signatures!} />}
            {tab === 'timeline'   && <TimelineTab   data={timeline!}   bucket={bucket} onBucket={setBucket} />}
          </>
        )
      }
    </PageWrap>
  )
}
