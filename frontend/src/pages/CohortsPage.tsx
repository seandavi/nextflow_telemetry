import { useEffect, useMemo, useState } from 'react'
import { T } from '../tokens'
import { fmtNum, fmtDate } from '../lib/format'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { api, API_BASE } from '../lib/api'
import PageWrap from '../components/PageWrap'
import Panel from '../components/Panel'
import SectionHeader from '../components/SectionHeader'
import Btn from '../components/Btn'
import type {
  CohortListItem,
  CohortLeaderboardRow,
  CohortSummaryResponse,
  CohortFailureRow,
  WorkflowResponse,
} from '../types'

const STALL_DAYS = 7

function isStalled(r: CohortLeaderboardRow): boolean {
  if (r.samples_remaining <= 0) return false            // nothing left to do
  if (r.samples_running > 0) return false               // actively progressing, not stalled
  if (!r.last_completed_at) return true                 // remaining, nothing running, never completed
  const days = (Date.now() - new Date(r.last_completed_at).getTime()) / 86_400_000
  return days > STALL_DAYS
}

type LbSortKey = 'collection_id' | 'sample_count' | 'samples_completed' | 'samples_remaining' | 'completion_pct'

// Stacked bar coloured by sample STATE, not by a completion threshold:
// green = completed, amber = running, red = failed, grey track = pending/remaining.
// The green fraction is the completion %. This keeps "red" meaning failed rather
// than "low completion".
function CompletionBar({ r }: { r: CohortLeaderboardRow }) {
  const total = r.sample_count || 1
  const pctOf = (n: number) => `${Math.max(0, Math.min(100, (n / total) * 100))}%`
  const seg = (width: string, color: string, title: string) =>
    width === '0%' ? null : <div title={title} style={{ width, height: '100%', background: color }} />
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div style={{ display: 'flex', flex: 1, height: 6, background: T.border, borderRadius: 3, overflow: 'hidden' }}>
        {seg(pctOf(r.samples_completed), T.green, `${r.samples_completed} completed`)}
        {seg(pctOf(r.samples_running),   T.amber, `${r.samples_running} running`)}
        {seg(pctOf(r.samples_failed),    T.red,   `${r.samples_failed} failed`)}
      </div>
      <div style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace', minWidth: 48, textAlign: 'right' }}>
        {r.completion_pct.toFixed(1)}%
      </div>
    </div>
  )
}

const LB_COLS = '1fr 70px 80px 80px 60px 60px 200px 96px'

