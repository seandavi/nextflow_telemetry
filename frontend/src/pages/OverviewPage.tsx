import { useState, useEffect } from 'react'
import { T } from '../tokens'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { fmtNum, fmtPct } from '../lib/format'
import { api } from '../lib/api'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import DataTable from '../components/DataTable'
import MiniBar from '../components/MiniBar'
import DonutChart from '../components/DonutChart'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { ProcessSummaryResponse, TopFailureRow, TopRetryRow, TopFailureExitCodeRow } from '../types'

function ExitCodeChart({ rows }: { rows: TopFailureExitCodeRow[] }) {
  const exitLabels: Record<string, string> = {
    '137': 'OOM kill (SIGKILL)', '1': 'Generic error',
    '2': 'Misuse / config', '134': 'Abort (SIGABRT)', 'null': 'Signal / unknown',
  }
  const max = Math.max(...rows.map(r => r.failures), 1)
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

export default function OverviewPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [summary, setSummary] = useState<ProcessSummaryResponse | null>(null)
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  useEffect(() => {
    api.metrics.summary(30).then(setSummary).catch(console.error)
  }, [tick])

  if (!summary) {
    return (
      <PageWrap>
        <div style={{ color: T.muted, fontSize: 14, padding: '32px 0' }}>Loading…</div>
      </PageWrap>
    )
  }

  const c = summary.cards

  const eventMixSegs = summary.event_mix.map((e, i) => ({
    label: e.event,
    value: e.rows,
    color: [T.green, T.blue, T.amber, T.red, T.muted][i % 5],
  }))

  return (
    <PageWrap>
      <div>
        <SectionHeader title="Process Execution"
          sub={`Last ${summary.window_days ?? 30} days · ${fmtNum(c.process_completed_rows)} task completions`}
          actions={
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {lastUpdated && <span style={{ fontSize: 11, color: T.muted }}>{fmtUpdated(lastUpdated)}</span>}
              <button onClick={refresh} style={{ background: T.elevated, border: `1px solid ${T.border}`, color: T.muted, fontSize: 11, cursor: 'pointer', borderRadius: 4, padding: '3px 8px' }}>↻</button>
            </div>
          }
        />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px, 1fr))', gap: 12 }}>
          <KPICard label="Task Runs"     value={fmtNum(c.process_completed_rows)} sub={`${fmtNum(c.distinct_runs)} NF runs`}     accent={T.accent} />
          <KPICard label="Success"       value={fmtNum(c.success_rows)}           sub={fmtPct(100 - c.failure_pct)}              accent={T.green} />
          <KPICard label="Failed"        value={fmtNum(c.failure_rows)}           sub={fmtPct(c.failure_pct)}                    accent={T.red} />
          <KPICard label="Retried"       value={fmtNum(c.retried_rows)}           sub={`${fmtPct(c.retry_pct)} of all`}          accent={T.amber} />
          <KPICard label="Retry Success" value={fmtPct(c.retry_success_pct)}      sub="Recovered via retry"                      accent={T.green} />
          <KPICard label="Processes"     value={c.distinct_processes}             sub="Distinct names"                           accent={T.blue} />
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 16 }}>
        <Panel>
          <SectionHeader title="Top Failing Processes" sub={`Ranked by failure count · ${summary.window_days ?? 30} days`} />
          {summary.top_failures.length === 0
            ? <div style={{ color: T.muted, fontSize: 13 }}>No failures recorded.</div>
            : (
              <DataTable<TopFailureRow>
                columns={[
                  { key: 'process',         label: 'Process',  mono: true },
                  { key: 'failed',          label: 'Failed',   align: 'right', mono: true, render: v => fmtNum(v as number) },
                  { key: 'total_completed', label: 'Total',    align: 'right', mono: true, render: v => fmtNum(v as number) },
                  { key: 'failure_pct',     label: 'Rate',
                    render: v => <MiniBar pct={v as number} color={(v as number) > 6 ? T.red : (v as number) > 4 ? T.amber : T.accent} /> },
                ]}
                rows={summary.top_failures}
              />
            )
          }
        </Panel>
        <Panel>
          <SectionHeader title="Exit Codes" sub="Most common failure codes" />
          {summary.top_failure_exit_codes.length === 0
            ? <div style={{ color: T.muted, fontSize: 13 }}>No failures recorded.</div>
            : <ExitCodeChart rows={summary.top_failure_exit_codes} />
          }
        </Panel>
      </div>

      {eventMixSegs.length > 0 && (
        <Panel>
          <SectionHeader title="Event Mix" sub="Distribution of Nextflow weblog event types" />
          <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
            <DonutChart segments={eventMixSegs} size={110} thickness={16} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {eventMixSegs.map(s => (
                <div key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, flexShrink: 0 }} />
                  <span style={{ fontSize: 12, color: T.muted, minWidth: 140 }}>{s.label}</span>
                  <span style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace' }}>{fmtNum(s.value)}</span>
                </div>
              ))}
            </div>
          </div>
        </Panel>
      )}

      <Panel>
        <SectionHeader title="Most Retried Processes" sub={`Ranked by retry count · ${summary.window_days ?? 30} days`} />
        {summary.top_retries.length === 0
          ? <div style={{ color: T.muted, fontSize: 13 }}>No retries recorded.</div>
          : (
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
              rows={summary.top_retries}
            />
          )
        }
      </Panel>
    </PageWrap>
  )
}
