import { useState, useEffect } from 'react'
import { T } from '../tokens'
import { api } from '../lib/api'
import { useAuth, useRole } from '../lib/auth'
import Btn from '../components/Btn'
import Badge from '../components/Badge'
import Input from '../components/Input'
import Select from '../components/Select'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { DispatchBatchResponse, ReconcileResult, RequeueResult, RequeueDlqResult, WatchdogResult, WorkflowResponse } from '../types'

function DispatchBatchPanel() {
  const [wfId,      setWfId]      = useState('')
  const [limit,     setLimit]     = useState('50')
  const [result,    setResult]    = useState<DispatchBatchResponse | null>(null)
  const [loading,   setLoading]   = useState(false)
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([])

  useEffect(() => {
    api.workflows.list().then(wfs => setWorkflows(wfs.filter(w => w.status === 'active'))).catch(console.error)
  }, [])

  const wfOptions = [
    { value: '', label: 'Any active workflow' },
    ...workflows.map(w => ({ value: w.workflow_id, label: `${w.workflow_id}  v${w.version}` })),
  ]

  function claim() {
    setLoading(true)
    api.dispatch.batch(wfId || undefined, Math.min(Math.max(1, +limit), 500))
      .then(r => { setResult(r); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <Panel>
        <SectionHeader title="POST /dispatch/batch"
          sub="Atomically claim a set of pending jobs and prepare a nextflow run command" />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 200px auto', gap: 12, alignItems: 'flex-end' }}>
          <Select label="Workflow filter (optional)" value={wfId} onChange={setWfId} options={wfOptions} />
          <Input  label="Batch size (max 500)" value={limit} onChange={setLimit} type="number" mono />
          <Btn onClick={claim} disabled={loading}>{loading ? 'Claiming…' : 'Claim Batch'}</Btn>
        </div>
      </Panel>

      {result && (
        <Panel>
          <SectionHeader title="Response — 200 OK"
            sub={`${result.jobs.length} jobs claimed and locked`}
            actions={<Badge label="claimed" variant="active" />}
          />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px,1fr))', gap: 10, marginBottom: 16 }}>
            {([
              ['run_name',       result.run_name],
              ['workflow_id',    result.workflow_id],
              ['version',        result.workflow_version],
              ['repository_url', result.repository_url],
              ['revision',       result.revision],
              ['profile',        result.profile],
            ] as [string, string][]).map(([k, v]) => (
              <div key={k}>
                <div style={{ fontSize: 10, color: T.muted, fontWeight: 600,
                  textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 2 }}>{k}</div>
                <div style={{ fontSize: 12, color: T.text, fontFamily: 'DM Mono, monospace', wordBreak: 'break-all' }}>{v}</div>
              </div>
            ))}
          </div>
          <div style={{ background: T.elevated, borderRadius: 6, padding: '10px 14px', marginBottom: 14 }}>
            <div style={{ fontSize: 11, color: T.muted, marginBottom: 6 }}>
              Sample IDs ({result.jobs.length}) — pass as{' '}
              <span style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>--sample_ids</span>
            </div>
            <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 11, color: T.text,
              lineHeight: 1.8, maxHeight: 96, overflowY: 'auto' }}>
              {result.jobs.map(j => j.sample_id).join(', ')}
            </div>
          </div>
          <div style={{ background: 'oklch(0.68 0.14 230 / 0.07)',
            border: `1px solid oklch(0.68 0.14 230 / 0.2)`, borderRadius: 6, padding: '12px 16px' }}>
            <div style={{ fontSize: 11, color: T.muted, marginBottom: 8 }}>nextflow command</div>
            <pre style={{ margin: 0, fontFamily: 'DM Mono, monospace', fontSize: 11,
              color: T.blue, lineHeight: 1.9, whiteSpace: 'pre-wrap' }}>
{`nextflow run ${result.repository_url} \\
  -r ${result.revision} \\
  -name ${result.run_name} \\
  -profile ${result.profile} \\
  -with-weblog $TELEMETRY_URL/telemetry \\
  --sample_ids "${result.jobs.slice(0, 3).map(j => j.sample_id).join(',')}${result.jobs.length > 3 ? ',…' : ''}"`}
            </pre>
          </div>
        </Panel>
      )}
    </div>
  )
}

