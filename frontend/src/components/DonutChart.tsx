interface Segment { label: string; value: number; color: string }
interface Props { segments: Segment[]; size?: number; thickness?: number }

export default function DonutChart({ segments, size = 120, thickness = 18 }: Props) {
  const total = segments.reduce((s, x) => s + x.value, 0)
  const r = (size - thickness) / 2
  const cx = size / 2, cy = size / 2, circ = 2 * Math.PI * r
  let cum = 0
  return (
    <svg width={size} height={size} style={{ display: 'block' }}>
      {segments.map((seg, i) => {
        const pct = seg.value / total
        const dash = pct * circ
        const offset = circ - cum * circ
        cum += pct
        return (
          <circle key={i} cx={cx} cy={cy} r={r} fill="none"
            stroke={seg.color} strokeWidth={thickness}
            strokeDasharray={`${dash} ${circ - dash}`}
            strokeDashoffset={offset}
            style={{ transform: 'rotate(-90deg)', transformOrigin: `${cx}px ${cy}px` }} />
        )
      })}
    </svg>
  )
}
