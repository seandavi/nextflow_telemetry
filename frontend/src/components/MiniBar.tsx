import { T } from '../tokens'

interface Props { pct: number; color?: string; height?: number }

export default function MiniBar({ pct, color, height = 6 }: Props) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 120 }}>
      <div style={{ flex: 1, height, background: T.elevated, borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          width: `${Math.min(pct, 100)}%`, height: '100%',
          background: color ?? T.accent, borderRadius: 2, transition: 'width 0.4s ease',
        }} />
      </div>
      <span style={{ fontSize: 11, color: T.muted, fontFamily: 'DM Mono, monospace', minWidth: 38, textAlign: 'right' }}>
        {pct.toFixed(1)}%
      </span>
    </div>
  )
}
