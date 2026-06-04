import { useState, useEffect } from 'react'
import { T } from '../tokens'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { fmtNum } from '../lib/format'
import { api } from '../lib/api'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import DataTable, { Column } from '../components/DataTable'
import Badge, { BadgeVariant } from '../components/Badge'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { RunListItem, RunDetail } from '../types'

// Map the server-derived run classification to a badge colour.
function classVariant(c: string): BadgeVariant {
  switch (c) {
    case 'completed': return 'success'
    case 'active':    return 'active'
    case 'stalled':   return 'paused'
    case 'failed':
    case 'wrapper-failed':
    case 'ended-no-log': return 'error'
    case 'expired':   return 'retired'
    default:          return 'neutral'
  }
}

const CLASS_HELP: Record<string, string> = {
  'wrapper-failed': 'Driver exited non-zero — the Nextflow process itself failed.',
  'ended-no-log': 'Reached a terminal state but never uploaded .nextflow.log — driver likely hard-killed (OOM / scancel / node failure). Check sacct.',
  'stalled': 'Non-terminal but no heartbeat for >15 min — wrapper gone, run not closed.',
}

function ts(v: string | null): string {
  return v ? v.replace('T', ' ').slice(0, 19) : '—'
}

function RunDrawer({ runName, onClose }: { runName: string; onClose: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  useEffect(() => {
    setDetail(null)
    api.runs.get(runName).then(setDetail).catch(console.error)
  }, [runName])

  return (
    <Panel>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <SectionHeader title={runName} sub="Run detail" />
        <button onClick={onClose} style={{
          background: 'transparent', border: `1px solid ${T.border}`, color: T.muted,
          borderRadius: 4, padding: '2px 10px', fontSize: 12, cursor: 'pointer',
        }}>Close</button>
      </div>
      {!detail ? (
        <div style={{ color: T.muted, fontSize: 12, padding: 8 }}>Loading…</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <Badge label={detail.classification} variant={classVariant(detail.classification)} />
            <span style={{ fontSize: 12, color: T.muted, fontFamily: 'DM Mono, monospace' }}>
              status={detail.status} · {detail.workflow_id} {detail.workflow_version}
            </span>
          </div>
          {CLASS_HELP[detail.classification] && (
            <div style={{ fontSize: 12.5, color: T.amber, lineHeight: 1.6 }}>
              {CLASS_HELP[detail.classification]}
            </div>
          )}

          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            {Object.entries(detail.task_status_counts).length === 0 && (
              <div style={{ fontSize: 12, color: T.muted }}>No process_completed events recorded.</div>
            )}
            {Object.entries(detail.task_status_counts).map(([status, n]) => (
              <KPICard key={status} label={status} value={fmtNum(n)}
                accent={status === 'COMPLETED' ? T.green : status === 'FAILED' ? T.red : status === 'ABORTED' ? T.amber : T.muted} />
            ))}
          </div>
          {(detail.task_status_counts['ABORTED'] ?? 0) > 0 && (detail.task_status_counts['FAILED'] ?? 0) === 0 && (
            <div style={{ fontSize: 12.5, color: T.amber, lineHeight: 1.6 }}>
              All-ABORTED with zero FAILED tasks — the run died from outside (driver/allocation killed),
              not a pipeline task error. The aborted tasks are collateral.
            </div>
          )}

          <div style={{ fontSize: 12.5, color: T.text, fontFamily: 'DM Mono, monospace', lineHeight: 1.8 }}>
            <div>claimed:   {ts(detail.claimed_at)}</div>
            <div>submitted: {ts(detail.submitted_at)}</div>
            <div>started:   {ts(detail.started_at)}</div>
            <div>completed: {ts(detail.completed_at)}</div>
            <div>heartbeat: {ts(detail.last_heartbeat_at)}</div>
            <div>executor_job_id: {detail.executor_job_id ?? '—'}</div>
            <div>wrapper_exit_code: {detail.wrapper_exit_code ?? '—'}</div>
            <div>slurm_state: {detail.last_known_slurm_state ?? '—'}{detail.slurm_reason ? ` (${detail.slurm_reason})` : ''}</div>
          </div>

          <div style={{ display: 'flex', gap: 12, fontSize: 12.5 }}>
            <span style={{ color: detail.nextflow_log_available ? T.green : T.muted }}>
              {detail.nextflow_log_available ? '✓' : '✗'} .nextflow.log
            </span>
            <span style={{ color: detail.wrapper_output_log_available ? T.green : T.muted }}>
              {detail.wrapper_output_log_available ? '✓' : '✗'} wrapper output log
            </span>
          </div>
        </div>
      )}
    </Panel>
  )
}

export default function RunsPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  useEffect(() => {
    api.runs.list({ limit: 100 }).then(r => setRuns(r.runs)).catch(console.error)
  }, [tick])

  const columns: Column<RunListItem>[] = [
    { key: 'run_name', label: 'Run', mono: true,
      render: (v, row) => (
        <span onClick={() => setSelected(row.run_name)}
          style={{ color: T.accent, cursor: 'pointer', textDecoration: 'underline' }}>
          {String(v).slice(0, 20)}
        </span>
      ) },
    { key: 'workflow_id', label: 'Workflow', mono: true },
    { key: 'classification', label: 'State',
      render: (v) => <Badge label={String(v)} variant={classVariant(String(v))} /> },
    { key: 'status', label: 'Raw', mono: true },
    { key: 'wrapper_exit_code', label: 'Exit', align: 'right',
      render: (v) => <span style={{ fontFamily: 'DM Mono, monospace' }}>{v == null ? '—' : String(v)}</span> },
    { key: 'claimed_at', label: 'Claimed', mono: true, render: (v) => ts(v as string | null) },
  ]

  const counts = runs.reduce<Record<string, number>>((acc, r) => {
    acc[r.classification] = (acc[r.classification] ?? 0) + 1
    return acc
  }, {})
  const attention = (counts['wrapper-failed'] ?? 0) + (counts['ended-no-log'] ?? 0) + (counts['stalled'] ?? 0)

  return (
    <PageWrap>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <SectionHeader title="Runs" sub="Workflow runs with derived state classification" />
        <span style={{ fontSize: 11, color: T.muted }}>
          {fmtUpdated(lastUpdated)} · <button onClick={refresh} style={{
            background: 'transparent', border: 'none', color: T.accent, cursor: 'pointer', fontSize: 11,
          }}>refresh</button>
        </span>
      </div>

      <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <KPICard label="Runs" value={fmtNum(runs.length)} accent={T.blue} />
        <KPICard label="Completed" value={fmtNum(counts['completed'] ?? 0)} accent={T.green} />
        <KPICard label="Active" value={fmtNum(counts['active'] ?? 0)} accent={T.accent} />
        <KPICard label="Needs attention" value={fmtNum(attention)}
          sub="wrapper-failed / ended-no-log / stalled" accent={attention ? T.red : T.muted} />
      </div>

      {selected && <div style={{ marginBottom: 16 }}><RunDrawer runName={selected} onClose={() => setSelected(null)} /></div>}

      <Panel>
        <DataTable<RunListItem> columns={columns} rows={runs} emptyMsg="No runs yet" />
      </Panel>
    </PageWrap>
  )
}
