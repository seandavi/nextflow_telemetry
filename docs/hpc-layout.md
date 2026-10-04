# HPC directory layout

Every SLURM cluster runs the same pieces; only values differ:

| Piece | Repo source | On the cluster |
|---|---|---|
| Paths, account, modules | `config/nf_tel.env.<cluster>` | `~/.nf_tel.env`, sourced from `~/.bash_profile` |
| v2 bearer token | GSM `cdsci-nf-telemetry-v2-api-token` (project `cdsci-infra`) | `~/.nf_tel.secrets` (mode 600, `export NF_OPERATOR_TOKEN=...`) |
| R2 S3 keys (`-profile r2`) | GSM `cdsci-r2-account-id`, `cdsci-r2-access-key-id`, `cdsci-r2-secret-access-key` | `~/.nf_tel.r2` (mode 600, `export R2_ACCOUNT_ID=...` etc.), sourced by the submit script ([ADR 0008](adr/0008-object-storage-on-r2.md)) |
| Client yaml | `config/client-<cluster>.yaml.example` | `$NF_TEL_CONFIG` |
| Submit template | `templates/submit_slurm.sh.j2` | read from `$NF_TEL_REPO` |
| Daemon launcher | `config/nf_tel_daemon.sh` | run from `$NF_TEL_REPO` in tmux |

The client yaml references the environment as `${NAME}` (nf-client expands it at
load and fails on an unset variable), so the yaml carries only scheduler choices:
partition, qos, resources, batch sizes, `profile`. The submit script sources
`~/.nf_tel.env` itself, because batch shells are not login shells and Alpine
submits with `--export=NONE`.

Sync after editing:

```bash
scp config/nf_tel.env.alpine alpine:~/.nf_tel.env
scp config/nf_tel.env.anvil  anvil:~/.nf_tel.env
ssh <cluster> 'source ~/.nf_tel.env && cd $NF_TEL_REPO && git pull --ff-only'
```

## Variables

| Variable | What | Alpine | Anvil |
|---|---|---|---|
| `NF_TEL_ACCOUNT` | SLURM account for wrapper and task jobs | `amc-general` | `cis240955` |
| `NF_TEL_HOME` | Persistent root | `/projects/seda0001_amc/cmgd` | `/anvil/projects/x-$NF_TEL_ACCOUNT/cmgd` |
| `NF_TEL_STORE` | Nextflow `storeDir` (DBs, refs), bound at `/keep/store` | `$NF_TEL_HOME/store` | `$NF_TEL_HOME/store` |
| `NF_TEL_LOGS` | slurm/nf/nextflow logs per run | `$NF_TEL_HOME/job_logs` | `$NF_TEL_HOME/logs` |
| `NF_TEL_DAEMON` | client yaml, `daemon.log` | `/projects/seda0001_amc/nf_client` | `$NF_TEL_HOME/nf_worker` |
| `NF_TEL_CONFIG` | the client yaml the daemon runs with | `$NF_TEL_DAEMON/client-alpine.yaml` | `$NF_TEL_DAEMON/client-anvil.yaml` |
| `NF_TEL_REPO` | git checkout of this repo | `$NF_TEL_DAEMON/nextflow_telemetry` | `$NF_TEL_DAEMON/nextflow_telemetry` |
| `NF_TEL_NXF_HOME` | `NXF_HOME` (assets, plugins) | `/projects/seda0001_amc/nf_home` | `$HOME/nxf_home` |
| `NF_TEL_SCRATCH` | per-run launch dir root, ephemeral | `/scratch/alpine/seda0001_amc/nf_worker` | `/anvil/scratch/x-seandavi/cmgd_data` |
| `NF_TEL_SIF_CACHE` | container image cache, ephemeral | `/scratch/alpine/seda0001_amc/apptainer_cache` | `/anvil/scratch/x-seandavi/singularity_cache` |
| `NF_TEL_CREDS` | GCS service-account json (legacy `gcs` profile) | `$HOME/curatedmetagenomicdata-*.json` | same |
| `NF_TEL_MODULES` | `module load` list for the submit script | `singularity git jdk/18.0.1.1` (+ `NXF_VER=25.10.8`) | `openjdk/11.0.8_10` (+ `NXF_VER=23.10.1`) |

