# STATUS — nf_telemetry v2 (Cloudflare control plane)

_Card written 2026-08-18. This is the baseline the next `/orient` diffs against —
update it rather than starting a fresh one, so `What changed` keeps working._

v2 is deployed and has run a full job lifecycle in production, but nothing points
at it, and the next real question is whether the reset primitive lands before the
local end-to-end loop can start.

## Current state

| | | anchor |
|---|---|---|
| ✓ | Worker deployed and answering; full lifecycle verified in prod — claim → submitted → running → MARK_COMPLETE → completed, with ledger, telemetry NDJSON and archive objects in R2 | health 200 @ `nf-telemetry.seandavi.workers.dev` |
| ✓ | 13 tests green, typecheck clean; alarm paths driven by `runDurableObjectAlarm` | `cf/test/lifecycle.test.ts` |
| ✓ | Catalog migrated — 1,727 samples + `cmgd_nextflow 2.2.1` active, multi-collection membership round-trips | `/api/admin/stats` → 1,729 (2 are smoke) |
| ✓ | Map charted: 11 tickets, native sub-issues + blocking edges, 5 takeable, 1 resolved | [#170](https://github.com/seandavi/nextflow_telemetry/issues/170) |
| ✓ | `cf/` committed | this commit |
| → | Deployment carries test residue: 2 smoke samples, retired `smoke` workflow, `PROBE`/`PROBE2` phantoms in live metrics | `/api/metrics/processes/running` |
| → | `ControlDO.reset()` / `SinkDO.reset()` written, no caller, not deployed | `cf/src/control-do.ts`, `cf/src/sink-do.ts` |
| ○ | No jobs exist → nothing can dispatch; both HPC daemons still on v1 | v1 health 200 |
| ○ | 8 analytical endpoints return 501 (historical tier) | [#175](https://github.com/seandavi/nextflow_telemetry/issues/175) |
| ○ | v2 spec lives only in conversation; no ADR records a v2 decision | `docs/adr/` ends at 0005 |

## Key decisions

- **One ControlDO holds all relational state** → rules out the spec's `DispatchDO`
  sharding. Measured 225 B/job-row puts the 10 GB cap at ~44M rows, and every job
  write is scoped by `workflow_pk` so a later split stays mechanical.
- **RunDO alarms replace Workflows and all three cron sweepers** → rules out
  per-step durable retries. `requeue-expired` / `expire-stale-runs` /
  `heartbeat-watchdog` are no-ops returning v1-shaped bodies.
- **NDJSON on R2, not Pipelines** → rules out Parquet-native queries until
  compaction; neither Cloudflare token in GSM carries Pipelines scope.
- **Reprocess, carry no job history** ([#171](https://github.com/seandavi/nextflow_telemetry/issues/171))
  → rules out any import path. Every job dispatches on first poll (50 trimmed,
  1,727 full).
- **Destination is "v2 stands on its own"** → rules out cutover sequencing and v1
  decommission for this map.
- **`job_counts` maintained by SQLite trigger** → rules out call-site bookkeeping;
  job-summary is a 6-row read, not a 100k-row scan.

## Your attention

1. **Push this branch.** The commit fixes "one working tree" only once it leaves
   this host. Until then a lost machine is still a lost control plane.
2. **Take [#172](https://github.com/seandavi/nextflow_telemetry/issues/172) (reset).**
   Unblocks [#181](https://github.com/seandavi/nextflow_telemetry/issues/181) and
   [#178](https://github.com/seandavi/nextflow_telemetry/issues/178); until it
   exists the deployment keeps accreting test residue and the corpus stays 1,727
   instead of the intended 50.
3. **Delete `migrate_from_v1.py --jobs` and `JOBS_SQL`.** It calls
   `/admin/import-jobs`, which #171 decided will never exist — a flag that looks
   like it migrates completion state and silently won't.

## Open questions

- **Historical tier**: DuckDB-WASM direct to R2, a DuckDB container, or defer?
  Decides whether 8 endpoints and the dashboard charts return, and whether the
  bucket needs public read. (#175)
- **Dashboard auth**: a static bearer token can't be used from a browser.
  Cloudflare Access, rebuild Google OAuth, or leave reads open? (#176)
- **`/submissions` and `/curated`**: rebuild, keep a v1 remnant, or retire in
  favour of a script? `nf-client add-study` breaks against v2 until settled. (#180)

## Important references

- [#170](https://github.com/seandavi/nextflow_telemetry/issues/170) — the map;
  every ticket carries acceptance criteria and blocking edges
- `cf/README.md` — deviations from the v2 spec with reasoning, cost shape, endpoint status
- `cf/docs/diagrams.md` — ERD + Durable Object topology
- `docs/adr/` — v1 decisions 0000–0005; nothing yet for v2
- `docs/roadmap.md` — v1 backlog, explicitly out of scope for this map
