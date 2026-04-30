import { ReactNode } from 'react'

export default function PageWrap({ children }: { children: ReactNode }) {
  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, display: 'flex', flexDirection: 'column', gap: 28 }}>
      {children}
    </div>
  )
}
