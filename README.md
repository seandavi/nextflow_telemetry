# Nextflow Telemetry

A dispatch and telemetry server for the curatedMetagenomics Nextflow pipeline. It ingests
real-time execution events from Nextflow, tracks sample-level processing outcomes, and
presents the results through a live React dashboard.

![Dashboard overview](docs/dashboard-overview.png)

---

## Dashboard

The dashboard is the primary interface for the cMGD team to monitor pipeline progress and
diagnose problems. It auto-refreshes every 30 seconds (configurable).

### Overview

The landing page gives a pipeline health summary for the last 30 days:

- **KPI cards** — total task runs, success rate, failure count, retry rate, and retry
  recovery rate across all Nextflow processes
- **In Flight** — live counts of tasks currently executing or queued in SLURM, broken down
  by process name. Updates each poll cycle so you can watch a batch progress in real time.
- **Top Failing Processes** — processes ranked by absolute failure count, with bar-chart
  failure rates. The most actionable view for triage.
- **Exit Codes** — frequency chart of the most common failure exit codes (e.g. 137 = OOM
  kill, 1 = generic error) to identify systemic resource or configuration problems.
- **Event Mix** — donut chart of raw Nextflow weblog event types (process_submitted,
  process_started, process_completed, etc.) — useful for spotting stalls.
- **Most Retried Processes** — processes that most often fail and get retried, with counts
  of how many retries ultimately recovered vs. exhausted all attempts.

### Process Metrics

Detailed task-level analytics with four tabs:

| Tab | What it shows |
|-----|--------------|
| **Failures** | Every process ranked by failure rate, with success/failure counts and most common exit code |
| **Retries** | Retry breakdown by attempt number and by process — how often does a second try succeed? |
| **Resources** | CPU and memory utilisation (average and P95) vs. what was requested, plus disk I/O |
| **Signatures** | Heatmap of (process × exit code) combinations — reveals whether a failure mode is process-specific or global |

### Workflows

Registry of pipeline versions. Each workflow card shows:

- Status (active / paused / retired) with colour coding
- A progress bar of jobs across the full sample queue: pending → running → completed / failed / dead-letter
- Configuration detail (repository, revision, profile, max retries) on expand

### Samples

Paginated catalog of all registered BioSample IDs with metadata and substring filtering by
sample_id or cohort.

### Cohorts

Collection-level summary (`/api/cohorts`) for any registered group of samples — a
BioProject, an SRA Study, or a manually-tagged cohort. Each cohort shows:

- Completion percentage and counts (pending / claimed / submitted / running / completed /
  failed) across the cohort's samples for a chosen workflow
- A failure-by-process bar chart: which Nextflow process is killing samples, and how many
- Click a process row to drill down to the failing task list, with deep-links into the
  log viewer

---

## Key Concepts

**Sample** — a BioSample ID (e.g. `SAMN01234567`) plus associated NCBI accessions and
metadata. Each sample is processed once per active workflow version.

**Job** — one processing attempt for a (sample, workflow version) pair. Lifecycle:
`pending → claimed → submitted → running → completed | failed`. The gap between `submitted`
(executor accepted) and `running` (Nextflow actually started) is the scheduler queue
wait — kept separate so dashboards can distinguish a slow pipeline from a slow cluster.
A job can be retried up to `max_retries` times before being written to the dead-letter
table.

**Workflow run** — a single Nextflow execution that processes a batch of samples together.
The server dispatches runs in configurable batch sizes.

**Process metrics** — task-level data from individual Nextflow process executions
(`FETCH_READS`, `PROFILE_TAXA`, etc.). A sample's job can succeed even if some tasks failed
and were rescued by retry. Process metrics and job outcomes are complementary views.

**MARK\_COMPLETE** — the pipeline's completion sentinel. When the `MARK_COMPLETE` process
fires successfully for a sample, that sample's job is marked `completed`. If a run ends
without it, the job is failed (or re-queued if retries remain).

---

## Architecture

```
                        ┌─────────────────────────────────┐
                        │       HPC cluster (Anvil/Alpine) │
                        │                                  │
  ┌──────────────┐      │  ┌───────────┐  SLURM submit    │
  │  nf-client   │──────┼─►│ sbatch    │──────────────►   │
  │  (daemon on  │      │  │ wrapper   │                   │
  │  head node)  │◄─────┼──│ job       │◄── Nextflow       │
  └──────┬───────┘      │  └───────────┘    per-sample     │
         │              │        │          tasks           │
         │ claim/report │        │ -with-weblog             │
         ▼              └────────┼────────────────────────-─┘
  ┌──────────────────────────────▼──────────────────┐
  │              FastAPI server + PostgreSQL          │
  │                                                  │
  │  /telemetry  ◄── Nextflow weblog events          │
  │  /runs/…/event ◄── wrapper + pipeline hook events│
  │  /dispatch   ◄── nf-client claims & reports      │
  │  /metrics    ──► dashboard queries               │
  │  /cohorts    ──► cohort summaries + drill-down   │
  │  /samples    ──► sample catalog                  │
  └──────────────────────────┬───────────────────────┘
                             │
                      ┌──────▼──────┐
                      │  React UI   │
                      │  dashboard  │
                      └─────────────┘
```

