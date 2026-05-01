import { useState, useEffect } from 'react'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { T } from '../tokens'
import { fmtNum, fmtDate, fmtAgo } from '../lib/format'
import { api } from '../lib/api'
import Btn from '../components/Btn'
import Badge from '../components/Badge'
import Input from '../components/Input'
import PageWrap from '../components/PageWrap'
import type { WorkflowResponse, WorkflowRegisterRequest, WorkflowJobSummary } from '../types'

type WfStatus = 'active' | 'paused' | 'retired'

function WorkflowFormModal({
  wf, onClose, onSave,
}: {
  wf: WorkflowResponse | null
  onClose: () => void
  onSave: (data: WorkflowRegisterRequest) => void
}) {
  const [form, setForm] = useState<WorkflowRegisterRequest>({
    workflow_id:    wf?.workflow_id    ?? '',
    version:        wf?.version        ?? '',
    repository_url: wf?.repository_url ?? '',
    revision:       wf?.revision       ?? 'main',
    profile:        wf?.profile        ?? 'standard',
    max_retries:    wf?.max_retries    ?? 3,
    description:    wf?.description    ?? '',
  })

  const set = <K extends keyof WorkflowRegisterRequest>(k: K, v: WorkflowRegisterRequest[K]) =>
    setForm(f => ({ ...f, [k]: v }))

  const valid = form.workflow_id.trim() !== '' && form.version.trim() !== ''
    && form.repository_url.trim() !== '' && form.revision.trim() !== ''

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }} onClick={onClose}>
      <div style={{
        background: T.surface, border: `1px solid ${T.borderHi}`,
        borderRadius: 10, padding: 28, width: 520, maxHeight: '90vh',
        overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 16,
      }} onClick={e => e.stopPropagation()}>
        <div style={{ fontSize: 16, fontWeight: 700, color: T.text }}>
          {wf ? 'Edit Workflow' : 'Register Workflow'}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <Input label="Workflow ID" value={form.workflow_id}
            onChange={v => set('workflow_id', v)} placeholder="curatedMetagenomics" mono />
          <Input label="Version"     value={form.version}
            onChange={v => set('version', v)} placeholder="1.0.0" mono />
        </div>
        <Input label="Repository URL" value={form.repository_url}
          onChange={v => set('repository_url', v)} placeholder="https://github.com/nf-core/…" mono />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <Input label="Revision" value={form.revision}
            onChange={v => set('revision', v)} placeholder="main" mono />
          <Input label="Profile"  value={form.profile}
            onChange={v => set('profile', v)} placeholder="standard" mono />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ fontSize: 11, color: T.muted, fontWeight: 600,
            letterSpacing: '0.05em', textTransform: 'uppercase' }}>Max Retries</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <input type="range" min={0} max={10} value={form.max_retries}
              onChange={e => set('max_retries', +e.target.value)}
              style={{ flex: 1, accentColor: T.accent }} />
            <span style={{ fontSize: 14, color: T.text, fontFamily: 'DM Mono, monospace',
              minWidth: 16, textAlign: 'center' }}>{form.max_retries}</span>
          </div>
        </div>
        <Input label="Description (optional)" value={form.description}
          onChange={v => set('description', v)} placeholder="What does this workflow do?" />
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 4 }}>
          <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
          <Btn disabled={!valid} onClick={() => valid && onSave(form)}>
            {wf ? 'Save Changes' : 'Register'}
          </Btn>
        </div>
      </div>
    </div>
  )
}