function SubmittedPanel() {
  const [runName, setRunName] = useState('')
  const [jobId,   setJobId]   = useState('')
  const [done,    setDone]    = useState(false)
  const [loading, setLoading] = useState(false)

  function confirm() {
    setLoading(true)
    api.dispatch.submitted({ run_name: runName, executor_job_id: jobId || undefined })
      .then(() => { setDone(true); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <Panel>
      <SectionHeader title="POST /dispatch/submitted"
        sub="Confirm a run has been handed off to the executor — transitions claimed → submitted" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 480 }}>
        <Input label="run_name" value={runName} onChange={setRunName} placeholder="nf-run-abc123" mono />
        <Input label="executor_job_id (optional)" value={jobId} onChange={setJobId} placeholder="SLURM job ID, PID…" mono />
        <div>
          <Btn onClick={confirm} disabled={!runName || loading || done}>
            {loading ? 'Confirming…' : done ? '✓ Confirmed' : 'Confirm Submitted'}
          </Btn>
        </div>
        {done && (
          <div style={{ background: 'oklch(0.68 0.15 145 / 0.1)',
            border: `1px solid oklch(0.68 0.15 145 / 0.3)`, borderRadius: 6, padding: '10px 14px' }}>
            <span style={{ fontSize: 12, color: T.green }}>
              Run <span style={{ fontFamily: 'DM Mono, monospace' }}>{runName}</span>{' '}
              transitioned <strong>claimed → submitted</strong>.
            </span>
          </div>
        )}
      </div>
    </Panel>
  )
}

function RequeuePanel() {
  const [result,  setResult]  = useState<RequeueResult | null>(null)
  const [loading, setLoading] = useState(false)

  function run() {
    setLoading(true)
    api.dispatch.requeueExpired()
      .then(r => { setResult(r); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <Panel>
      <SectionHeader title="POST /dispatch/requeue-expired"
        sub="Reset claimed runs not confirmed within 5 min back to pending — safe to call from cron" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 480 }}>
        <div style={{ fontSize: 13, color: T.muted, lineHeight: 1.6 }}>
          Finds <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>workflow_run</code>{' '}
          records stuck in <Badge label="claimed" variant="neutral" /> for &gt;5 minutes,
          marks them <Badge label="expired" variant="error" />, and resets associated jobs to{' '}
          <Badge label="pending" variant="neutral" />.
        </div>
        <div><Btn onClick={run} disabled={loading}>{loading ? 'Scanning…' : 'Run Requeue'}</Btn></div>
        {result && (
          <div style={{ display: 'flex', gap: 12 }}>
            <KPICard label="Runs requeued" value={result.requeued_runs} accent={T.green} />
          </div>
        )}
      </div>
    </Panel>
  )
}

function ReconcilePanel() {
  const [result,  setResult]  = useState<ReconcileResult | null>(null)
  const [loading, setLoading] = useState(false)

  function run() {
    setLoading(true)
    api.admin.reconcile()
      .then(r => { setResult(r); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <Panel>
      <SectionHeader title="POST /admin/reconcile-jobs"
        sub="Materialise pending jobs for every (sample × active workflow) pair missing one — idempotent" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 520 }}>
        <div style={{ fontSize: 13, color: T.muted }}>
          Uses{' '}
          <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>ON CONFLICT DO NOTHING</code>
          {' '}— safe to call repeatedly.
        </div>
        <div><Btn onClick={run} disabled={loading}>{loading ? 'Reconciling…' : 'Run Reconcile'}</Btn></div>
        {result && (
          <div style={{ display: 'flex', gap: 12 }}>
            <KPICard label="Jobs created" value={result.jobs_created.toLocaleString()}
              sub="New pending jobs" accent={T.green} />
          </div>
        )}
      </div>
    </Panel>
  )
}

function RequeueDlqPanel() {
  const [result,  setResult]  = useState<RequeueDlqResult | null>(null)
  const [loading, setLoading] = useState(false)

  function run() {
    setLoading(true)
    api.admin.requeueDeadLetter()
      .then(r => { setResult(r); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <Panel>
      <SectionHeader title="POST /admin/requeue-dead-letter"
        sub="Move every unresolved dead-letter job back to pending — use after fixing an infra-level cause" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 520 }}>
        <div style={{ fontSize: 13, color: T.muted, lineHeight: 1.6 }}>
          Resets all unresolved <Badge label="dead_letter" variant="error" /> jobs to{' '}
          <Badge label="pending" variant="neutral" /> (clears{' '}
          <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>retry_count</code>,{' '}
          <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>run_name</code>, failure fields)
          and marks the dead-letter rows resolved. All-or-nothing — there is no per-job filter.
          Fix the root cause first, or the jobs just re-fail and re-dead-letter.
        </div>
        <div><Btn onClick={run} disabled={loading}>{loading ? 'Requeuing…' : 'Requeue Dead-Letter'}</Btn></div>
        {result && (
          <div style={{ display: 'flex', gap: 12 }}>
            <KPICard label="Jobs requeued" value={result.requeued.toLocaleString()}
              sub="dead_letter → pending" accent={T.green} />
          </div>
        )}
      </div>
    </Panel>
  )
}

