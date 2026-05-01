# HPC Deployment Guide (Alpine)

This guide covers running `nf-client` on the Alpine HPC cluster at CU Anschutz to dispatch
batches of the curatedMetagenomicData (cmgd) pipeline.

## Architecture

```
┌─────────────────────┐        ┌──────────────────────────┐
│  Telemetry Server   │◄───────│  Alpine head node         │
│  (public HTTPS)     │        │  nf-client daemon (tmux)  │
│                     │        │  claims batches, sbatch   │
└─────────────────────┘        └──────────┬───────────────┘
         ▲                                │ sbatch
         │ -with-weblog                   ▼
         │                     ┌──────────────────────────┐
         └─────────────────────│  SLURM wrapper job        │
                               │  runs nextflow            │
                               │  → submits per-sample     │
                               │    tasks to SLURM         │
                               └──────────────────────────┘
```

- The daemon runs on the Alpine **head node** (not from your laptop via SSH).
- The SLURM wrapper job is lightweight (2G / 1 CPU / 100h) — it orchestrates Nextflow,
  which itself submits per-sample compute tasks to SLURM via `process.executor = 'slurm'`.
- Nextflow sends weblog events to the public telemetry server URL. Alpine compute nodes
  can reach the public internet.

## Installation on Alpine

```bash
ssh alpine
cd /projects/seda0001_amc/nf_worker

# Pull latest code
git pull

# Install nf-client into project venv (uv already available)
uv pip install -e packages/nf_client

# Verify
nf-client --help
```

> **Note**: If `nf-client` fails with a Click `make_metavar` error after a `uv sync`,
> re-run `uv pip install -e packages/nf_client` — it re-pins `click<8.2`.

## Configuration

Copy the example config and fill in the server URL:

```bash
cp config/client-alpine.yaml.example client-alpine.yaml
```

Edit `client-alpine.yaml` (gitignored):

```yaml
server_url: "https://YOUR_SERVER.run.app"
weblog_url: "https://YOUR_SERVER.run.app/telemetry"

dispatch:
  batch_size: 100             # samples per Nextflow wrapper run
  workflow_id: "cmgd_nextflow"
  workflow_version: "1.3.0"

submission:
  mode: slurm
  template_path: "templates/submit_alpine.sh.j2"
  max_concurrent_runs: 5      # hold back if 5 wrapper jobs already in SLURM queue
  defaults:
    mem: "2G"
    cpus: 1
    time: "100:00:00"
    qos: "long"
    partition: "amilan"
    log_dir: "/projects/seda0001_amc/cmgd/job_logs"
    singularity_cache: "/scratch/alpine/seda0001_amc/apptainer_cache"
    store_dir: "/projects/seda0001_amc/nf_keep/store"
    google_credentials: "$HOME/curatedmetagenomicdata-232f4a306d1d.json"
```

## Registering the cmgd workflow

Before dispatching, the pipeline must be registered with the server:

```bash
curl -X POST https://YOUR_SERVER.run.app/workflows \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow_id": "cmgd_nextflow",
    "version": "1.3.0",
    "repository_url": "https://github.com/seandavi/curatedmetagenomicsnextflow",
    "revision": "main",
    "profile": "alpine",
    "max_retries": 4,
    "description": "curatedMetagenomicData pipeline — Alpine SLURM"
  }'
```

Then reconcile jobs (creates one pending job per sample × active workflow version):

```bash
curl -X POST https://YOUR_SERVER.run.app/admin/reconcile-jobs
```

## Running the daemon

The daemon claims batches, generates metadata TSVs, submits SLURM wrapper jobs,
and sleeps when the concurrency limit is reached. Run it in a `tmux` session
so it persists after you disconnect:

```bash
tmux new -s nf-daemon
nf-client daemon --config client-alpine.yaml
# Ctrl-B D  to detach
# tmux attach -t nf-daemon  to reattach
```

To do a dry run first (fetch one batch, print the command, don't submit):

```bash
nf-client submit --config client-alpine.yaml --dry-run
```

## Sample data model

| DB column | Content |
|-----------|---------|
| `sample_id` | BioSample accession (e.g. `SAMN12345678`) |
| `metadata.ncbi_accession` | Semicolon-separated SRR list (e.g. `SRR001;SRR002`) |

The submit template generates a TSV with columns `sample_id` and `NCBI_accession`
(the pipeline's expected column name) and passes it as `--metadata_tsv`.

## Concurrency limits

Alpine enforces per-user job limits. `max_concurrent_runs` in the config caps how many
SLURM wrapper jobs are submitted at once. The daemon checks `squeue` before each
submission and pauses when the limit is reached.

Recommended values:
- `max_concurrent_runs: 5` — 5 wrapper jobs × 100 samples = 500 samples in flight
- `batch_size: 100` — samples per wrapper job (also controls Nextflow's `--sample_ids` channel size)

## Security note

The `/telemetry` weblog endpoint is unauthenticated — Nextflow does not support sending
an auth token with weblog events. This means the endpoint accepts events from anyone
who knows the URL. Mitigation options for the future:
- IP allowlist on the load balancer (limit to Alpine egress IPs)
- Shared-secret middleware (requires Nextflow to support a custom header — not yet available)

For now, the endpoint is append-only and read endpoints are separate, so the exposure
is limited to someone injecting spurious telemetry records.
