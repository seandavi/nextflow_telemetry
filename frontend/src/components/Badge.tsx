import { T } from '../tokens'

export type BadgeVariant = 'active' | 'paused' | 'retired' | 'success' | 'error' | 'neutral'

interface Props { label: string; variant?: BadgeVariant }

const map: Record<BadgeVariant, { bg: string; color: string; dot: string }> = {
  active:  { bg: 'oklch(0.68 0.15 145 / 0.15)', color: T.green, dot: T.green },
  paused:  { bg: 'oklch(0.78 0.14 75  / 0.15)', color: T.amber, dot: T.amber },
  retired: { bg: 'rgba(255,255,255,0.06)',       color: T.muted, dot: T.muted },
  success: { bg: 'oklch(0.68 0.15 145 / 0.15)', color: T.green, dot: T.green },
  error:   { bg: 'oklch(0.62 0.18 22  / 0.15)', color: T.red,   dot: T.red   },
  neutral: { bg: 'rgba(255,255,255,0.06)',       color: T.muted, dot: T.muted },
}

export default function Badge({ label, variant = 'neutral' }: Props) {
  const s = map[variant]
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      background: s.bg, color: s.color,
      fontSize: 11, fontWeight: 600, letterSpacing: '0.04em',
      padding: '3px 8px', borderRadius: 4,
      fontFamily: 'DM Mono, monospace', textTransform: 'uppercase',
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: s.dot, flexShrink: 0 }} />
      {label}
    </span>
  )
}
