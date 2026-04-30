import { T } from '../tokens'

interface Props {
  label?: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  mono?: boolean
  type?: string
}

export default function Input({ label, value, onChange, placeholder, mono, type = 'text' }: Props) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {label && (
        <label style={{ fontSize: 11, color: T.muted, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase' }}>
          {label}
        </label>
      )}
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={{
          background: T.elevated, border: `1px solid ${T.border}`,
          borderRadius: 6, padding: '8px 12px', color: T.text,
          fontSize: 13, outline: 'none',
          fontFamily: mono ? 'DM Mono, monospace' : 'DM Sans, sans-serif',
          transition: 'border-color 0.15s',
        }}
        onFocus={e => (e.target.style.borderColor = T.accent)}
        onBlur={e => (e.target.style.borderColor = T.border)}
      />
    </div>
  )
}
