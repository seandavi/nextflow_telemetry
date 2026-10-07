# 0009. Keep the catalog in Postgres and the control plane in the Worker

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** Sean Davis

## Context

[0006](0006-cloudflare-control-plane.md) framed the v2 Worker as a replacement
for the FastAPI + Postgres server. A side-by-side review on 2026-10-07 (#170)
found that framing too coarse.

| | v1 FastAPI + Postgres | v2 Worker + DO + R2 |
|---|---|---|
| Code | 8.9k LOC Python, 5.5k LOC tests, 16 tables, 18 Alembic migrations | 2.9k LOC TS, 450 LOC tests, 11 SQLite tables |
| Who talks to it | dashboard, daily health cron (#143), pilot task-log uploads | Alpine daemon, run wrapper, weblog |
| Live state | 1,715 orphaned `pending` jobs | 4 jobs, 1 workflow |
| Commits since 2026-08-01 | 0 | 8 |

v1 is already dead for orchestration but is still what people see (#191).
As a control plane v2 gives exact timers, no sweepers, no locks and no
dependence on `onclappc02` being up. Its storage is a black box: no SQL access
except through endpoints written in TypeScript, no `psql`, no DuckDB attach, no
join to the lake catalog, no migration framework for DO SQLite, and a 10 GB
single-thread ceiling. v1 is the opposite: a real database (constraints, FKs,
JSONB indexes, Alembic, backups, any client tool), with submissions and curated
annotations already built.

Sample and study metadata (#55, `docs/sample-metadata-design.md`) is:

- relational and many-to-many (sample ↔ study);
- curated and human-reviewed;
- LLM-harmonised, with provenance and controlled vocabularies;
- queried directly by analysts and the ETL;
- joined to pipeline outputs.

## Decision

We will split by concern, not by era.

- **The v2 Worker is the control plane**: dispatch, runs, timers, the event
  sink and task logs. Its `samples` table stays a thin registry (readset id,
  accessions, collection membership).
- **A Postgres catalog service** (v1 cut down) owns study and sample metadata,
  submissions, curated annotations, users, and the historical-metrics tier.
  It reads live orchestration state from v2 over HTTP.
- **The historical tier is DuckDB over the R2 NDJSON**, served from the same
  Python service, which already holds the DuckDB/DuckLake code in `etl/`.
- **The shared key is the readset id** ([0007](0007-readset-identity.md)),
  computed identically on both sides. `sample_id` stays as an alias during the
  transition.
- **Registration writes through**: the catalog writes to v2 `POST /api/samples`.
  v2 never calls the catalog to dispatch.
- **Harmonisation reads from the catalog and never writes to it**
  (`docs/sample-metadata-design.md`). Its output lands as curated annotations
  through the catalog's own write path.
- **v1 dispatch, lifecycle, runs, daemons, process-metrics and sweeper
  endpoints retire** once the dashboard reads v2.

## Alternatives considered

- **Everything in the Worker.** Turns the component that is good at timers into
  a catalog database. Durable Object and D1 SQLite are unreachable by SQL tools,
  and one object is 10 GB and single-threaded. Rejected.
- **Fold dispatch back into Postgres.** Reinstates the three sweepers,
  `FOR UPDATE SKIP LOCKED` and the single-host dependency that 0006 removed.
  Rejected.
- **Postgres behind Hyperdrive, called from the Worker.** Keeps the sweepers
  and locks, and puts a regional database on the dispatch path. Rejected, as in
  0006.
- **A container behind the Worker, or DuckDB-WASM, for the historical tier.**
  Rejected in favour of the Python service, which already has the DuckDB code.

## Consequences

- #175 (historical tier) and #180 (submissions and curated in v2) are resolved
  by this decision: neither is built in the Worker.
- The v1 API retires route by route as the catalog answers its routes; the
  dashboard repoints to v2 for runs and to the catalog for metadata.
- Two languages remain: TypeScript for the Worker, Python for everything else.
- Two stores share one key. The write-through is the only coupling from the
  catalog to v2; v2 does not depend on the catalog to dispatch.
- Postgres still needs a backup strategy (#84) and OAuth sessions for the
  catalog service.

## References

- Decision: #170 (comment of 2026-10-07). Plan:
  `docs/plan-v2-cutover-and-metadata-phase1.md`.
- Work: #193, #194, #195, #196, #197.
- [0006](0006-cloudflare-control-plane.md) (amended),
  [0007](0007-readset-identity.md), `docs/sample-metadata-design.md`.
