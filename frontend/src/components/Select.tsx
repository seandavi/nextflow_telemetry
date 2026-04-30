import { T } from '../tokens'

interface Option { value: string; label: string }
interface Props {
  label?: string
  value: string
  onChange: (v: string) => void
  options: Option[]
}

export default function Select({ label, value, onChange, options }: Props) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {label && (
        <label style={{ fontSize: 11, color: T.muted, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase' }}>
          {label}
        </label>
      )}
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{
          background: T.elevated, border: `1px solid ${T.border}`,
          borderRadius: 6, padding: '8px 12px', color: T.text,
          fontSize: 13, outline: 'none', fontFamily: 'DM Sans, sans-serif', cursor: 'pointer',
        }}
      >
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}
