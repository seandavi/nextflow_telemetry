import { CSSProperties, ReactNode } from 'react'
import { T } from '../tokens'

interface Props { children: ReactNode; style?: CSSProperties }

export default function Panel({ children, style }: Props) {
  return (
    <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: '20px 22px', ...style }}>
      {children}
    </div>
  )
}
