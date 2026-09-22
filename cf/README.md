# nf_telemetry v2 — Cloudflare-native control plane

A drop-in replacement for the FastAPI + Postgres server in `src/nextflow_telemetry/`,
built on Workers + Durable Objects + R2. **The wire protocol is unchanged**: nf-client,
the SLURM run wrapper, the Nextflow weblog hook and the dashboard only need a new base URL.

```
HPC (Anvil / Alpine) — unchanged
nf-client · run_wrapper · nextflow -with-weblog
                 │ HTTPS, v1 paths
                 ▼
        ┌──────────────────┐
        │  API Worker      │  Hono; parse → route. No business logic.
        └───┬─────┬─────┬──┘
            │     │     │
   ┌────────▼─┐ ┌─▼────────┐ ┌▼──────────┐
   │ControlDO │ │ RunDO    │ │ SinkDO    │
   │ (single) │ │ per run  │ │ (single)  │
   │ samples  │ │ deadline │ │ event     │
   │ workflows│ │ alarm +  │ │ buffer +  │
   │ jobs     │ │ heartbeat│ │ process   │
   │ runs     │ │ absorber │ │ counters  │
   │ DLQ      │ └────┬─────┘ └────┬──────┘
   │ daemons  │◄─────┘            │
   └────┬─────┘  closes the run   │
        │                         │
        ▼                         ▼
   R2: ledger/ snapshots/    R2: telemetry/events/dt=…/*.ndjson.gz
       nextflow-logs/ task-logs/
```

## Commands

```bash
npm install
npm test          # vitest + real DOs via miniflare — the full job lifecycle
npm run typecheck
npm run dev       # local server on :8787
npm run deploy    # needs the R2 bucket to exist first (see below)
```

First deploy:

```bash
export CLOUDFLARE_API_TOKEN=$(gcloud secrets versions access latest \
  --secret=cdsci-cloudflare-workers-token --project=cdsci-infra)
npx wrangler r2 bucket create nf-telemetry
npx wrangler deploy
npx wrangler secret put API_TOKEN     # optional; enforces bearer auth on writes
```

Then point a client at it — the only change on the HPC side:

```yaml
server_url: https://nf-telemetry.<subdomain>.workers.dev/api
weblog_url: https://nf-telemetry.<subdomain>.workers.dev/telemetry
```

Every route is served at both `/` and `/api`, matching v1's split mount.

