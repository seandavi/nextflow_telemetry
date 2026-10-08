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
carry the bearer token; `/telemetry` and `/runs/{run}/event` are open. The
wrapper job's only secret is the R2 write key for `-profile r2`
([ADR 0008](adr/0008-object-storage-on-r2.md)), which the Nextflow driver uses
for the `publishDir` copy.

## First-time setup on a cluster

```bash
# from a workstation with this repo
scp config/nf_tel.env.<cluster> <cluster>:~/.nf_tel.env
gcloud secrets versions access latest --secret=cdsci-nf-telemetry-v2-api-token --project=cdsci-infra \
  | sed 's/^/export NF_OPERATOR_TOKEN=/' \
  | ssh <cluster> 'umask 077; cat > ~/.nf_tel.secrets'
g() { gcloud secrets versions access latest --secret=$1 --project=cdsci-infra; }
# GitHub token for Nextflow's per-run pipeline clones (unauthenticated API: 60/h per cluster IP)
g cmgd-nextflow-github-token | sed 's/^/export GITHUB_TOKEN=/' | ssh <cluster> 'umask 077; cat > ~/.nf_tel.github'
printf 'export R2_ACCOUNT_ID=%s\nexport R2_ACCESS_KEY_ID=%s\nexport R2_SECRET_ACCESS_KEY=%s\n' \
  "$(g cdsci-r2-account-id)" "$(g cdsci-r2-access-key-id)" "$(g cdsci-r2-secret-access-key)" \
  | ssh <cluster> 'umask 077; cat > ~/.nf_tel.r2'

# on the cluster
echo '[ -f ~/.nf_tel.env ] && . ~/.nf_tel.env' >> ~/.bash_profile
source ~/.nf_tel.env
mkdir -p $NF_TEL_DAEMON $NF_TEL_LOGS $NF_TEL_STORE
git clone https://github.com/seandavi/nextflow_telemetry $NF_TEL_REPO
cp $NF_TEL_REPO/config/client-$NF_TEL_CLUSTER.yaml.example $NF_TEL_CONFIG
uv tool install --python 3.13 $NF_TEL_REPO/packages/nf_client
curl -fsSL https://get.nextflow.io -o $NF_TEL_DAEMON/nextflow && chmod +x $NF_TEL_DAEMON/nextflow   # pinned by NXF_VER
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
- No per-release `nextflow pull`: every run clones the pipeline at its revision
  into `$WORKDIR/assets` (`NXF_ASSETS`, #192), so a new tag needs nothing on the
  clusters. `$NXF_HOME/assets` is no longer used by dispatched runs.

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

A registration is one pipeline configuration ([ADR 0010](adr/0010-registrations-are-bundles.md)).
`--param key=value` (repeatable) pins pipeline params, which runs receive as
`-params-file params.json`; `--collection` (repeatable) limits the jobs to
samples in those collections. A cluster that serves several bundles lists
them in its client yaml:

```yaml
dispatch:
  workflow_id:
    - cmgd_humann3.9
    - cmgd_humann4a1
```

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