function WatchdogPanel() {
  const [minutes, setMinutes] = useState('15')
  const [result,  setResult]  = useState<WatchdogResult | null>(null)
  const [loading, setLoading] = useState(false)

  function run() {
    setLoading(true)
    const m = Number(minutes)
    api.admin.heartbeatWatchdog(Number.isFinite(m) && m > 0 ? m : undefined)
      .then(r => { setResult(r); setLoading(false) })
      .catch(e => { console.error(e); setLoading(false) })
  }

  return (
    <Panel>
      <SectionHeader title="POST /admin/heartbeat-watchdog"
        sub="Fail zombie 'running' runs that stopped heartbeating — the SLURM walltime/OOM backstop; safe to call from cron" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 560 }}>
        <div style={{ fontSize: 13, color: T.muted, lineHeight: 1.6 }}>
          Finds <Badge label="running" variant="neutral" /> runs whose last{' '}
          <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>heartbeat</code>{' '}
          is older than the window, marks them <Badge label="failed" variant="error" />, and sweeps
          their jobs through retry / dead-letter. Queued{' '}
          <Badge label="submitted" variant="neutral" /> runs (no heartbeat by design) are left alone.
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
          <Input label="Stale after (min)" value={minutes} onChange={setMinutes} type="number" mono />
          <Btn onClick={run} disabled={loading}>{loading ? 'Scanning…' : 'Run Watchdog'}</Btn>
        </div>
        {result && (
          <div style={{ display: 'flex', gap: 12 }}>
            <KPICard label="Runs failed" value={result.stale_runs_failed}
              sub={`no heartbeat > ${result.stale_after_minutes}m`} accent={result.stale_runs_failed ? T.red : T.green} />
            <KPICard label="Jobs swept" value={result.jobs_swept.toLocaleString()}
              sub="retry / dead-letter" accent={T.amber} />
          </div>
        )}
      </div>
    </Panel>
  )
}

type Tab = 'dispatch' | 'submitted' | 'requeue' | 'reconcile' | 'requeue-dlq' | 'watchdog'
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'dispatch',    label: 'Dispatch Batch'      },
  { id: 'submitted',   label: 'Confirm Submitted'   },
  { id: 'requeue',     label: 'Requeue Expired'     },
  { id: 'requeue-dlq', label: 'Requeue Dead-Letter' },
  { id: 'reconcile',   label: 'Reconcile Jobs'      },
  { id: 'watchdog',    label: 'Heartbeat Watchdog'  },
]

export default function DispatchPage() {
  const [tab, setTab] = useState<Tab>('dispatch')
  const isAdmin = useRole('admin')
  const { signIn, user } = useAuth()

  if (!isAdmin) {
    return (
      <PageWrap>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Dispatch & Admin</div>
          <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
            Claim batches, confirm submissions, requeue expired claims, reconcile jobs
          </div>
        </div>
        <Panel>
          <div style={{
            display: 'flex', flexDirection: 'column', gap: 14,
            padding: '20px 4px', maxWidth: 560,
          }}>
            <div style={{ fontSize: 14, color: T.text, fontWeight: 600 }}>
              Admin access required
            </div>
            <div style={{ fontSize: 13, color: T.muted, lineHeight: 1.6 }}>
              This page mutates dispatch state (claim batches, confirm submissions,
              requeue, reconcile). It's available to administrators only.
              {user ? (
                <> You're signed in as <strong>{user.email}</strong> with role{' '}
                  <strong>{user.role ?? 'none'}</strong> — ask an admin to grant
                  access if you need it.</>
              ) : null}
            </div>
            {!user && (
              <div><Btn onClick={signIn}>Sign in with Google</Btn></div>
            )}
          </div>
        </Panel>
      </PageWrap>
    )
  }

  return (
    <PageWrap>
      <div>
        <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>Dispatch & Admin</div>
        <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>
          Claim batches, confirm submissions, requeue expired claims, reconcile jobs
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
      {tab === 'dispatch'  && <DispatchBatchPanel />}
      {tab === 'submitted' && <SubmittedPanel />}
      {tab === 'requeue'     && <RequeuePanel />}
      {tab === 'requeue-dlq' && <RequeueDlqPanel />}
      {tab === 'reconcile'   && <ReconcilePanel />}
      {tab === 'watchdog'    && <WatchdogPanel />}
    </PageWrap>
  )
}
