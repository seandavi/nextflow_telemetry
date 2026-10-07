import { T } from '../tokens'

// Shown in place of a panel whose route answered 501 (see unless501 in lib/api).
export default function NotAvailable({ what }: { what: string }) {
  return (
    <div role="status" style={{
      padding: '14px 16px', borderRadius: 6,
      background: T.elevated, border: `1px dashed ${T.borderHi}`,
      fontSize: 13, color: T.muted, lineHeight: 1.6,
    }}>
      <div style={{ color: T.text, fontWeight: 600, marginBottom: 2 }}>{what}: not available yet</div>
      Per-task history moved to the telemetry archive, which the API does not query yet
      (historical tier, #175). The API answered 501 Not Implemented.
    </div>
  )
}
