# HPC deployment guide

One procedure for every SLURM cluster (Alpine, Anvil, and later Bridges-2).
Paths, variables and the file inventory are in [`hpc-layout.md`](hpc-layout.md).

## Architecture

```
┌──────────────────────────────┐        ┌──────────────────────────┐
│  v2 control plane            │◄───────│  login node              │
│  nf-telemetry.seandavi       │ HTTPS  │  nf-client daemon (tmux) │
│  .workers.dev                │        │  claims batches, sbatch  │
└──────────────────────────────┘        └──────────┬───────────────┘
         ▲                                         │ sbatch
         │ -with-weblog, run-wrapper events        ▼
         │                              ┌──────────────────────────┐
         └──────────────────────────────│  SLURM wrapper job        │
                                        │  runs nextflow, which     │
                                        │  submits per-sample tasks │
                                        └──────────────────────────┘
```

The daemon runs on the cluster and only makes outbound HTTPS calls
([ADR 0001](adr/0001-pull-mode-orchestration.md)). The daemon's dispatch calls
carry the bearer token; `/telemetry` and `/runs/{run}/event` are open, so the
wrapper job needs no secret.

## First-time setup on a cluster

```bash
# from a workstation with this repo
scp config/nf_tel.env.<cluster> <cluster>:~/.nf_tel.env
gcloud secrets versions access latest --secret=cdsci-nf-telemetry-v2-api-token --project=cdsci-infra \
  | sed 's/^/export NF_OPERATOR_TOKEN=/' \
  | ssh <cluster> 'umask 077; cat > ~/.nf_tel.secrets'

# on the cluster
echo '[ -f ~/.nf_tel.env ] && . ~/.nf_tel.env' >> ~/.bash_profile
source ~/.nf_tel.env
mkdir -p $NF_TEL_DAEMON $NF_TEL_LOGS $NF_TEL_STORE
git clone https://github.com/seandavi/nextflow_telemetry $NF_TEL_REPO
cp $NF_TEL_REPO/config/client-$NF_TEL_CLUSTER.yaml.example $NF_TEL_CONFIG
uv tool install --python 3.13 $NF_TEL_REPO/packages/nf_client
```

Before the first start, render the template and let SLURM validate it without
claiming work:

```bash
sbatch --test-only <rendered script>
```

A partition, qos or account the cluster no longer accepts fails here instead of
on every batch.

## Pipeline prerequisites

- The reference store at `$NF_TEL_STORE` should be populated before running more
  than one batch at a time, or concurrent runs download the same databases.
- After registering a new pipeline revision, refresh the cached asset on each
  cluster (`nextflow pull seandavi/curatedMetagenomicsNextflow -r <rev>` with the
  cluster's `NXF_HOME`). `nextflow run` does not fetch new tags on its own.

## Loading work

Workflows and samples are registered against the control plane from any machine
with the operator token:

```bash
export NF_OPERATOR_TOKEN=$(gcloud secrets versions access latest --secret=cdsci-nf-telemetry-v2-api-token --project=cdsci-infra)
S=https://nf-telemetry.seandavi.workers.dev/api
nf-client register-workflow --server $S --id cmgd_nextflow --version 2.2.1 \
  --repo https://github.com/seandavi/curatedMetagenomicsNextflow --revision 23e89cd --max-retries 2
nf-client add-cmd --server $S --study ZellerG_2014 --limit 2 --reconcile
```

Do not use `nf-client submit --dry-run` as a smoke test: fetching a batch claims it.

## Sample data model

| Field | Content |
|---|---|
| `sample_id` | md5 of the sorted, deduplicated run accessions (content address; outputs are published under it) |
| `ncbi_accession` | Semicolon-separated run accessions (e.g. `SRR001;SRR002`) |
| `biosample_id` | BioSample accession when known (set by study submissions; empty for cMD TSV loads) |

The submit template writes a TSV with columns `sample_id` and `NCBI_accession`
(the pipeline's column name) and passes it as `--metadata_tsv`.

## Concurrency

`max_concurrent_runs` caps wrapper jobs in the queue; the daemon checks `squeue`
before each submission. `dispatch.batch_size` is samples per wrapper job.
