import { T } from '../tokens'
import { fmtNum, fmtPct } from '../lib/format'
import { MOCK_SUMMARY, MOCK_JOB_TOTALS, MOCK_WORKFLOWS, MOCK_SAMPLE_TOTAL } from '../lib/mock-data'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import DataTable from '../components/DataTable'
import MiniBar from '../components/MiniBar'
import DonutChart from '../components/DonutChart'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { TopFailureRow, TopRetryRow, TopFailureExitCodeRow } from '../types'

function ThroughputChart({ data }: { data: number[] }) {
  const W = 600, H = 100
  const max = Math.max(...data), min = Math.min(...data), range = max - min || 1
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * W,
    H - ((v - min) / range) * (H - 10) - 5,
  ] as [number, number])
  const linePts = pts.map(p => p.join(',')).join(' ')
  const areaPts = `0,${H} ${linePts} ${W},${H}`
  const avg = Math.round(data.reduce((a, b) => a + b, 0) / data.length)
  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', display: 'block', height: 100 }}>
        <defs>
          <linearGradient id="tg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"   stopColor={T.accent} stopOpacity="0.25" />
            <stop offset="100%" stopColor={T.accent} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        <polygon points={areaPts} fill="url(#tg)" />
        <polyline points={linePts} fill="none" stroke={T.accent} strokeWidth="1.5" strokeLinejoin="round" />
        {([0, 14, 29] as const).map(i => (
          <text key={i} x={(i / 29) * W} y={H} dy={-2} textAnchor="middle"
            style={{ fontSize: 9, fill: T.muted, fontFamily: 'DM Mono, monospace' }}>
            {i === 0 ? '30d ago' : i === 14 ? '15d ago' : 'today'}
          </text>
        ))}
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
        {([['Min', Math.min(...data)], ['Avg', avg], ['Max', Math.max(...data)]] as [string, number][]).map(([label, val]) => (
          <span key={label} style={{ fontSize: 11, color: T.muted }}>
            {label}: <span style={{ color: T.text, fontFamily: 'DM Mono, monospace' }}>{fmtNum(val)}/day</span>
          </span>
        ))}
      </div>
    </div>
  )
}

function JobStateDonut() {
  const jt = MOCK_JOB_TOTALS
  const segs = [
    { label: 'Completed',   value: jt.completed,       color: T.green },
    { label: 'Pending',     value: jt.pending,         color: T.amber },
    { label: 'Running',     value: jt.running,         color: T.blue  },
    { label: 'Failed',      value: jt.failed,          color: T.red   },
    { label: 'Dead-letter', value: jt.dead_letter ?? 0, color: 'oklch(0.55 0.1 22)' },
  ]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
      <DonutChart segments={segs} size={110} thickness={16} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {segs.map(s => (
          <div key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, flexShrink: 0 }} />
            <span style={{ fontSize: 12, color: T.muted, minWidth: 80 }}>{s.label}</span>
            <span style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace' }}>{fmtNum(s.value)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ExitCodeChart({ rows }: { rows: TopFailureExitCodeRow[] }) {
  const exitLabels: Record<string, string> = {
    '137': 'OOM kill (SIGKILL)', '1': 'Generic error',
    '2': 'Misuse / config', '134': 'Abort (SIGABRT)', 'null': 'Signal / unknown',
  }
  const max = Math.max(...rows.map(r => r.failures))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 4 }}>
      {rows.map(r => (
        <div key={r.exit_code}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
            <span style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace' }}>exit {r.exit_code}</span>
            <span style={{ fontSize: 11, color: T.muted }}>{fmtNum(r.failures)}</span>
          </div>
          <div style={{ height: 6, background: T.elevated, borderRadius: 2, overflow: 'hidden' }}>
            <div style={{ width: `${(r.failures / max) * 100}%`, height: '100%', background: T.red, borderRadius: 2 }} />
          </div>
          <div style={{ fontSize: 10, color: T.muted, marginTop: 3 }}>{exitLabels[r.exit_code] ?? ''}</div>
        </div>
      ))}
    </div>
  )
}