function LeaderboardTable({
  rows, selected, onSelect,
}: {
  rows: CohortLeaderboardRow[]
  selected: string
  onSelect: (id: string) => void
}) {
  const [sortKey, setSortKey] = useState<LbSortKey>('completion_pct')
  const [dir, setDir] = useState<'asc' | 'desc'>('asc')

  const sorted = useMemo(() => {
    const s = [...rows].sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string' && typeof bv === 'string') return av.localeCompare(bv)
      return (av as number) - (bv as number)
    })
    return dir === 'asc' ? s : s.reverse()
  }, [rows, sortKey, dir])

  const toggle = (k: LbSortKey) => {
    if (k === sortKey) setDir(d => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(k); setDir('asc') }
  }

  const Th = ({ k, label, align = 'right' }: { k: LbSortKey; label: string; align?: 'left' | 'right' }) => (
    <button
      type="button"
      onClick={() => toggle(k)}
      style={{
        font: 'inherit', background: 'transparent', border: 'none', cursor: 'pointer', padding: 0,
        fontSize: 10, color: sortKey === k ? T.text : T.muted, textTransform: 'uppercase', letterSpacing: '0.06em',
        textAlign: align, justifySelf: align === 'right' ? 'end' : 'start',
      }}
    >
      {label}{sortKey === k ? (dir === 'asc' ? ' ↑' : ' ↓') : ''}
    </button>
  )

  if (rows.length === 0) {
    return <div style={{ fontSize: 12, color: T.muted, padding: 14 }}>No cohorts yet.</div>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{
        display: 'grid', gridTemplateColumns: LB_COLS, gap: 12, padding: '8px 12px',
        borderBottom: `1px solid ${T.border}`,
      }}>
        <Th k="collection_id" label="Study" align="left" />
        <Th k="sample_count" label="Samples" />
        <Th k="samples_completed" label="Done" />
        <Th k="samples_remaining" label="Left" />
        <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em', textAlign: 'right' }}>Fail</div>
        <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em', textAlign: 'right' }}>Run</div>
        <Th k="completion_pct" label="Completion (active)" align="left" />
        <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em', textAlign: 'right' }}>Last done</div>
      </div>
      {sorted.map(r => {
        const stalled = isStalled(r)
        const isSel = r.collection_id === selected
        return (
          <button
            key={r.collection_id}
            type="button"
            onClick={() => onSelect(r.collection_id)}
            aria-pressed={isSel}
            style={{
              display: 'grid', gridTemplateColumns: LB_COLS, gap: 12, padding: '10px 12px', alignItems: 'center',
              borderBottom: `1px solid ${T.border}`, borderLeft: `2px solid ${isSel ? T.accent : 'transparent'}`,
              background: isSel ? T.accentDim : 'transparent',
              font: 'inherit', color: 'inherit', textAlign: 'inherit', width: '100%', cursor: 'pointer',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
              <span style={{ fontFamily: 'DM Mono, monospace', fontSize: 12, color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.collection_id}
              </span>
              {stalled && (
                <span style={{
                  fontSize: 9, color: T.red, border: `1px solid ${T.red}`, borderRadius: 3,
                  padding: '1px 5px', textTransform: 'uppercase', letterSpacing: '0.05em', flexShrink: 0,
                }}>
                  Stalled
                </span>
              )}
            </div>
            <div style={{ fontSize: 12, color: T.text, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{fmtNum(r.sample_count)}</div>
            <div style={{ fontSize: 12, color: T.green, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{fmtNum(r.samples_completed)}</div>
            <div style={{ fontSize: 12, color: T.muted, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{fmtNum(r.samples_remaining)}</div>
            <div style={{ fontSize: 12, color: r.samples_failed ? T.red : T.muted, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{fmtNum(r.samples_failed)}</div>
            <div style={{ fontSize: 12, color: r.samples_running ? T.amber : T.muted, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{fmtNum(r.samples_running)}</div>
            <CompletionBar r={r} />
            <div style={{ fontSize: 10, color: T.muted, textAlign: 'right' }}>{r.last_completed_at ? fmtDate(r.last_completed_at) : '—'}</div>
          </button>
        )
      })}
    </div>
  )
}

function StatusChip({ label, count, color }: { label: string; count: number; color: string }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 2,
      padding: '12px 16px', minWidth: 92,
      background: T.elevated, border: `1px solid ${T.border}`, borderRadius: 6,
    }}>
      <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, color, fontFamily: 'DM Mono, monospace' }}>{fmtNum(count)}</div>
    </div>
  )
}

function FailureBar({
  process, failedCount, sampleCount, total, onClick, selected,
}: {
  process: string
  failedCount: number
  sampleCount: number
  total: number
  onClick: () => void
  selected: boolean
}) {
  const pct = total > 0 ? (failedCount / total) * 100 : 0
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      style={{
        display: 'grid', gridTemplateColumns: '1fr 70px 80px 32px', gap: 12,
        alignItems: 'center', padding: '10px 12px', borderRadius: 4,
        background: selected ? T.accentDim : 'transparent',
        border: `1px solid ${selected ? T.accent : 'transparent'}`,
        cursor: 'pointer',
        font: 'inherit', color: 'inherit', textAlign: 'inherit', width: '100%',
      }}
    >
      <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 12, color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'left' }}>
        {process}
      </div>
      <div style={{ position: 'relative', height: 6, background: T.border, borderRadius: 3, overflow: 'hidden' }}>
        <div style={{
          position: 'absolute', top: 0, left: 0, height: '100%',
          width: `${Math.min(100, pct)}%`, background: T.red,
        }} />
      </div>
      <div style={{ fontSize: 12, color: T.text, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>
        {failedCount} <span style={{ color: T.muted }}>/{sampleCount}</span>
      </div>
      <div style={{ fontSize: 11, color: T.muted, textAlign: 'right' }}>›</div>
    </button>
  )
}

function FailuresTable({ rows, loading }: { rows: CohortFailureRow[]; loading: boolean }) {
  if (loading) {
    return <div style={{ fontSize: 12, color: T.muted, padding: 14 }}>Loading…</div>
  }
  if (rows.length === 0) {
    return <div style={{ fontSize: 12, color: T.muted, padding: 14 }}>No failed tasks for this process.</div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{
        display: 'grid',
        gridTemplateColumns: '180px 1fr 1fr 60px 60px 110px',
        gap: 12, padding: '8px 12px',
        fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em',
        borderBottom: `1px solid ${T.border}`,
      }}>
        <div>Sample</div>
        <div>Run name</div>
        <div>Task hash</div>
        <div style={{ textAlign: 'right' }}>Attempt</div>
        <div style={{ textAlign: 'right' }}>Exit</div>
        <div style={{ textAlign: 'right' }}>When</div>
      </div>
      {rows.map((r) => (
        <div key={r.telemetry_id} style={{
          display: 'grid',
          gridTemplateColumns: '180px 1fr 1fr 60px 60px 110px',
          gap: 12, padding: '8px 12px', alignItems: 'center',
          borderBottom: `1px solid ${T.border}`,
        }}>
          <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 11, color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.sample_id ?? '—'}
          </div>
          <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 11, color: T.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.run_name}
          </div>
          <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 11, color: T.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.task_hash ? (
              <a
                href={`${API_BASE}/task-logs/${encodeURIComponent(r.run_name)}/${r.task_hash}`}
                target="_blank"
                rel="noreferrer"
                style={{ color: T.accent, textDecoration: 'none' }}
              >
                {r.task_hash}
              </a>
            ) : '—'}
          </div>
          <div style={{ fontSize: 11, color: T.text, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{r.attempt}</div>
          <div style={{ fontSize: 11, color: T.text, textAlign: 'right', fontFamily: 'DM Mono, monospace' }}>{r.exit_code ?? '—'}</div>
          <div style={{ fontSize: 11, color: T.muted, textAlign: 'right' }}>{fmtDate(r.utc_time)}</div>
        </div>
      ))}
    </div>
  )
}

export default function CohortsPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  const [cohorts, setCohorts] = useState<CohortListItem[]>([])
  const [leaderboard, setLeaderboard] = useState<CohortLeaderboardRow[]>([])
  const [selectedCohort, setSelectedCohort] = useState<string>('')
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([])
  const [workflowKey, setWorkflowKey] = useState<string>('')  // "wf_id|version" or ""
  const [summary, setSummary] = useState<CohortSummaryResponse | null>(null)
  const [selectedProcess, setSelectedProcess] = useState<string>('')
  const [failures, setFailures] = useState<CohortFailureRow[]>([])
  const [loadingFailures, setLoadingFailures] = useState(false)

  useEffect(() => {
    api.cohorts.list().then(setCohorts).catch(console.error)
    api.cohorts.leaderboard().then(setLeaderboard).catch(console.error)
    api.workflows.list().then(setWorkflows).catch(console.error)
  }, [tick])

  useEffect(() => {
    if (!selectedCohort && cohorts.length > 0) setSelectedCohort(cohorts[0].collection_id)
  }, [cohorts, selectedCohort])

  const wfFilter = useMemo(() => {
    if (!workflowKey) return {}
    const [workflowId, workflowVersion] = workflowKey.split('|')
    return { workflowId, workflowVersion }
  }, [workflowKey])

  // Reset selection AND clear the previously-shown summary whenever the
  // user changes cohort or workflow filter. Clearing summary prevents
  // flashing stale data (counts/failure_by_process from the old cohort)
  // during the brief window between selection change and the new fetch
  // resolving. We deliberately do NOT depend on `tick` here — that would
  // wipe a user's drill-down every poll interval.
  useEffect(() => {
    setSummary(null)
    setSelectedProcess('')
    setFailures([])
  }, [selectedCohort, workflowKey])

  // Refresh the summary on every relevant change, including poll ticks.
  // Each invocation owns an `ignore` flag so a slow response from a
  // previous cohort/workflow can't repopulate `summary` after the user
  // has switched (the cleanup on the next render flips ignore=true,
  // and the resolved promise no-ops).
  useEffect(() => {
    if (!selectedCohort) return
    let ignore = false
    api.cohorts.summary(selectedCohort, wfFilter).then(s => {
      if (!ignore) setSummary(s)
    }).catch(console.error)
    return () => { ignore = true }
  }, [selectedCohort, workflowKey, tick])

  // Same pattern for the failed-task drill-down: refresh on poll, but
  // never lose the user's selectedProcess just because the timer fired,
  // and never let a stale response repopulate failures after the user
  // has changed cohort/process.
  useEffect(() => {
    if (!selectedCohort || !selectedProcess) return
    let ignore = false
    setLoadingFailures(true)
    api.cohorts.failures(selectedCohort, selectedProcess, wfFilter)
      .then(r => { if (!ignore) setFailures(r.rows) })
      .catch(console.error)
      .finally(() => { if (!ignore) setLoadingFailures(false) })
    return () => { ignore = true }
  }, [selectedCohort, selectedProcess, workflowKey, tick])

  const totalFailedAcrossProcesses = summary?.failure_by_process.reduce((s, r) => s + r.failed_count, 0) ?? 0
  const counts = summary?.job_status_counts

  return (
    <PageWrap>
      <SectionHeader
        title="Cohorts"
        sub="Workflow completion and failure breakdown for each collection"
        actions={
          <>
            <span style={{ fontSize: 11, color: T.muted, alignSelf: 'center' }}>{fmtUpdated(lastUpdated)}</span>
            <Btn variant="ghost" onClick={refresh}>Refresh</Btn>
          </>
        }
      />

      <Panel>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 280 }}>
            <label style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Cohort</label>
            <select
              value={selectedCohort}
              onChange={e => setSelectedCohort(e.target.value)}
              style={{
                background: T.elevated, color: T.text,
                border: `1px solid ${T.border}`, borderRadius: 4,
                padding: '8px 10px', fontFamily: 'DM Mono, monospace', fontSize: 12,
              }}
            >
              {cohorts.length === 0 && <option value="">No cohorts yet</option>}
              {cohorts.map(c => (
                <option key={c.collection_id} value={c.collection_id}>
                  {c.collection_id} ({c.sample_count} samples) — {c.source}
                </option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 280 }}>
            <label style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Workflow filter</label>
            <select
              value={workflowKey}
              onChange={e => setWorkflowKey(e.target.value)}
              style={{
                background: T.elevated, color: T.text,
                border: `1px solid ${T.border}`, borderRadius: 4,
                padding: '8px 10px', fontFamily: 'DM Mono, monospace', fontSize: 12,
              }}
            >
              <option value="">All workflows</option>
              {workflows.map(w => (
                <option key={`${w.workflow_id}|${w.version}`} value={`${w.workflow_id}|${w.version}`}>
                  {w.workflow_id} v{w.version} ({w.status})
                </option>
              ))}
            </select>
          </div>
        </div>
      </Panel>

      <Panel>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.text }}>Study leaderboard</div>
          <div style={{ fontSize: 11, color: T.muted }}>
            {leaderboard.length} stud{leaderboard.length === 1 ? 'y' : 'ies'} · completion in samples under the active workflow version
          </div>
        </div>
        <LeaderboardTable rows={leaderboard} selected={selectedCohort} onSelect={setSelectedCohort} />
      </Panel>

      {summary && (
        <>
          <Panel>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 16 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{summary.collection_id}</div>
              <div style={{ fontSize: 11, color: T.muted }}>{summary.label ?? summary.source}</div>
            </div>
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'stretch' }}>
              <StatusChip label="Samples" count={summary.sample_count} color={T.text} />
              <StatusChip label="Jobs total" count={summary.total_jobs} color={T.text} />
              <StatusChip label="Pending" count={counts?.pending ?? 0} color={T.muted} />
              <StatusChip label="Claimed" count={counts?.claimed ?? 0} color={T.blue} />
              <StatusChip label="Submitted" count={counts?.submitted ?? 0} color={T.blue} />
              <StatusChip label="Running" count={counts?.running ?? 0} color={T.amber} />
              <StatusChip label="Completed" count={counts?.completed ?? 0} color={T.green} />
              <StatusChip label="Failed" count={counts?.failed ?? 0} color={T.red} />
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 2,
                padding: '12px 16px', minWidth: 140,
                background: T.elevated, border: `1px solid ${T.border}`, borderRadius: 6,
              }}>
                <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Completion</div>
                <div style={{ fontSize: 20, fontWeight: 700, color: T.green, fontFamily: 'DM Mono, monospace' }}>
                  {summary.completion_pct.toFixed(1)}%
                </div>
                <div style={{ fontSize: 10, color: T.muted, fontFamily: 'DM Mono, monospace' }}>
                  {fmtNum(summary.samples_completed)} / {fmtNum(summary.sample_count)} samples
                </div>
              </div>
            </div>
          </Panel>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 18, alignItems: 'flex-start' }}>
            <Panel>
              <div style={{ fontSize: 12, fontWeight: 700, color: T.text, marginBottom: 12 }}>
                Failures by process
              </div>
              {summary.failure_by_process.length === 0 ? (
                <div style={{ fontSize: 12, color: T.muted, padding: 14 }}>
                  No process failures recorded for this cohort/workflow.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {summary.failure_by_process.map(p => (
                    <FailureBar
                      key={p.process}
                      process={p.process}
                      failedCount={p.failed_count}
                      sampleCount={p.sample_count}
                      total={Math.max(totalFailedAcrossProcesses, 1)}
                      onClick={() => setSelectedProcess(p.process)}
                      selected={selectedProcess === p.process}
                    />
                  ))}
                </div>
              )}
            </Panel>

            <Panel>
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 12 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: T.text }}>
                  {selectedProcess ? `Failed tasks · ${selectedProcess}` : 'Click a process to inspect failures'}
                </div>
                {selectedProcess && (
                  <div style={{ fontSize: 11, color: T.muted }}>{failures.length} task{failures.length !== 1 ? 's' : ''}</div>
                )}
              </div>
              {selectedProcess ? (
                <FailuresTable rows={failures} loading={loadingFailures} />
              ) : (
                <div style={{ fontSize: 12, color: T.muted, padding: 14 }}>
                  Select a process on the left to see the failed task occurrences for this cohort.
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </PageWrap>
  )
}