**Data flow**

1. Samples are registered in the server's catalog (BioSample IDs + NCBI accessions).
2. `nf-client` (running as a daemon on each HPC head node — Anvil and Alpine) claims
   batches of pending samples from the server and submits a SLURM wrapper job for each
   batch.
3. The wrapper job (`nf_client.run_wrapper`) emits run-lifecycle events to
   `/api/runs/{run_name}/event` (wrapper_started, pre_nextflow with queue wait,
   periodic heartbeats), then runs Nextflow under instrumentation.
4. Nextflow submits individual compute tasks (downloading reads, taxonomic profiling,
   etc.) back to SLURM via the `process.executor = 'slurm'` setting, and posts real-time
   weblog events to `/telemetry` as each task starts and finishes.
5. When the `MARK_COMPLETE` sentinel process fires for a sample, the server marks that
   sample's job as `completed`. If the run ends without it, the job is swept to `failed`
   (or re-queued, within the workflow's retry budget).
6. On exit, the wrapper posts `wrapper_exited` and uploads the `.nextflow.log` so the
   diagnosis surface is complete even when the run dies before any weblog event.
7. The dashboard polls the server's metrics, cohort, and status endpoints to render live
   progress.

**Storage** — all data lives in PostgreSQL. Raw Nextflow events are stored as JSONB in the
`telemetry` table; sample, workflow, and job state live in their own relational tables.
Process-level metrics are computed at query time from the raw event stream, with partial
functional indexes on `(trace->>'process')` and `(trace->>'status')` keeping the
analytical queries fast. Metrics endpoints apply a 7-day default look-back when no time
filter is supplied, so unparameterised calls stay bounded as event volume grows.

---

## Development

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node 18+ with npm
- PostgreSQL (or use the Docker Compose stack)
- [just](https://github.com/casey/just) command runner

### Quick start

```bash
uv sync --group dev      # install Python dependencies
just up-db               # start PostgreSQL via Docker Compose
just migrate             # run Alembic migrations
just run                 # start the API server (hot reload)

cd frontend && npm install && npm run dev   # frontend dev server
```

### Common commands

```bash
just help       # list all commands
just check      # typecheck + tests
just ci         # full CI gate (sync --frozen + mypy + pytest)
just seed       # seed sample catalog from ArtachoA_2021_sample.tsv
```

### API reference

Interactive OpenAPI docs are available at `/docs` when the server is running.

| Group | Endpoints |
|-------|-----------|
| Telemetry ingest | `POST /telemetry` (Nextflow `-with-weblog`) |
| Run-lifecycle events | `POST /api/runs/{run_name}/event` (wrapper, pipeline hooks, daemon sacct polling) |
| Dispatch | `POST /dispatch/batch`, `/dispatch/submitted`, `/dispatch/requeue-expired` |
| Samples | `GET/POST /samples`, `GET /samples/{id}`, `GET /samples/by-srr/{srr}`, `GET /samples/by-biosample/{id}` |
| Workflows | `GET/POST /workflows`, `PATCH /workflows/{pk}/status`, `/revision`, `GET /workflows/{pk}/job-summary` |
| Cohorts | `GET /cohorts`, `/cohorts/{id}/summary`, `/cohorts/{id}/failures` |
| Process metrics | `GET /metrics/processes/running`, `/summary`, `/failures`, `/retries`, `/resources-by-attempt`, `/failure-signatures`, `/timeline`, `/tasks` |
| Task logs | `POST /task-logs`, `GET /task-logs/{run_name}/{task_hash}` |
| Daemons | `GET /daemons/`, `POST /daemons/heartbeat` |
| Curated | `GET/POST /curated/studies`, `/curated/samples` |
| Admin | `POST /admin/reconcile-jobs`, `/admin/expire-stale-runs`, `GET /admin/stats` |

### nf-client (HPC orchestration)

The `nf-client` CLI dispatches Nextflow runs against the server's job queue. It is only
needed by whoever operates the HPC submission daemon — not by dashboard consumers.

```bash
uv pip install -e packages/nf_client
nf-client daemon --config client-alpine.yaml
```

The daemon's SLURM template invokes `python -m nf_client.run_wrapper` rather than
`nextflow run` directly. The wrapper emits run-lifecycle events (wrapper_started,
pre_nextflow with queue wait, periodic heartbeats, wrapper_exited with exit code and
`.nextflow.log` upload), all best-effort and incapable of failing the run. This makes
crashes visible end-to-end — including pre-Nextflow failures (module load, container
pull) that the weblog stream can't see.

For full Alpine / Anvil SLURM deployment details see
[docs/hpc-deployment.md](docs/hpc-deployment.md).

### Test pipeline

`nf_testing/main.nf` is a stub metagenomics pipeline (no real tools required) that
exercises the full telemetry contract. `v0.2.0` includes a `STOCHASTIC_STEP` that fails
with configurable probability (default 30%) to generate realistic retry telemetry.

---

## Production Deployment

The production stack runs on Google Cloud:

| Component | Service |
|-----------|---------|
| API | Cloud Run (`nf-telemetry.cancerdatasci.org`) |
| Frontend | Firebase Hosting (`curatedmetagenomicdata.web.app`) |
| Database | Cloud SQL (PostgreSQL) |

A self-hosted alternative is in flight — see `deploy/onclappc02/` for the Docker Compose
stack that runs the same image behind Traefik on the existing
`pg_main` / Postgres-18 instance. Cutover plan tracked in the project's GitHub issues
(meta issue: self-host migration).

### Prerequisites

- `gcloud` CLI authenticated to the target project
- `firebase` CLI (`npm install -g firebase-tools`)
- Docker (for local image builds)
- [just](https://github.com/casey/just)

### GCP setup (one-time)

1. Enable APIs: Cloud Run, Artifact Registry, Cloud Build, Secret Manager.
2. Create an Artifact Registry Docker repository named `nextflow-telemetry`.
3. Create a service account for the Cloud Run service and grant it:
   - `roles/secretmanager.secretAccessor`
   - `roles/cloudsql.client` (if using Cloud SQL IAM auth later)
4. Store the Cloud SQL connection string in Secret Manager:
   ```bash
   echo -n "postgresql://user:pass@HOST:5432/dbname" | \
     gcloud secrets create nextflow-telemetry-db-uri --data-file=-
   ```
5. Update the placeholder values in `deploy/cloudrun.yaml` and `deploy/.firebaserc`:
   - `YOUR_GCP_PROJECT` → your GCP project ID
   - `YOUR_REGION` → e.g. `us-central1`
   - `YOUR_FIREBASE_PROJECT` → your Firebase project ID
   - Service account name in `cloudrun.yaml`

### Database migration

Run Alembic against Cloud SQL before the first deploy and after any schema change:

```bash
SQLALCHEMY_URI=postgresql://user:pass@HOST:5432/dbname just migrate-prod
```

Cloud SQL uses a public IP, so this can be run directly from a machine with network
access to the instance (no Cloud SQL Proxy required).

### Deploying the API

```bash
export GCP_PROJECT=your-project
export REGION=us-central1

just build-api       # build image and push to Artifact Registry
just deploy-api      # update the Cloud Run service
```

The `deploy/cloudrun.yaml` service definition pulls `SQLALCHEMY_URI` from Secret Manager
and sets `CORS_ORIGINS` to the frontend's domain (see DNS section below).

### Deploying the frontend

```bash
just deploy-frontend   # vite build + firebase deploy
```

Firebase Hosting serves the React SPA with a global CDN, automatic HTTPS, and a catch-all
rewrite to `index.html` for client-side routing. Static assets under `/assets/` are cached
with immutable headers; `index.html` itself is not cached so new deploys take effect
immediately.

### DNS

The system uses two stable public hostnames, both custom domains rather than the default
Cloud Run / Firebase subdomains so that HPC-submitted jobs always reach the right endpoint
regardless of future infrastructure moves.

| Hostname | Currently points to | Purpose |
|----------|---------------------|---------|
| `nf-telemetry.cancerdatasci.org` | Cloud Run (custom domain mapping) | All API traffic, including Nextflow weblog and wrapper-event POSTs from HPC |
| `cmgd.cancerdatasci.org` | Firebase Hosting (custom domain) | React dashboard |

**To add a custom domain to Cloud Run:**
```bash
gcloud run domain-mappings create \
  --service nf-telemetry \
  --domain nf-telemetry.cancerdatasci.org \
  --region $REGION
```
Cloud Run will issue a managed TLS certificate automatically. Add the CNAME shown in the
output to your DNS provider (Cloudflare, DNS-only on the campus network).

**To add a custom domain to Firebase Hosting:** run `firebase hosting:channel:deploy` or
use the Firebase console; Firebase provisions the cert and provides the DNS records.

> **Important — weblog URL stability:** the weblog URL (`-with-weblog <url>`) is baked
> into SLURM scripts at submission time. Changing the API hostname mid-batch will silently
> drop telemetry for all in-flight jobs. Always keep the old hostname resolving (or
> redirect it) until all running Nextflow jobs have finished before cutting DNS over.
> The same goes for the wrapper events that `nf_client.run_wrapper` posts.

### CORS

The API reads allowed origins from the `CORS_ORIGINS` environment variable
(comma-separated). Set this in `deploy/cloudrun.yaml` to the frontend's domain:

```
CORS_ORIGINS=https://cmgd.cancerdatasci.org
```

Nextflow weblog POSTs and `nf_client.run_wrapper` events are server-to-server and are not
subject to CORS, so the HPC endpoint does not need to be in the allowlist.