Nextflow on both clusters is the standalone launcher at `$NF_TEL_DAEMON/nextflow`
(`curl -fsSL https://get.nextflow.io`), pinned by `NXF_VER`. Alpine needs >= 25.04
for the `r2` profile and < 26.04 until the pipeline passes the strict parser;
Anvil's Java 11 caps it at 23.10.1, which cannot use `r2`; Anvil's daemon is
stopped (since 2026-10-03) and must not be restarted on `anvil,gcs`, since that
would write new outputs to GCS ([ADR 0008](adr/0008-object-storage-on-r2.md)).
Give it Java 17+ and `anvil,r2` first (untested option: a user-space JDK under `$NF_TEL_DAEMON`).

Rule of thumb: **projects** = anything a run needs to resume or an operator
needs to read later. **scratch** = anything regenerable. **home** = credentials
only.

The template overrides the pipeline profile's `params.store_dir` and the
singularity bind with `$NF_TEL_STORE`, so the store follows the allocation
rather than the path hard-coded in `conf/profiles/<cluster>.config`.

## Switching allocations

Anvil project space is per allocation (`/anvil/projects/x-<account>`), so
`NF_TEL_HOME` derives from `NF_TEL_ACCOUNT`. To move to another allocation:

1. Confirm the account exists: `sacctmgr -nP show assoc user=$USER format=account`
   and `mybalance`. ACCESS Credits must be exchanged for the resource first.
2. Copy `$NF_TEL_STORE` (~120 GiB, include the root `.command*` and
   `versions.yml`; storeDir skips a process only when every declared output
   exists) and `$NF_TEL_DAEMON` (client yaml, pinned `nextflow`, repo checkout)
   to the new project dir.
3. Edit `NF_TEL_ACCOUNT` in `config/nf_tel.env.<cluster>`, sync, restart the daemon.

## Adding a cluster (e.g. PSC Bridges-2)

Add `config/nf_tel.env.<cluster>` with every variable above and
`config/client-<cluster>.yaml.example` with that cluster's partition, qos and
resources. No template or code changes are needed when the cluster runs SLURM.
Decide `slurm_export_none` by testing whether login-node modules leak into jobs,
and check `sbatch --test-only` with the rendered script before starting the daemon.

## Storage facts (2026-09-21)

| | Alpine | Anvil |
|---|---|---|
| home | 2 G quota, **84 % full** | 25 G quota, 9.3 G used, 8.2 G is `~/.apptainer` |
| projects | 250 G, 140 G used (56 %) | 5 T, 132 G used |
| scratch | 2.8 P shared, purged | 100 T, purged |
| nextflow | 25.10.8 launcher at `$NF_TEL_DAEMON/nextflow` (Java 18 module); the 24.04 module is not used | pinned 23.10.1 at `$NF_TEL_DAEMON/nextflow` |
| partitions | `acpu` (1 day) with qos `cpu-normal`; `cpu-long` for 7 days. `amilan` is gone. | `shared` |

Housekeeping still open:

- Alpine scratch root holds ~200 `nxf-*`, `build-temp-*`, `bundle-temp-*` dirs
  from Sep–Dec 2025. Regenerable; delete when convenient.
- Anvil `~/.apptainer` (8.2 G) belongs on scratch. Move it and symlink, or set
  `APPTAINER_CACHEDIR=$NF_TEL_SIF_CACHE`.
- Alpine `~/.bash_profile` sets `SINGULARITY_CACHEDIR=/projects/$USER/.singularity`,
  which disagrees with `NF_TEL_SIF_CACHE`. The env file makes scratch win.
- On Alpine, run `uv tool install` with `UV_CACHE_DIR` on scratch; home is nearly full.

## Running the daemon

Same commands on every cluster:

```bash
source ~/.nf_tel.env
tmux new-session -d -s nf "$NF_TEL_REPO/config/nf_tel_daemon.sh"   # start
tmux kill-session -t nf                                             # stop
tail -f $NF_TEL_DAEMON/daemon.log
```

Update nf-client from the checkout, then restart:

```bash
source ~/.nf_tel.env && cd $NF_TEL_REPO && git pull --ff-only
uv tool install --force --python 3.13 $NF_TEL_REPO/packages/nf_client
```

The daemon re-reads `$NF_TEL_CONFIG` every poll and the template on every
submit; only nf-client code changes and env-file changes need a restart.
