import { ReactNode } from 'react'
import { T } from '../tokens'

type Variant = 'primary' | 'ghost' | 'danger'

interface Props {
  children: ReactNode
  onClick?: () => void
  variant?: Variant
  small?: boolean
  disabled?: boolean
}

const variants: Record<Variant, React.CSSProperties> = {
  primary: { background: T.accent, color: '#000' },
  ghost:   { background: T.elevated, color: T.text, border: `1px solid ${T.border}` },
  danger:  { background: 'oklch(0.62 0.18 22 / 0.15)', color: T.red, border: `1px solid oklch(0.62 0.18 22 / 0.3)` },
}

export default function Btn({ children, onClick, variant = 'primary', small, disabled }: Props) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: small ? '5px 12px' : '8px 16px',
        fontSize: small ? 12 : 13, fontWeight: 600,
        borderRadius: 6, cursor: disabled ? 'not-allowed' : 'pointer',
        border: 'none', outline: 'none',
        transition: 'opacity 0.15s', opacity: disabled ? 0.5 : 1,
        fontFamily: 'DM Sans, sans-serif',
        ...variants[variant],
      }}
    >
      {children}
    </button>
  )
}