**API reference:** [`/docs`](https://nf-telemetry.seandavi.workers.dev/docs) (Scalar) over
[`/openapi.json`](https://nf-telemetry.seandavi.workers.dev/openapi.json). The document is
built from the router at request time by `hono-openapi`, so it cannot list a route that does
not exist; `test/openapi.test.ts` fails if a route exists that it does not list. Request and
response shapes are Zod in `src/schemas.ts`, and the lifecycle suite parses live responses
through them, so a handler that changes shape fails a test before it breaks a client (#184;
the rest of the contract work, generated TS types and validation on every route, is #173).

## The dev loop

v2 state is disposable until cutover. Teardown and rebuild is the supported
cycle, not a workaround:

```bash
B=https://nf-telemetry.seandavi.workers.dev
T=$(gcloud secrets versions access latest --secret=cdsci-nf-telemetry-v2-api-token --project=cdsci-infra)

curl -s -X POST -H "Authorization: Bearer $T" $B/api/admin/reset   # wipe everything
scripts/migrate_from_v1.py --limit 50                              # light corpus
scripts/migrate_from_v1.py                                         # or the full catalog
```

`POST /admin/reset` empties all three Durable Object classes and deletes every
object under the R2 prefixes this service writes. It finalises each RunDO before
wiping the `runs` rows — a RunDO is addressed by `run_name`, so once the rows are
gone nothing knows which timers to cancel.

It is guarded by `ALLOW_RESET`, which must be exactly the string `"true"`; the
check fails closed on any other value, including unset. That is deliberately a
separate control from the bearer token, which every other write route shares.
**A production deployment must set `ALLOW_RESET` to `"false"`.**

Jobs are never migrated (issue #171) — v2 reprocesses from scratch, so run
`POST /api/admin/reconcile-jobs` when you want work to become dispatchable.

### Local end-to-end (the pre-cluster gate, #181 / #183)

The recommended way to seed a corpus is the same command an operator uses, not
the migration script:

```bash
just v2-e2e                       # reset → seed via nf-client → happy path + 3 failure shapes
just v2-run test,fail-mark SRR…   # one sample, one run, one profile
```

`v2-seed` loads `ArtachoA_2021_sample.tsv` and one curatedMetagenomicData study
through unmodified `nf-client add-samples` / `add-cmd`, which is the #183
acceptance. Cohorts have no write endpoint in either version: a collection
exists once a sample is registered into it.

Verified 2026-09-21 against the deployed worker: 94-sample happy path, `fail-mark`
and `fail-fetch` (requeue once, then dead-letter at `max_retries=1`),
`stochastic` (Nextflow task retries leave `retry_count` at 1), claim expiry at
exactly `CLAIM_TTL_MINUTES`, liveness closure at exactly last heartbeat +
`LIVENESS_MINUTES` with reason `presumed dead`, `job_counts` equal to the jobs
scan afterwards. Two things the recipe does not automate:

- **Timers.** Claim expiry: claim with `curl POST /api/dispatch/batch`, never
  confirm, wait 5 min. Liveness: `nf-client run-wrapper --run-name R
  --heartbeat-seconds 5 -- sleep 900`, `kill -9` the wrapper, wait 10 min.
- **The wrapper.** `submission.mode: local` runs nextflow directly, so wrapper
  events, `.nextflow.log` upload and `wait_seconds` only appear when the run
  wrapper runs, as it does under SLURM. Drive it by hand as above, wrapping the
  real `nextflow run` command. Task logs are only posted by `nf-client
  upload-logs`, never by the daemon.

Things the loop found, so you do not find them again:

- `nf-client` must run from its own venv (`uv run --project packages/nf_client`);
  the root venv's click 8.3 breaks typer 0.12 option parsing.
- The run wrapper carries no bearer token, and v1 never required one on
  `POST /runs/{run}/event`. v2 exempts that route (`authExempt` in `index.ts`).
  The daemon does need `token:` in its client yaml when `API_TOKEN` is set.
- `add-samples --reconcile` and `daemon` with `continuous: false` drain every
  pending job, so a one-run test needs exactly one pending job.

## How this differs from the v2 spec, and why

The spec's shape was followed where it earns its keep and collapsed where it
didn't. Each deviation below removes a moving part without changing observable
behaviour.

**One ControlDO instead of DispatchDO-per-workflow-version + four registry DOs.**
The sharding in the spec exists to spread claim contention, but contention is
what a Durable Object already eliminates: it is single-threaded, and
`storage.sql.exec` is synchronous, so any method that doesn't `await` mid-way is
atomic by construction. One DO also means samples ⋈ collections ⋈ jobs stay
joinable in SQL — cohort summaries and job counts are one query, not a fan-out.
The claim path already scopes every query to one workflow version, so sharding
later is mechanical. `ponytail:` note in `control-do.ts` marks the ceiling.

**No RunWorkflow.** The spec ran a Workflow instance *and* a RunDO per run, with
the DO poking the instance on anomaly. A DO alarm is the same durable timer with
one less object and no cross-object event routing, and it satisfies the same
acceptance criterion (no cron sweepers). RunDO holds the deadline; when it fires
it calls ControlDO, which owns every status write. `closeRun` is idempotent, so
the weblog, the alarm and an operator all reach terminal state through one path.

**NDJSON on R2 instead of Pipelines.** Neither Cloudflare token in `cdsci-infra`
carries Pipelines scope. SinkDO batches events (500 rows or 60s) and flushes
gzipped NDJSON partitioned by UTC date — the same layout Parquet would use, and
DuckDB reads it directly:

```sql
select * from read_json_auto('r2://nf-telemetry/telemetry/events/**/*.ndjson.gz');
```

**Daily snapshot instead of an anti-entropy diff.** The spec's diff reconciles
DO state against the ledger because they were independent stores. With one
authoritative DO the ledger is derived, so there is nothing to diff — what's
actually worth having is the recovery path, which is the cron's job:
`snapshots/YYYY-MM-DD.json.gz` is a full dump of every table.

**wrapper_exited closes the run immediately.** v1 waited for the submit script's
separate `close-run` call. The exit code is authoritative and already in hand, so
waiting out the liveness window buys nothing. `close-run` still works and is a
no-op on an already-closed run.

## Endpoint status

| Route | Status |
|---|---|
| `/dispatch/batch`, `/dispatch/submitted` | full |
| `/telemetry`, `/api/runs/{run}/event`, `/runs`, `/runs/{run}` | full |
| `/samples*`, `/workflows*`, `/daemons*`, `/task-logs*`, `/cohorts`, `/cohorts/leaderboard`, `/cohorts/{id}/summary` | full |
| `/admin/{reconcile-jobs,reset-running,close-run,requeue-dead-letter,dispatchability,stats}` | full |
| `/metrics/processes/running` | full (live counters in SinkDO) |
| `/dispatch/requeue-expired`, `/admin/expire-stale-runs`, `/admin/heartbeat-watchdog` | **deprecated no-ops** — expiry is a per-run timer now. v1-shaped success bodies + `Deprecation: true`. |
| `/metrics/processes/{summary,retries,resources-by-attempt,failures,failure-signatures,tasks,timeline}`, `/cohorts/{id}/failures` | **501** — historical tier, see below |
| `/submissions*`, `/curated*`, `/auth/*` | **not built** — see below |

### Historical tier

The seven analytical endpoints and the cohort failure drill-down all read
per-task history, which is now NDJSON on R2 rather than a Postgres table. They
return 501 rather than an empty result, so a broken dashboard panel is
unambiguous. Building it is a separate, smaller job once events have
accumulated: a read-only DuckDB over the `telemetry/events/` prefix (Container),
or DuckDB-WASM in the browser reading R2 directly. The response shapes are fixed
by `src/nextflow_telemetry/models.py` either way.

`nf-client` commands that hit the unbuilt routes (`add-study`, anything under
`submissions`, the curated endpoints, Google OAuth) still need v1. Everything the
dispatch daemon and run wrapper touch is here.

## Semantics preserved from v1

- Job: `pending → claimed → submitted → running → completed | failed`, dead-letter at retry budget.
- Run: `claimed → submitted → running → completed | expired | failed`.
- Claim TTL 5 min; a claim that expires burns no retry (nothing was attempted).
- `MARK_COMPLETE` sentinel completes a sample the moment it lands; a late one never flips a terminal job.
- Sample identity is the content address of its sorted, deduplicated SRR set.
- Collection membership rows are the truth, never a `metadata.cohort` key (ADR-0005).
- Run classification (`active` / `stalled` / `wrapper-failed` / `ended-no-log` / …) ported verbatim.

## Cost shape

Durable Objects bill rows read ($0.001/M after 25B/month), rows written ($1/M
after 50M/month), requests, and duration. At 100k samples the whole control
plane sits inside the included allowances — ControlDO is ~200–500 MB against a
10 GB per-object cap and a 5 GB-month included allowance, and the lifecycle
writes about 12M rows/month.

The one thing that would *not* have been free is a status `GROUP BY` over the
jobs table: ~100k rows read per call, and a dashboard polling job-summary on a
timer burns the entire 25-billion-row monthly allowance by itself. So
`job_counts` maintains per-(workflow, status) tallies by SQLite trigger, and
`/workflows/{pk}/job-summary`, `/admin/stats` and `/admin/dispatchability` read
those instead — a 6-row read rather than a full scan. The triggers (not
bookkeeping at the call sites) are what make it impossible for one of the six
status-writing statements to forget; `test/lifecycle.test.ts` re-derives the
counts from `jobs` after every test and fails if they disagree.

The other growth term is `jobs` = samples × workflow versions, measured at 225
bytes/row, so the 10 GB per-object ceiling is ~44M job rows. Retired versions'
jobs are archived to `archive/jobs/{workflow_pk}/*.ndjson.gz` and purged by the
daily cron (`POST /admin/archive-retired` to do it now), which bounds the live
table at samples × *active* versions — a permanent ~7% of one object even at 1M
samples. `/admin/stats` reports `storage.pct_of_limit`; splitting jobs into a DO
per workflow version (the spec's `DispatchDO`) is the answer if that ever climbs
past ~50%, and every job write is already scoped by `workflow_pk` or `run_name`
to make that mechanical.

Still full scans, deliberately: `listRuns` and the cohort leaderboard. Both are
bounded by run/collection count rather than sample count, so they are ~10–100×
cheaper — worth revisiting only if the runs table gets big and something starts
polling it hard.

## Tunables

`wrangler.jsonc` vars: `CLAIM_TTL_MINUTES` (5), `SUBMIT_BACKSTOP_HOURS` (48),
`LIVENESS_MINUTES` (10), `CORS_ORIGINS`. Secret `API_TOKEN` — when set, bearer
auth is required on every mutating route except `/telemetry`, which must stay
open because Nextflow's weblog reporter cannot send headers.