function JobSummaryBar({ summary }: { summary: WorkflowJobSummary }) {
  const { total, completed, failed, running, pending, claimed, dead_letter, completion_pct } = summary
  if (total === 0) {
    return (
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <span style={{ fontSize: 11, color: T.muted }}>No jobs</span>
      </div>
    )
  }
  const completedPct = (completed / total) * 100
  const failedPct    = (failed / total) * 100
  const runningPct   = (running / total) * 100
  return (
    <div style={{ width: 220, flexShrink: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
        <span style={{ fontSize: 11, color: T.muted }}>Jobs</span>
        <span style={{ fontSize: 11, color: T.text, fontFamily: 'DM Mono, monospace' }}>
          {fmtNum(completed)}/{fmtNum(total)}
          <span style={{ color: T.muted }}> · {completion_pct.toFixed(1)}%</span>
        </span>
      </div>
      <div style={{ height: 5, background: T.elevated, borderRadius: 2, overflow: 'hidden' }}>
        <div style={{ display: 'flex', height: '100%' }}>
          <div style={{ width: `${completedPct}%`, background: T.green }} />
          <div style={{ width: `${failedPct}%`, background: T.red }} />
          <div style={{ width: `${runningPct}%`, background: T.blue }} />
        </div>
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 4, flexWrap: 'wrap' }}>
        {(pending + claimed) > 0 && (
          <span style={{ fontSize: 10, color: T.amber }}>{fmtNum(pending + claimed)} pending</span>
        )}
        {running > 0 && <span style={{ fontSize: 10, color: T.blue }}>{fmtNum(running)} running</span>}
        {failed  > 0 && <span style={{ fontSize: 10, color: T.red  }}>{fmtNum(failed)} failed</span>}
        {dead_letter > 0 && (
          <span style={{ fontSize: 10, color: T.muted }}>{fmtNum(dead_letter)} DLQ</span>
        )}
      </div>
    </div>
  )
}

