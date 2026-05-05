import { useState } from 'react'
import { LayoutDashboard, BarChart2, GitBranch, FlaskConical, SendHorizonal, type LucideIcon } from 'lucide-react'
import { T } from './tokens'
import { MOCK_HEALTH } from './lib/mock-data'
import OverviewPage from './pages/OverviewPage'
import MetricsPage from './pages/MetricsPage'
import WorkflowsPage from './pages/WorkflowsPage'
import SamplesPage from './pages/SamplesPage'
import DispatchPage from './pages/DispatchPage'

type NavId = 'overview' | 'metrics' | 'workflows' | 'samples' | 'dispatch'

const NAV: Array<{ id: NavId; label: string; icon: LucideIcon; sub: string }> = [
  { id: 'overview',  label: 'Overview',        icon: LayoutDashboard, sub: 'Pipeline health'      },
  { id: 'metrics',   label: 'Process Metrics', icon: BarChart2,       sub: 'Failures & resources' },
  { id: 'workflows', label: 'Workflows',        icon: GitBranch,       sub: 'Registry'             },
  { id: 'samples',   label: 'Samples',          icon: FlaskConical,    sub: 'Catalog'              },
  { id: 'dispatch',  label: 'Dispatch',         icon: SendHorizonal,   sub: '& Admin'              },
]

function HealthDot() {
  const h = MOCK_HEALTH
  const ok = h.status === 'Healthy' && h.database === 'Connected'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{
        width: 7, height: 7, borderRadius: '50%',
        background: ok ? T.green : T.red,
        boxShadow: ok ? `0 0 6px ${T.green}` : `0 0 6px ${T.red}`,
      }} />
      <span style={{ fontSize: 11, color: T.muted }}>{ok ? 'Healthy' : 'Degraded'}</span>
    </div>
  )
}

const POLL_OPTIONS = [
  { label: '10s',  value: 10_000  },
  { label: '30s',  value: 30_000  },
  { label: '1 min', value: 60_000 },
  { label: '2 min', value: 120_000 },
]

function Sidebar({ active, onNav, pollInterval, onPollInterval }: {
  active: NavId
  onNav: (id: NavId) => void
  pollInterval: number
  onPollInterval: (ms: number) => void
}) {
  return (
    <aside style={{
      width: 220, flexShrink: 0,
      background: '#0a0f1c',
      borderRight: '1px solid rgba(255,255,255,0.06)',
      display: 'flex', flexDirection: 'column',
      height: '100vh', position: 'sticky', top: 0,
    }}>
      <div style={{ padding: '22px 20px 18px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            width: 28, height: 28, borderRadius: 6,
            background: 'oklch(0.65 0.16 160)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 14, color: '#000', fontWeight: 700,
          }}>N</div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 700, color: T.text, lineHeight: 1.2 }}>Nextflow</div>
            <div style={{ fontSize: 10, color: T.muted, letterSpacing: '0.06em', textTransform: 'uppercase' }}>Telemetry</div>
          </div>
        </div>
      </div>

      <nav style={{ flex: 1, padding: '12px 10px', overflowY: 'auto' }}>
        {NAV.map(item => {
          const isActive = active === item.id
          return (
            <button key={item.id} onClick={() => onNav(item.id)} style={{
              width: '100%', display: 'flex', alignItems: 'center', gap: 12,
              padding: '9px 10px', borderRadius: 6, marginBottom: 2,
              background: isActive ? 'oklch(0.65 0.16 160 / 0.12)' : 'transparent',
              border: `1px solid ${isActive ? 'oklch(0.65 0.16 160 / 0.25)' : 'transparent'}`,
              cursor: 'pointer', textAlign: 'left', transition: 'all 0.15s',
            }}
              onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'rgba(255,255,255,0.04)' }}
              onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
            >
              <item.icon size={16} style={{
                color: isActive ? T.accent : T.muted, flexShrink: 0,
              }} />
              <div>
                <div style={{ fontSize: 13, fontWeight: isActive ? 600 : 500, color: isActive ? T.text : T.muted }}>
                  {item.label}
                </div>
                <div style={{ fontSize: 10, color: isActive ? T.accent : 'rgba(107,122,150,0.7)', marginTop: 1 }}>
                  {item.sub}
                </div>
              </div>
            </button>
          )
        })}
      </nav>

      <div style={{
        padding: '14px 16px', borderTop: '1px solid rgba(255,255,255,0.06)',
        display: 'flex', flexDirection: 'column', gap: 10,
      }}>
        <HealthDot />
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 10, color: 'rgba(107,122,150,0.7)' }}>Auto-refresh</span>
          <select
            value={pollInterval}
            onChange={e => onPollInterval(Number(e.target.value))}
            style={{
              background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.15)',
              color: T.text, fontSize: 11, borderRadius: 4, padding: '2px 4px', cursor: 'pointer',
            }}
          >
            {POLL_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <div style={{ fontSize: 10, color: 'rgba(107,122,150,0.5)', fontFamily: 'DM Mono, monospace' }}>v0.1.0</div>
      </div>
    </aside>
  )
}

export default function App() {
  const [page, setPage] = useState<NavId>('overview')
  const [pollInterval, setPollInterval] = useState(30_000)

  const pageMap: Record<NavId, React.ReactElement> = {
    overview:  <OverviewPage  pollInterval={pollInterval} />,
    metrics:   <MetricsPage   pollInterval={pollInterval} />,
    workflows: <WorkflowsPage pollInterval={pollInterval} />,
    samples:   <SamplesPage   pollInterval={pollInterval} />,
    dispatch:  <DispatchPage />,
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <Sidebar active={page} onNav={setPage} pollInterval={pollInterval} onPollInterval={setPollInterval} />
      <main style={{ flex: 1, overflowY: 'auto', background: '#080c14' }}>
        {pageMap[page]}
      </main>
    </div>
  )
}
