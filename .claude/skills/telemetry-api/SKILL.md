---
name: telemetry-api
description: >
  Query the Nextflow telemetry/orchestration backend — job-summary, runs,
  dispatchability, reconcile, requeue. Use when checking job/run status,
  counting pending/running/failed jobs, diagnosing why work isn't dispatching,
  or kicking off runs. Trigger words: telemetry, job summary, job-summary,
  check runs, run status, dispatchability, reconcile, requeue, DLQ, dead letter,
  how many running, is it stuck.
---

# Telemetry API

The orchestration backend for the Nextflow telemetry system. This skill is the
shared reference for talking to it; `cmgd-triage`, `cmgd-release`, and
`alpine-daemon` build on it.

## Base URL

Production: `https://nf-telemetry.cancerdatasci.org`
Local dev:  `http://localhost:8000`

All app routes are under `/api`. The single exception is `/telemetry` (the
Nextflow `-with-weblog` sink) which is **unauthenticated by design** — Nextflow's
weblog cannot send auth headers.

## Endpoint map

Path = base + `/api` + router-prefix + route.

### workflows (`/api/workflows`)
- `POST /` — register/update a workflow (keyed on `workflow_id`+`version`)
- `GET  /` — list; filter `?status=active|paused|retired`
- `GET  /{pk}` — one workflow
- `GET  /{pk}/job-summary` — **authoritative** job counts (see Gotchas)
- `PATCH /{pk}/status` — body `{"status":"active|paused|retired"}`
- `PATCH /{pk}/revision` — body `{"revision":"<git-tag>"}`

### admin (`/api/admin`)
- `POST /reconcile-jobs` — create pending jobs = samples × active workflows; returns `{"jobs_created":N}`
- `GET  /dispatchability` — which workflows have pending jobs but no daemon to claim them
- `GET  /stats` — global counts
- `POST /requeue-dead-letter` — move DLQ jobs back to pending
- `POST /reset-running` — force running→pending (recovery)
- `POST /expire-stale-runs?older_than_hours=2.0` — close zombie runs, sweep their jobs
- `POST /close-run` — close a single run by name

### runs (`/api/runs`)
- `GET  /` — list runs newest-first, each with derived `classification`
- `GET  /{run_name}` — full row + `task_status_counts` + log availability
- `POST /{run_name}/event` — lifecycle events (clients use this, not you)

### task-logs (`/api/task-logs`)
- `GET  /{run_name}/{task_hash}` — per-task logs; `?log_type=command_err|command_out|command_sh|command_log`

### daemons (`/api/daemons`)
- `GET  /` — registered daemons + last heartbeat
- `DELETE /{agent_id}` — deregister

### dispatch (`/api/dispatch`) — clients only
- `POST /batch` `/submitted` `/requeue-expired`

### process metrics (`/api/metrics/processes`) — per-process aggregates

## Quick recipes

```bash
API=https://nf-telemetry.cancerdatasci.org

# Active workflows + their pks (pk is the {workflow_pk} in PATCH/job-summary)
curl -s "$API/api/workflows?status=active" | python3 -m json.tool

# AUTHORITATIVE job counts for workflow pk 12
curl -s "$API/api/workflows/12/job-summary" | python3 -m json.tool

# Anything stuck? (pending jobs with no daemon to claim them)
curl -s "$API/api/admin/dispatchability" | python3 -m json.tool

# Kick off newly-created work
curl -s -X POST "$API/api/admin/reconcile-jobs"
```

## Gotchas

- **`/api/runs/` ignores `limit`, `workflow_pk`, and `revision` query params and
  hard-caps at ~58 newest rows.** Do NOT use it to count runs for a workflow —
  the count will silently be wrong. For counts, always use
  `/api/workflows/{pk}/job-summary`. `/api/runs/` is only good for eyeballing the
  most-recent runs' `classification`.
- **Job-summary `running` ≠ number of run records returned by `/api/runs/`.**
  These come from different tables; the runs-list cap above is why they disagree.
  Trust job-summary for "how many are running."
- **`{pk}` is the integer `id`, not the `workflow_id` string.** `cmgd_nextflow` is
  the workflow_id; its active 2.0.6 row is pk 12. Get the pk from
  `GET /api/workflows`.
- **`reconcile-jobs` is a cross-product of samples × *active* workflows.** Retire
  old revisions (`PATCH /{pk}/status {"status":"retired"}`) BEFORE reconciling or
  you double-dispatch every sample across both versions.
- **Dispatchability `stuck` often lists `nf_testing` (pk 1).** That's the dead
  `onclappc02` daemon — pre-existing, irrelevant to cmgd. Only act on
  `cmgd_nextflow` appearing there.
- JSONB status lives at `trace->>'status'`; if writing server SQL, bind the
  `.astext` expression once and reuse it (raw GROUP BY on the JSON path errors).

## Run classification (from `runs.py`)

`/api/runs/` annotates each run with one of:
`active` · `stalled` · `completed` · `failed` · `expired` · `wrapper-failed` · `ended-no-log`

Decision order: `wrapper_exit_code` non-zero → `wrapper-failed`; else non-terminal
+ heartbeat older than 15 min → `stalled`; else terminal but no `.nextflow.log`
uploaded → `ended-no-log`. See `cmgd-triage` for what each means and what to do.
