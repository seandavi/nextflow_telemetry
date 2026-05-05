import { useState, useEffect } from 'react'
import { usePoll, fmtUpdated } from '../lib/usePoll'
import { T } from '../tokens'
import { fmtAgo } from '../lib/format'
import { api } from '../lib/api'
import PageWrap from '../components/PageWrap'
import SectionHeader from '../components/SectionHeader'
import type { DaemonAgentResponse } from '../types'

function fmtSecs(s: string): string {
  const diffMs = Date.now() - new Date(s).getTime()
  const sec = Math.floor(diffMs / 1000)
  if (sec < 60) return `${sec}s ago`
  return fmtAgo(s)
}

function StatusBadge({ agent }: { agent: DaemonAgentResponse }) {
  const stale = !agent.is_active
  const color = stale ? T.muted : agent.status === 'running' ? T.green : T.accent
  const label = stale ? 'stale' : agent.status
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '2px 9px', borderRadius: 99,
      background: stale ? 'rgba(255,255,255,0.05)' : agent.status === 'running' ? 'rgba(72,199,142,0.12)' : 'rgba(99,179,237,0.12)',
      border: `1px solid ${color}40`,
      fontSize: 11, fontWeight: 600, color, textTransform: 'uppercase', letterSpacing: '0.05em',
    }}>
      <span style={{
        width: 5, height: 5, borderRadius: '50%', background: color,
        boxShadow: stale ? 'none' : `0 0 5px ${color}`,
      }} />
      {label}
    </span>
  )
}

function AgentCard({ agent }: { agent: DaemonAgentResponse }) {
  return (
    <div style={{
      background: 'rgba(255,255,255,0.03)',
      border: `1px solid ${agent.is_active ? 'rgba(255,255,255,0.08)' : 'rgba(255,255,255,0.04)'}`,
      borderRadius: 10, padding: '18px 20px',
      display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 15, fontWeight: 700, color: T.text, fontFamily: 'DM Mono, monospace' }}>
              {agent.hostname}
            </span>
            <StatusBadge agent={agent} />
          </div>
          <div style={{ fontSize: 11, color: T.muted, fontFamily: 'DM Mono, monospace' }}>
            {agent.agent_id}
          </div>
        </div>
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          <div style={{ fontSize: 11, color: T.muted }}>last seen</div>
          <div style={{ fontSize: 12, color: agent.is_active ? T.text : T.muted, fontFamily: 'DM Mono, monospace' }}>
            {fmtSecs(agent.last_seen_at)}
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {([
          ['mode', agent.mode],
          ['profile', agent.profile ?? '—'],
          ['batch', String(agent.batch_size)],
          ...(agent.max_concurrent_runs != null ? [['max concurrent', String(agent.max_concurrent_runs)]] : []),
          ['active runs', String(agent.active_runs)],
          ...(agent.nf_client_version ? [['nf-client', agent.nf_client_version]] : []),
        ] as [string, string][]).map(([label, value]) => (
          <span key={label} style={{
            padding: '3px 8px', borderRadius: 5,
            background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.08)',
            fontSize: 11, color: T.text,
          }}>
            <span style={{ color: T.muted }}>{label}: </span>{value}
          </span>
        ))}
      </div>

      {agent.config_yaml && (
        <div>
          <div style={{ fontSize: 10, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 6 }}>
            config
          </div>
          <pre style={{
            margin: 0, padding: '10px 12px', borderRadius: 6,
            background: 'rgba(0,0,0,0.35)', border: '1px solid rgba(255,255,255,0.06)',
            fontSize: 11, lineHeight: 1.6, color: 'rgba(180,200,230,0.85)',
            fontFamily: 'DM Mono, monospace', overflowX: 'auto',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>
            {agent.config_yaml}
          </pre>
        </div>
      )}

      <div style={{ fontSize: 10, color: 'rgba(107,122,150,0.5)', marginTop: -4 }}>
        started {fmtAgo(agent.started_at)}
      </div>
    </div>
  )
}

export default function InfraPage({ pollInterval }: { pollInterval: number }) {
  const [agents, setAgents] = useState<DaemonAgentResponse[]>([])
  const [error, setError] = useState<string | null>(null)
  const { tick, lastUpdated } = usePoll(pollInterval)

  useEffect(() => {
    api.daemons.list()
      .then(data => { setAgents(data); setError(null) })
      .catch(e => setError(String(e)))
  }, [tick])

  const active = agents.filter(a => a.is_active)
  const stale  = agents.filter(a => !a.is_active)

  return (
    <PageWrap>
      <SectionHeader
        title="Infrastructure"
        sub={`${active.length} active daemon${active.length !== 1 ? 's' : ''} · ${stale.length} stale · ${fmtUpdated(lastUpdated)}`}
      />

      {error && (
        <div style={{ padding: 16, color: T.red, fontSize: 13 }}>
          Failed to load daemon agents: {error}
        </div>
      )}

      {agents.length === 0 && !error && (
        <div style={{ padding: '40px 0', textAlign: 'center', color: T.muted, fontSize: 13 }}>
          No daemon agents have reported in yet.
          <br />
          <span style={{ fontSize: 11, opacity: 0.6 }}>
            Run <code style={{ fontFamily: 'DM Mono, monospace' }}>nf-client daemon --continuous --config …</code> to register one.
          </span>
        </div>
      )}

      {active.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 11, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 10 }}>
            Active
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {active.map(a => <AgentCard key={a.agent_id} agent={a} />)}
          </div>
        </div>
      )}

      {stale.length > 0 && (
        <div>
          <div style={{ fontSize: 11, color: T.muted, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 10 }}>
            Stale
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {stale.map(a => <AgentCard key={a.agent_id} agent={a} />)}
          </div>
        </div>
      )}
    </PageWrap>
  )
}
