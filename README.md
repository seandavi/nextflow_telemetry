# Nextflow Telemetry

A dispatch and telemetry server for Nextflow pipeline runs. It receives weblog events from
Nextflow, tracks sample-level job outcomes, and exposes process-level metrics through a
React dashboard.

## System Overview

```
┌──────────────┐    claims/reports    ┌─────────────────┐    -with-weblog    ┌──────────────┐
│  nf-client   │◄────────────────────►│  FastAPI server  │◄───────────────────│   Nextflow   │
│  (daemon)    │                      │  + PostgreSQL    │                    │   pipeline   │
└──────────────┘                      └────────┬─────────┘                    └──────────────┘
                                               │
                                        ┌──────▼──────┐
                                        │  React UI    │
                                        │  dashboard   │
                                        └─────────────┘
```

**Server** (`src/nextflow_telemetry/`): FastAPI + SQLAlchemy async + PostgreSQL. Manages
samples, workflows, jobs, dispatch batches, and telemetry events.

**Client** (`packages/nf_client/`): Python CLI that claims job batches from the server,
generates SLURM/PBS/local submit scripts from Jinja2 templates, submits them, and reports back.

**Frontend** (`frontend/`): React + TypeScript (Vite) dashboard showing live data from the API.

**Test pipeline** (`nf_testing/`): Stub Nextflow pipeline that exercises the full
dispatch → weblog → telemetry loop without requiring real bioinformatics tools.

## Data Model

- **Samples**: catalog of biosample IDs with metadata (NCBI accessions, cohort, etc.)
- **Workflows**: registered pipeline versions with repository URL, revision, and profile
- **Jobs**: one row per (sample, workflow version) — tracks `pending → running → completed | failed`
- **Telemetry**: raw Nextflow weblog events (workflow/process started/completed) stored as JSONB
- **Dead letter**: samples that did not receive a `MARK_COMPLETE` event after a run finished

Job completion is driven by a `MARK_COMPLETE` sentinel process in the pipeline. When that
process fires with `status=COMPLETED`, the job is marked complete. If the run ends without it,
the job is swept to failed (or re-queued if retries remain) and written to the dead letter table.

## Building and Running

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node 18+ with npm
- PostgreSQL (or use the Docker Compose stack)
- [just](https://github.com/casey/just) command runner

### Quick start

```bash
# Install Python dependencies
uv sync --group dev

# Start PostgreSQL
just up-db

# Run migrations
just migrate

# Start the API (with hot reload)
just run

# In another terminal, start the frontend dev server
cd frontend && npm install && npm run dev
```

### Common just commands

```bash
just help       # list all commands
just sync       # install dev dependencies
just run        # run API locally with reload
just check      # typecheck + tests
just ci         # CI-equivalent gate (sync --frozen + mypy + pytest)
just seed       # seed 69 samples from ArtachoA_2021_sample.tsv
just migrate    # run alembic migrations
```

## nf-client

The `nf-client` CLI dispatches Nextflow runs against the server's job queue.

```bash
# Install
uv pip install -e packages/nf_client

# Fetch the next pending batch (no submission)
nf-client fetch --config client-local.yaml

# Claim and submit one batch
nf-client submit --config client-local.yaml [--dry-run]

# Run continuously until the queue is empty
nf-client daemon --config client-local.yaml [--batch-size 10]
```

Example `client-local.yaml`:

```yaml
server_url: "http://localhost:8000"
weblog_url: "http://localhost:8000/telemetry"
submission:
  mode: local
```

For HPC (Alpine SLURM) deployment, see [docs/hpc-deployment.md](docs/hpc-deployment.md).

## API Endpoints

### Dispatch & Jobs
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/dispatch/batch` | Claim next pending batch |
| `POST` | `/dispatch/submitted` | Report batch as submitted to executor |
| `POST` | `/dispatch/requeue-expired` | Requeue stale claimed batches |

### Samples & Workflows
| Method | Path | Description |
|--------|------|-------------|
| `GET/POST` | `/samples` | List or register samples |
| `GET/POST` | `/workflows` | List or register workflow versions |
| `GET` | `/workflows/{pk}/job-summary` | Sample-level outcome counts for a workflow version |
| `PATCH` | `/workflows/{pk}/status` | Pause/activate/retire a workflow |

### Metrics (process-level)
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/metrics/processes/summary` | KPI cards + top failures/retries |
| `GET` | `/metrics/processes/failures` | Failure rate by process |
| `GET` | `/metrics/processes/retries` | Retry breakdown by process and attempt |
| `GET` | `/metrics/processes/resources-by-attempt` | CPU/memory utilisation by process + attempt |
| `GET` | `/metrics/processes/failure-signatures` | (process × exit_code) heatmap data |

### Admin
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/reconcile-jobs` | Create pending jobs for all sample × active workflow combinations |

All endpoints have OpenAPI docs at `/docs`.

## Metrics vs Job Outcomes

**Process metrics** (under `/metrics/processes/`) are Nextflow task-level: they show
individual process failures, retry counts, and resource utilisation. A sample can have
process failures that are rescued by retry and still complete successfully.

**Job outcomes** (`/workflows/{pk}/job-summary`) are sample-level: `completed` means the
`MARK_COMPLETE` sentinel fired; `failed` means the run ended without it. This is the
authoritative view of whether a sample actually succeeded end-to-end.

## Test Pipeline (nf_testing)

`nf_testing/main.nf` is a stub metagenomics pipeline (no real tools required) that exercises
the full telemetry contract:

- v0.1.0: 5 processes (`FETCH_READS → QC_READS → PROFILE_TAXA → AGGREGATE_RESULTS → MARK_COMPLETE`)
- v0.2.0: adds `STOCHASTIC_STEP` between `PROFILE_TAXA` and `AGGREGATE_RESULTS` — fails with
  configurable probability (default 30%, `--stochastic_fail_pct`), retries up to 2 times.
  Good for generating realistic failure/retry telemetry.

Each sample must be tagged as `sample_id:run_name` (the pipeline handles this automatically).

## Automated Tests

```bash
uv run pytest            # all tests
uv run pytest -x -q      # fail fast, quiet
```

Tests cover: health check, telemetry ingest, process metrics router, dispatch lifecycle,
and E2E loop with the nf_testing stub pipeline.
