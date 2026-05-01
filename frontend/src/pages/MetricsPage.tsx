import { useState, useEffect } from 'react'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { T } from '../tokens'
import { fmtNum, fmtPct } from '../lib/format'
import { api } from '../lib/api'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import DataTable from '../components/DataTable'
import MiniBar from '../components/MiniBar'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import Btn from '../components/Btn'
import Select from '../components/Select'
import type {
  ProcessFailuresResponse,
  ProcessRetriesResponse,
  ProcessResourcesByAttemptResponse,
  ProcessFailureSignaturesResponse,
  ProcessFailuresRow,
  RetryByAttemptRow,
  RetryByProcessRow,
  ResourceByAttemptRow,
} from '../types'

function FailuresTab({ data }: { data: ProcessFailuresResponse }) {
  return (
    <Panel>
      <SectionHeader title="Failure Rate by Process" sub="Ranked by total failures · min 50 samples" />
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
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px,1fr))', gap: 12 }}>
        <KPICard label="Retried Tasks"   value={fmtNum(summary.retried_rows)}
          sub={`${fmtPct(summary.retried_pct)} of completions`} accent={T.amber} />
        <KPICard label="Retry Successes" value={fmtNum(summary.retry_success_rows)} sub="Eventually passed"  accent={T.green} />
        <KPICard label="Retry Failures"  value={fmtNum(summary.retry_failure_rows)} sub="Went to DLQ"        accent={T.red} />
        <KPICard label="Retry Win Rate"  value={fmtPct(summary.retry_success_pct)}  sub="Success after retry" accent={T.green} />
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

function ResourcesTab({ data }: { data: ProcessResourcesByAttemptResponse }) {
  const [filterProcess, setFilterProcess] = useState('')
  const processes = [...new Set(data.rows.map(r => r.process))]
  const rows = data.rows.filter(r => !filterProcess || r.process === filterProcess)

  return (
    <>
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
  type ProcessMap = Record<string, Record<string, number>>
  const byProcess = rows.reduce<ProcessMap>((acc, r) => {
    if (!acc[r.process]) acc[r.process] = {}
    acc[r.process]![r.exit_code] = r.failures
    return acc
  }, {})
  const exitCodes = [...new Set(rows.map(r => r.exit_code))].sort()
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
              {exitCodes.map(ec => (
                <th key={ec} style={{ padding: '6px 10px', textAlign: 'center', color: T.muted,
                  fontWeight: 600, borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap' }}>
                  exit {ec}
                </th>
              ))}
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
                  {exitCodes.map(ec => {
                    const v = byProcess[p]![ec]
                    return (
                      <td key={ec} style={{ padding: '6px 10px', textAlign: 'center' }}>
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

type Tab = 'failures' | 'retries' | 'resources' | 'signatures'
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'failures',   label: 'Failures'   },
  { id: 'retries',    label: 'Retries'    },
  { id: 'resources',  label: 'Resources'  },
  { id: 'signatures', label: 'Signatures' },
]

export default function MetricsPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [tab, setTab] = useState<Tab>('failures')
  const [failures,   setFailures]   = useState<ProcessFailuresResponse | null>(null)
  const [retries,    setRetries]    = useState<ProcessRetriesResponse | null>(null)
  const [resources,  setResources]  = useState<ProcessResourcesByAttemptResponse | null>(null)
  const [signatures, setSignatures] = useState<ProcessFailureSignaturesResponse | null>(null)
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  useEffect(() => {
    api.metrics.failures().then(setFailures).catch(console.error)
    api.metrics.retries().then(setRetries).catch(console.error)
    api.metrics.resources().then(setResources).catch(console.error)
    api.metrics.signatures().then(setSignatures).catch(console.error)
  }, [tick])

  const loading = !failures || !retries || !resources || !signatures

  return (
    <PageWrap>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Process Metrics</div>
          <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
            Task-level telemetry across all Nextflow runs · last 30 days
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 4 }}>
          {lastUpdated && <span style={{ fontSize: 11, color: T.muted }}>{fmtUpdated(lastUpdated)}</span>}
          <button onClick={refresh} style={{ background: T.elevated, border: `1px solid ${T.border}`, color: T.muted, fontSize: 11, cursor: 'pointer', borderRadius: 4, padding: '3px 8px' }}>↻</button>
        </div>
      </div>
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
      {loading
        ? <div style={{ color: T.muted, fontSize: 14, padding: '32px 0' }}>Loading…</div>
        : (
          <>
            {tab === 'failures'   && <FailuresTab   data={failures}   />}
            {tab === 'retries'    && <RetriesTab    data={retries}    />}
            {tab === 'resources'  && <ResourcesTab  data={resources}  />}
            {tab === 'signatures' && <SignaturesTab  data={signatures} />}
          </>
        )
      }
    </PageWrap>
  )
}
