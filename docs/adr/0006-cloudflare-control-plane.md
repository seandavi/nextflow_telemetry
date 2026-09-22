# 0006. Replace the FastAPI + Postgres server with a Cloudflare control plane

- **Status:** Accepted (running in parallel with v1; cutover not yet sequenced)
- **Date:** 2026-08-18, recorded 2026-09-21
- **Deciders:** Sean Davis

## Context

v1 is a FastAPI server and a Postgres database on a single host (`onclappc02`,
after Cloud Run was retired in May 2026). It works, but three of its mechanisms
exist only because a stateless server cannot hold a timer or a lock:

- Three cron sweepers (`requeue-expired`, `expire-stale-runs`,
  `heartbeat-watchdog`) scan for runs that should have ended.
- Claims use `FOR UPDATE SKIP LOCKED` to serialise the two HPC daemons.
- Postgres is both the live control plane and the analytics store, so every
  weblog event is a JSONB row and the dashboard's metrics queries scan them.

The HPC side (`nf-client`, the SLURM run wrapper, Nextflow's weblog reporter)
is hard to redeploy and cannot be changed casually. Whatever replaces v1 must
keep every path, method and payload those three call.

A written spec (draft 0.1, 2026-08-17) proposed Workers + Durable Objects +
Workflows + Pipelines + R2, with one DispatchDO per workflow version, four
registry DOs, a Workflow instance per run, and a content-hashed ledger with a
daily anti-entropy diff.

## Decision

We will run the control plane as one Cloudflare Worker with three Durable
Object classes and one R2 bucket, in `cf/`, wire-compatible with v1:

- **One ControlDO** holds every relational table and owns every job and run
  status write. A Durable Object is single-threaded and its SQLite calls are
  synchronous, so any method that does not `await` is a transaction. There is
  no locking anywhere.
- **One RunDO per run** holds the deadline for whatever phase the run is in
  (claim TTL, submit backstop, liveness) as the object's single alarm, and
  absorbs wrapper heartbeats. When the alarm fires it calls ControlDO. The
  three v1 sweeper endpoints become no-ops that return v1-shaped bodies.
- **One SinkDO** buffers every weblog and run event and flushes gzipped NDJSON
  to R2 partitioned by UTC date. ControlDO keeps only live state; R2 is history.
- `job_counts` is maintained by SQLite triggers so summaries are a six-row read.
- Jobs are never migrated. v2 reprocesses from scratch (#171).
- Bearer auth applies to every write except `/telemetry` and
  `/runs/{run}/event`, because neither the weblog reporter nor the wrapper can
  carry a token and v1 required none there.

## Alternatives considered

- **The spec as written.** DispatchDO sharding solves contention a single DO
  does not have, and breaks the samples-collections-jobs join that cohort
  summaries need. Measured 225 bytes per job row puts one object's 10 GB cap at
  about 44M jobs. A Workflow instance plus a RunDO is two objects for one timer.
  Anti-entropy diffs two independent stores; with one authoritative DO the
  ledger is derived and a daily snapshot is the useful artifact instead.
- **Pipelines to Parquet.** Neither Cloudflare token in `cdsci-infra` carries
  Pipelines scope. NDJSON in the same partition layout is readable by DuckDB
  today and can be compacted later.
- **Keep Postgres via Hyperdrive.** Keeps the sweepers and the lock, and keeps
  the database as both control plane and analytics store, which is the problem.
- **Stay on v1.** Viable. The cost is continued operation of a single host and
  the three sweepers, and analytics scans that grow with event volume.

## Consequences

- Claim expiry and heartbeat death are exact: verified on the deployed worker
  at claimed + 5:00 and last heartbeat + 10:00 (#181).
- The seven analytical `/metrics/processes/*` endpoints and
  `/cohorts/{id}/failures` return 501 until a historical tier exists over the
  NDJSON (#175). `/submissions`, `/curated` and OAuth are not built (#176, #180).
- Every heartbeat is two billed DO writes (`put` + `setAlarm`). Inside the
  included allowance at current scale; a fixed-cadence alarm is the upgrade if
  it ever shows on the bill.
- v2 state is disposable until cutover. `POST /admin/reset`, guarded by
  `ALLOW_RESET`, is the dev loop (#172). A production deploy must set it to
  `"false"`.
- The daemon config on each cluster needs `token:`; the wrapper needs nothing.
- Rollback at any point is a base-URL change on the cluster, because the wire
  protocol is unchanged.

## References

- Map: #170. Corpus decision: #171. Reset: #172. Local loop: #181, #183.
- `cf/README.md` (deviations, endpoint status, cost), `cf/docs/diagrams.md`
  (ERD, topology, sequences, state machines), `cf/STATUS.md`.
- Commits 6aa1e36, e47a37a, 72ad527 on `feat/cf-control-plane`; PR #182.
