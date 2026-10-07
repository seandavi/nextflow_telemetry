import { T } from '../tokens'
import { fmtNum } from '../lib/format'
import type { StudiesState } from '../lib/useStudies'
import KPICard from './KPICard'
import NotAvailable from './NotAvailable'
import SectionHeader from './SectionHeader'

// Study-level progress under the active pipeline version, used on Overview and Studies.
export default function StudyHeadline({ s, actions }: { s: StudiesState; actions?: React.ReactNode }) {
  const { counts, completed, curated } = s.summary
  return (
    <div>
      <SectionHeader
        title={`Studies · ${s.pipelineLabel}`}
        sub={`${fmtNum(completed)} samples completed / ${fmtNum(curated)} curated samples, under the active version`}
        actions={actions}
      />
      {s.catalogFailed && <Notice>Study catalog (curatedMetagenomicDataCuration studies_status.csv) could not be loaded; showing v2 collections only.</Notice>}
      {s.liveFailed && <Notice>Live counts from the API could not be loaded; every study shows as not started.</Notice>}
      {s.liveNotAvailable && <NotAvailable what="Live study counts" />}
      {s.loading ? (
        <div style={{ color: T.muted, fontSize: 13 }}>Loading…</div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(168px, 1fr))', gap: 12 }}>
          <KPICard label="Processed"   value={counts.processed}   sub="completed ≥ curated samples" accent={T.green} />
          <KPICard label="Partial"     value={counts.partial}     sub="some completed, none active" accent={T.amber} />
          <KPICard label="In progress" value={counts.in_progress} sub="jobs active"                 accent={T.blue} />
          <KPICard label="Not started" value={counts.not_started} sub="nothing completed or active" accent={T.muted} />
        </div>
      )}
    </div>
  )
}

function Notice({ children }: { children: React.ReactNode }) {
  return <div role="status" style={{ fontSize: 12, color: T.amber, marginBottom: 10 }}>{children}</div>
}