function WorkflowCard({
  wf, summary, onStatusChange, onEdit,
}: {
  wf: WorkflowResponse
  summary: WorkflowJobSummary | undefined
  onStatusChange: (id: number, status: WfStatus) => void
  onEdit: () => void
}) {
  const [expanded, setExpanded] = useState(false)

  const nextStatuses: Record<WfStatus, WfStatus[]> = {
    active:  ['paused', 'retired'],
    paused:  ['active', 'retired'],
    retired: [],
  }

  const stripeColor: Record<WfStatus, string> = {
    active: T.green, paused: T.amber, retired: T.muted,
  }

  return (
    <div style={{
      background: T.surface, border: `1px solid ${T.border}`,
      borderRadius: 8, overflow: 'hidden', opacity: wf.status === 'retired' ? 0.72 : 1,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '16px 20px', cursor: 'pointer' }}
        onClick={() => setExpanded(e => !e)}>
        <div style={{
          width: 3, alignSelf: 'stretch', borderRadius: 2, flexShrink: 0,
          background: stripeColor[wf.status],
        }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: T.text, fontFamily: 'DM Mono, monospace' }}>
              {wf.workflow_id}
            </span>
            <span style={{ fontSize: 12, color: T.muted, fontFamily: 'DM Mono, monospace' }}>v{wf.version}</span>
            <Badge label={wf.status} variant={wf.status} />
          </div>
          {wf.description && (
            <div style={{ fontSize: 12, color: T.muted, marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {wf.description}
            </div>
          )}
        </div>
        {summary && <JobSummaryBar summary={summary} />}
        <span style={{ color: T.muted, fontSize: 14, transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}>▾</span>
      </div>

      {expanded && (
        <div style={{
          borderTop: `1px solid ${T.border}`, padding: '16px 20px 16px 39px',
          display: 'flex', flexDirection: 'column', gap: 14,
        }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px,1fr))', gap: 12 }}>
            {([
              ['Repository',  wf.repository_url],
              ['Revision',    wf.revision],
              ['Profile',     wf.profile],
              ['Max Retries', String(wf.max_retries)],
              ['Registered',  fmtDate(wf.created_at)],
              ['Updated',     fmtAgo(wf.updated_at)],
            ] as [string, string][]).map(([k, v]) => (
              <div key={k}>
                <div style={{ fontSize: 10, color: T.muted, fontWeight: 600,
                  letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 2 }}>{k}</div>
                <div style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace', wordBreak: 'break-all' }}>{v}</div>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Btn variant="ghost" small onClick={onEdit}>Edit</Btn>
            {nextStatuses[wf.status].map(s => (
              <Btn key={s}
                variant={s === 'retired' ? 'danger' : 'ghost'}
                small
                onClick={() => onStatusChange(wf.id, s)}>
                → {s.charAt(0).toUpperCase() + s.slice(1)}
              </Btn>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function WorkflowsPage({ pollInterval = 30_000 }: { pollInterval?: number }) {
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([])
  const [summaries, setSummaries] = useState<Record<number, WorkflowJobSummary>>({})
  const [showForm, setShowForm]   = useState(false)
  const [editWf, setEditWf]       = useState<WorkflowResponse | null>(null)
  const [statusFilter, setStatusFilter] = useState<WfStatus | ''>('')
  const { tick, refresh, lastUpdated } = usePoll(pollInterval)

  const filtered = statusFilter ? workflows.filter(w => w.status === statusFilter) : workflows
  const counts = { active: 0, paused: 0, retired: 0 }
  workflows.forEach(w => counts[w.status]++)

  useEffect(() => {
    api.workflows.list().then(wfs => {
      setWorkflows(wfs)
      // Fetch job summaries for all workflows in parallel
      wfs.forEach(wf => {
        api.workflows.jobSummary(wf.id)
          .then(s => setSummaries(prev => ({ ...prev, [wf.id]: s })))
          .catch(() => {/* summary unavailable — silently skip */})
      })
    }).catch(console.error)
  }, [tick])

  function updateStatus(id: number, status: WfStatus) {
    api.workflows.setStatus(id, status)
      .then(updated => setWorkflows(ws => ws.map(w => w.id === id ? updated : w)))
      .catch(console.error)
  }

  function handleSave(data: WorkflowRegisterRequest) {
    api.workflows.create(data)
      .then(created => {
        setWorkflows(ws => editWf
          ? ws.map(w => w.id === editWf.id ? created : w)
          : [...ws, created]
        )
        setShowForm(false)
      })
      .catch(console.error)
  }

  return (
    <PageWrap>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Workflows</div>
          <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
            {workflows.length} registered · {counts.active} active · {counts.paused} paused · {counts.retired} retired
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {lastUpdated && <span style={{ fontSize: 11, color: T.muted }}>{fmtUpdated(lastUpdated)}</span>}
          <button onClick={refresh} style={{ background: T.elevated, border: `1px solid ${T.border}`, color: T.muted, fontSize: 11, cursor: 'pointer', borderRadius: 4, padding: '3px 8px' }}>↻</button>
          <Btn onClick={() => { setEditWf(null); setShowForm(true) }}>+ Register Workflow</Btn>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        {(['', 'active', 'paused', 'retired'] as const).map(s => (
          <button key={s} onClick={() => setStatusFilter(s)} style={{
            background: statusFilter === s ? T.accentDim : T.elevated,
            border: `1px solid ${statusFilter === s ? T.accent : T.border}`,
            color: statusFilter === s ? T.accent : T.muted,
            borderRadius: 20, padding: '4px 14px', fontSize: 12,
            fontWeight: 600, cursor: 'pointer', fontFamily: 'DM Sans, sans-serif',
          }}>
            {s === '' ? 'All' : s.charAt(0).toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {filtered.map(wf => (
          <WorkflowCard key={wf.id} wf={wf}
            summary={summaries[wf.id]}
            onStatusChange={updateStatus}
            onEdit={() => { setEditWf(wf); setShowForm(true) }}
          />
        ))}
      </div>

      {showForm && (
        <WorkflowFormModal
          wf={editWf}
          onClose={() => setShowForm(false)}
          onSave={handleSave}
        />
      )}
    </PageWrap>
  )
}
