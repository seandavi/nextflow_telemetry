# 0013. Run the control plane on Postgres; keep Cloudflare as the edge event sink

- **Status:** Proposed
- **Date:** 2026-10-30
- **Deciders:** Sean Davis

## Context

[0006](0006-cloudflare-control-plane.md) moved dispatch to a Cloudflare Worker and
Durable Objects (v2). [0009](0009-catalog-in-postgres-control-plane-in-worker.md)
split the system: the Worker became the control plane, and a Postgres catalog
service owned metadata, submissions and history. 0009 rejected moving dispatch
back to Postgres for three reasons: it would bring back the sweepers, the
`FOR UPDATE SKIP LOCKED` lock, and the dependence on one host.

Two months of running v2, and the 2026-10-30 registration batch (#381,
`docs/research/study-backlog-2026-10-30`), showed that what limits v2 is its
storage, not its semantics:

- **No bulk path.** 58,699 samples went in as one POST each, at about 12/s with
  six parallel clients, with intermittent Cloudflare 502s and retries.
- **No SQL.** Every question meant paging HTTP endpoints, or writing a new
  TypeScript endpoint. Nothing joins the registry to the lake or the source
  catalog.
- **Two stores drift apart.** The add-study bot registered into v1 while
  dispatch read v2, so five approved BioProjects never reached v2 (#330).
- **Earlier symptoms:** reconcile timed out at about 6k samples, there was no job
  priority (#298), and the dead-letter queue couldn't be listed or requeued
  selectively (#299).

v2 got these semantics right, and they are kept: readset identity
([0007](0007-readset-identity.md)), registrations as bundles
([0010](0010-registrations-are-bundles.md)), exact per-run deadlines, and an
always-available endpoint for the Nextflow weblog.

Constraints:

- **There is no connection from Cloudflare to Postgres.** The campus firewall
  blocks Tunnel and therefore Hyperdrive. HTTPS works in both directions: clusters
  and Cloudflare can reach onclappc02 on 443, and onclappc02 can reach Cloudflare's APIs.
- **The weblog is part of the control path.** `started`, the `MARK_COMPLETE` task
  event and `completed` drive run and job state.
- **SLURM scripts embed their URLs at submission.** A run can wait in the queue
  for days and then run for up to 4 days, so URLs in use today must keep working
  for about a week after any change.
- The scale is small: 3 daemons with up to 10 concurrent runs each, 72k
  samples, about 76k jobs.

## Decision

We will run the control plane and the catalog as **one Python service on one
Postgres database** on onclappc02. **Cloudflare stays only as the edge event
sink.**

1. **One service, one database.** The existing FastAPI application, reshaped,
   implements v2's API contract (paths, payloads, and the readset and
   registration semantics) on Postgres with SQLAlchemy Core. Registry,
   submissions, source catalog, studies, curation decisions, jobs, runs,
   dead letters and daemons share one transactional store. The v1↔v2 write-through
   goes away.
2. **Calls that need an answer go to the origin. Writes that don't go to the edge.**
   - Origin (onclappc02 over HTTPS): claim, submitted, requeue-expired, daemon
     heartbeat, registration, workflow and admin calls.
   - Edge (Worker): `/telemetry`, `/runs/{run}/event` and `/task-logs`. They
     return 2xx immediately and append to R2 as gzipped NDJSON. This is the
     current SinkDO, which flushes at most every 60 s.
3. **Events reach Postgres by pull.** An ingester on onclappc02 reads new R2
   objects after a stored cursor. It applies them through the same idempotent
   transition functions the API uses, in `received_at` order. Duplicate or
   out-of-order events are harmless, and a terminal state never flips. The
   expected lag is about a minute. **No Cloudflare Queue for now.** Add one only
   if under-a-minute latency starts to matter.
4. **Deadlines are columns, plus one sweep.**
   - Claim TTL, submit backstop and liveness deadlines are columns on `runs`.
   - One sweep every 30–60 s takes expired rows with `FOR UPDATE SKIP LOCKED`
     and applies v2's transitions.
   - If the time since the previous sweep is longer than the liveness window,
     all open deadlines are pushed out by that gap first. Without this, every
     run would be killed when the service comes back after an outage.
5. **History stays in R2.** Task-level events stay in R2 NDJSON, and the service
   queries them with DuckDB for the historical routes (#175). Postgres holds
   live state plus the per-run triage tables (`run_task_counts`,
   `run_failed_tasks`).
6. **Auth:** GETs are public, writes need a bearer token. The operator token
   lives in GSM, and the event routes stay unauthenticated because the
   reporters can't send headers (#176). Revisit if restricted metadata is ever served (#343).
7. **Hostnames:**
   - Control calls use a stable origin hostname (#177, #368).
   - The edge keeps accepting events at `nf-telemetry.seandavi.workers.dev`
     through the drain after cutover, at least the 7-day submit backstop.
   - `nf-client` gets an `event_url` separate from `server_url`.
8. **Jobs and runs are migrated, not reprocessed.** This reverses 0006's "jobs are
   never migrated". The Worker exports a consistent snapshot of the Durable
   Object (it's single-threaded, so one call is enough), and an import script
   loads it into Postgres.
9. **Source metadata values stay in the lake**
   ([0012](0012-multi-source-sample-metadata-in-the-lake.md)). Postgres holds
   the identity and study structure they join through:
   - `readset_runs`, with each run accession unique;
   - `studies` and `study_aliases`;
   - submission → catalog snapshot provenance;
   - curation decisions (#371).

## Alternatives considered

- **Keep v2 (0009 as written).** It keeps the storage limits listed in Context.
  They show up in every bulk operation, every new question, and the
  dual-write between stores.
- **Postgres behind Hyperdrive, called from the Worker.** Not possible: Tunnel is
  blocked from onclappc02.
- **Send the weblog straight to the origin.** That removes the Worker, but an
  outage on onclappc02 would then stall or lose `MARK_COMPLETE` events (#350),
  which the edge sink avoids at little cost.
- **Cloudflare Queue as the buffer.** Lower latency, but messages expire after a
  few days, so they would also need an R2 archive. R2 alone is durable,
  replayable, and already in place.
- **Load task events into Postgres.** One store for everything, but a large
  append-only table to vacuum and back up, for queries that DuckDB over R2
  already answers.

0009's three objections, reconsidered:
- **Sweepers:** one deadline sweep over indexed columns replaces three
  unrelated cron jobs.
- **`SKIP LOCKED`:** this is the standard way to serialize a few claimers, and at
  three daemons it isn't a contention point.
- **Host dependency:** catalog, lake, ETL and publication already depend on
  onclappc02. SLURM runs keep going through an outage, the edge keeps their
  events, and the grace period stops false liveness failures on recovery.

## Consequences

- One store and mostly one language. TypeScript shrinks to the sink-only
  Worker. Bulk registration becomes `COPY`, and every question becomes SQL.
- **onclappc02 now affects dispatch.** While it's down, no new claims happen.
  Postgres backups with a tested restore (#84) are a precondition for cutover.
- Runs and jobs show completion about a minute after the event.
- `nf-client` must be redeployed to all clusters with `event_url` (watch for
  version drift, #249).
- Status changes: 0006 and 0009 become **Superseded by 0013**. 0012 stands.
- Work: #381 (M0–M10, #370–#380).

## References

- Plan and tracking: #381. This ADR: #370.
- Evidence: PR #369 (`docs/research/study-backlog-2026-10-30`).
- Issues: #84, #175, #176, #177, #298, #299, #330, #350, #368.
- Constraint: monode network notes for onclappc02 (no outbound 7844, so no Tunnel).