export default function OverviewPage() {
  const c  = MOCK_SUMMARY.cards
  const jt = MOCK_JOB_TOTALS
  const activeWfCount = MOCK_WORKFLOWS.filter(w => w.status === 'active').length

  return (
    <PageWrap>
      <div>
        <SectionHeader title="Pipeline Health" sub="Live job state across all workflows and samples" />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px, 1fr))', gap: 12 }}>
          <KPICard label="Total Jobs"  value={fmtNum(jt.total)}
            sub={`${MOCK_SAMPLE_TOTAL.toLocaleString()} samples × ${activeWfCount} active workflows`} accent={T.accent} />
          <KPICard label="Pending"     value={fmtNum(jt.pending)}         sub="Awaiting dispatch"     accent={T.amber} />
          <KPICard label="Running"     value={fmtNum(jt.running)}         sub="In flight"             accent={T.blue} />
          <KPICard label="Completed"   value={fmtNum(jt.completed)}
            sub={`${fmtPct((jt.completed / jt.total) * 100)} of total`} accent={T.green} />
          <KPICard label="Failed"      value={fmtNum(jt.failed)}          sub="Awaiting retry / DLQ"  accent={T.red} />
          <KPICard label="Dead-letter" value={fmtNum(jt.dead_letter ?? 0)} sub="Exhausted retries"   accent={T.red} />
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16 }}>
        <Panel>
          <SectionHeader title="Daily Throughput" sub="Jobs completed per day — last 30 days" />
          <ThroughputChart data={jt.sparkline} />
        </Panel>
        <Panel>
          <SectionHeader title="Job State" />
          <JobStateDonut />
        </Panel>
      </div>

      <div>
        <SectionHeader title="Process Execution"
          sub={`Last 30 days · ${fmtNum(c.process_completed_rows)} task completions`} />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px, 1fr))', gap: 12 }}>
          <KPICard label="Task Runs"     value={fmtNum(c.process_completed_rows)} sub={`${fmtNum(c.distinct_runs)} NF runs`}           accent={T.accent} />
          <KPICard label="Success"       value={fmtNum(c.success_rows)}  sub={fmtPct(100 - c.failure_pct)}    accent={T.green} />
          <KPICard label="Failed"        value={fmtNum(c.failure_rows)}  sub={fmtPct(c.failure_pct)}          accent={T.red} />
          <KPICard label="Retried"       value={fmtNum(c.retried_rows)}  sub={`${fmtPct(c.retry_pct)} of all`} accent={T.amber} />
          <KPICard label="Retry Success" value={fmtPct(c.retry_success_pct)} sub="Recovered via retry"       accent={T.green} />
          <KPICard label="Processes"     value={c.distinct_processes}    sub="Distinct names"                 accent={T.blue} />
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 16 }}>
        <Panel>
          <SectionHeader title="Top Failing Processes" sub="Ranked by failure count · 30 days" />
          <DataTable<TopFailureRow>
            columns={[
              { key: 'process',         label: 'Process',  mono: true },
              { key: 'failed',          label: 'Failed',   align: 'right', mono: true, render: v => fmtNum(v as number) },
              { key: 'total_completed', label: 'Total',    align: 'right', mono: true, render: v => fmtNum(v as number) },
              { key: 'failure_pct',     label: 'Rate',
                render: v => <MiniBar pct={v as number} color={(v as number) > 6 ? T.red : (v as number) > 4 ? T.amber : T.accent} /> },
            ]}
            rows={MOCK_SUMMARY.top_failures}
          />
        </Panel>
        <Panel>
          <SectionHeader title="Exit Codes" sub="Most common failure codes" />
          <ExitCodeChart rows={MOCK_SUMMARY.top_failure_exit_codes} />
        </Panel>
      </div>

      <Panel>
        <SectionHeader title="Most Retried Processes" sub="Ranked by retry count · 30 days" />
        <DataTable<TopRetryRow>
          columns={[
            { key: 'process',         label: 'Process',    mono: true },
            { key: 'retried',         label: 'Retried',    align: 'right', mono: true, render: v => fmtNum(v as number) },
            { key: 'retried_pct',     label: 'Retry Rate',
              render: v => <MiniBar pct={v as number} color={T.amber} /> },
            { key: 'retried_success', label: 'Recovered',  align: 'right', mono: true,
              render: v => <span style={{ color: T.green }}>{fmtNum(v as number)}</span> },
            { key: 'retried_failed',  label: 'Exhausted',  align: 'right', mono: true,
              render: v => <span style={{ color: T.red }}>{fmtNum(v as number)}</span> },
          ]}
          rows={MOCK_SUMMARY.top_retries}
        />
      </Panel>
    </PageWrap>
  )
}
