import { ReactNode } from 'react'
import { T } from '../tokens'

export interface Column<Row> {
  key: keyof Row | string
  label: string
  align?: 'left' | 'right' | 'center'
  mono?: boolean
  render?: (value: unknown, row: Row) => ReactNode
}

interface Props<Row> {
  columns: Column<Row>[]
  rows: Row[]
  emptyMsg?: string
}

export default function DataTable<Row extends object>({
  columns, rows, emptyMsg = 'No data',
}: Props<Row>) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={i} style={{
                padding: '8px 14px', textAlign: c.align ?? 'left',
                color: T.muted, fontWeight: 600, fontSize: 11,
                letterSpacing: '0.05em', textTransform: 'uppercase',
                borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
              }}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} style={{ padding: '32px 14px', textAlign: 'center', color: T.muted }}>
                {emptyMsg}
              </td>
            </tr>
          )}
          {rows.map((row, i) => (
            <tr key={i}
              style={{ borderBottom: `1px solid ${T.border}` }}
              onMouseEnter={e => (e.currentTarget.style.background = T.elevated)}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              {columns.map((c, ci) => (
                <td key={ci} style={{
                  padding: '10px 14px', color: T.text,
                  textAlign: c.align ?? 'left', whiteSpace: 'nowrap',
                  fontFamily: c.mono ? 'DM Mono, monospace' : 'inherit',
                }}>
                  {c.render
                    ? c.render((row as Record<string, unknown>)[c.key as string], row)
                    : ((row as Record<string, unknown>)[c.key as string] as ReactNode)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
