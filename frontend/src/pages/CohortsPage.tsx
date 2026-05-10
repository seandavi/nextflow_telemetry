import { useEffect, useMemo, useState } from 'react'
import { T } from '../tokens'
import { fmtNum, fmtDate } from '../lib/format'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { api } from '../lib/api'
import PageWrap from '../components/PageWrap'
import Panel from '../components/Panel'
import SectionHeader from '../components/SectionHeader'
import Btn from '../components/Btn'
import type {
  CohortListItem,
  CohortSummaryResponse,
  CohortFailureRow,
  WorkflowResponse,
} from '../types'

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
    <div
      onClick={onClick}
      style={{
        display: 'grid', gridTemplateColumns: '1fr 70px 80px 32px', gap: 12,
        alignItems: 'center', padding: '10px 12px', borderRadius: 4,
        background: selected ? T.accentDim : 'transparent',
        border: `1px solid ${selected ? T.accent : 'transparent'}`,
        cursor: 'pointer',
      }}
    >
      <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 12, color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
    </div>
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
                href={`/api/task-logs/${encodeURIComponent(r.run_name)}/${r.task_hash}`}
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
  const [selectedCohort, setSelectedCohort] = useState<string>('')
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([])
  const [workflowKey, setWorkflowKey] = useState<string>('')  // "wf_id|version" or ""
  const [summary, setSummary] = useState<CohortSummaryResponse | null>(null)
  const [selectedProcess, setSelectedProcess] = useState<string>('')
  const [failures, setFailures] = useState<CohortFailureRow[]>([])
  const [loadingFailures, setLoadingFailures] = useState(false)

  useEffect(() => {
    api.cohorts.list().then(setCohorts).catch(console.error)
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

  useEffect(() => {
    if (!selectedCohort) return
    setSummary(null)
    setSelectedProcess('')
    setFailures([])
    api.cohorts.summary(selectedCohort, wfFilter).then(setSummary).catch(console.error)
  }, [selectedCohort, workflowKey, tick])

  useEffect(() => {
    if (!selectedCohort || !selectedProcess) return
    setLoadingFailures(true)
    api.cohorts.failures(selectedCohort, selectedProcess, wfFilter)
      .then(r => setFailures(r.rows))
      .catch(console.error)
      .finally(() => setLoadingFailures(false))
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
