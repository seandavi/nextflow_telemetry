import { useState } from 'react'
import { T } from '../tokens'
import { MOCK_WORKFLOWS, MOCK_SAMPLE_TOTAL } from '../lib/mock-data'
import Btn from '../components/Btn'
import Badge from '../components/Badge'
import Input from '../components/Input'
import Select from '../components/Select'
import KPICard from '../components/KPICard'
import SectionHeader from '../components/SectionHeader'
import Panel from '../components/Panel'
import PageWrap from '../components/PageWrap'
import type { DispatchBatchResponse, ReconcileResult, RequeueResult } from '../types'

function DispatchBatchPanel() {
  const [wfId,    setWfId]    = useState('')
  const [limit,   setLimit]   = useState('50')
  const [result,  setResult]  = useState<DispatchBatchResponse | null>(null)
  const [loading, setLoading] = useState(false)

  const activeWfs = MOCK_WORKFLOWS.filter(w => w.status === 'active')
  const wfOptions = [
    { value: '', label: 'Any active workflow' },
    ...activeWfs.map(w => ({ value: w.workflow_id, label: `${w.workflow_id}  v${w.version}` })),
  ]

  function claim() {
    setLoading(true)
    setTimeout(() => {
      const chosen = activeWfs.find(w => w.workflow_id === wfId) ?? activeWfs[0]!
      const n = Math.min(Math.max(1, +limit), 500)
      setResult({
        run_name: `nf-run-${Date.now().toString(36)}`,
        workflow_id: chosen.workflow_id, workflow_version: chosen.version,
        workflow_pk: chosen.id, repository_url: chosen.repository_url,
        revision: chosen.revision, profile: chosen.profile,
        jobs: Array.from({ length: n }, (_, i) => ({ sample_id: `SRR${(10000000 + i * 137).toString()}` })),
      })
      setLoading(false)
    }, 600)
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
    setTimeout(() => { setDone(true); setLoading(false) }, 500)
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
    setTimeout(() => {
      setResult({
        requeued:       Math.floor(Math.random() * 12),
        expired_marked: Math.floor(Math.random() * 12),
      })
      setLoading(false)
    }, 700)
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
            <KPICard label="Runs expired"  value={result.expired_marked} accent={T.amber} />
            <KPICard label="Jobs requeued" value={result.requeued}       accent={T.green} />
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
    setTimeout(() => {
      setResult({
        inserted:         Math.floor(Math.random() * 2400 + 100),
        skipped_existing: Math.floor(Math.random() * 400  +  50),
      })
      setLoading(false)
    }, 900)
  }

  const activeWfs = MOCK_WORKFLOWS.filter(w => w.status === 'active')

  return (
    <Panel>
      <SectionHeader title="POST /admin/reconcile-jobs"
        sub="Materialise pending jobs for every (sample × active workflow) pair missing one — idempotent" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 520 }}>
        <div style={{ background: T.elevated, borderRadius: 6, padding: '12px 14px' }}>
          <div style={{ fontSize: 11, color: T.muted, fontWeight: 600,
            textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Scope</div>
          <div style={{ fontSize: 13, color: T.text, marginBottom: 8 }}>
            <span style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>
              {MOCK_SAMPLE_TOTAL.toLocaleString()}
            </span>{' '}samples ×{' '}
            <span style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>
              {activeWfs.length}
            </span>{' '}active workflow{activeWfs.length !== 1 ? 's' : ''}
          </div>
          {activeWfs.map(w => (
            <div key={w.id} style={{ fontSize: 12, color: T.muted, fontFamily: 'DM Mono, monospace', padding: '2px 0' }}>
              · {w.workflow_id} v{w.version}
            </div>
          ))}
        </div>
        <div style={{ fontSize: 13, color: T.muted }}>
          Uses{' '}
          <code style={{ fontFamily: 'DM Mono, monospace', color: T.accent }}>ON CONFLICT DO NOTHING</code>
          {' '}— safe to call repeatedly.
        </div>
        <div><Btn onClick={run} disabled={loading}>{loading ? 'Reconciling…' : 'Run Reconcile'}</Btn></div>
        {result && (
          <div style={{ display: 'flex', gap: 12 }}>
            <KPICard label="Jobs inserted" value={result.inserted.toLocaleString()}
              sub="New pending jobs created" accent={T.green} />
            <KPICard label="Already exist" value={result.skipped_existing.toLocaleString()}
              sub="Skipped (idempotent)"    accent={T.muted} />
          </div>
        )}
      </div>
    </Panel>
  )
}

type Tab = 'dispatch' | 'submitted' | 'requeue' | 'reconcile'
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'dispatch',  label: 'Dispatch Batch'    },
  { id: 'submitted', label: 'Confirm Submitted' },
  { id: 'requeue',   label: 'Requeue Expired'   },
  { id: 'reconcile', label: 'Reconcile Jobs'    },
]

export default function DispatchPage() {
  const [tab, setTab] = useState<Tab>('dispatch')
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
      {tab === 'requeue'   && <RequeuePanel />}
      {tab === 'reconcile' && <ReconcilePanel />}
    </PageWrap>
  )
}
