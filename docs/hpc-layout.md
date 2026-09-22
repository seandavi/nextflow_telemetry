# HPC directory layout

One env file per cluster, same variable names everywhere, sourced from
`~/.bash_profile`. Troubleshooting on either cluster then starts with
`echo $NF_TEL_LOGS` instead of remembering paths.

Files: `config/nf_tel.env.alpine`, `config/nf_tel.env.anvil`. Installed at
`~/.nf_tel.env` on each cluster. Sync with:

```bash
scp config/nf_tel.env.alpine alpine:~/.nf_tel.env
scp config/nf_tel.env.anvil  anvil:~/.nf_tel.env
```

## Variables

| Variable | What | Alpine | Anvil |
|---|---|---|---|
| `NF_TEL_HOME` | Persistent root | `/projects/seda0001_amc/cmgd` | `/anvil/projects/x-cis240955/cmgd` |
| `NF_TEL_STORE` | Nextflow `storeDir` (DBs, refs) | `$NF_TEL_HOME/store` | `$NF_TEL_HOME/store` |
| `NF_TEL_LOGS` | slurm/nf/nextflow logs per run | `$NF_TEL_HOME/job_logs` | `$NF_TEL_HOME/logs` |
| `NF_TEL_DAEMON` | daemon config, daemon.log, templates | `/projects/seda0001_amc/nf_client` | `$NF_TEL_HOME/nf_worker` |
| `NF_TEL_CONFIG` | the client yaml the daemon runs with | `$NF_TEL_DAEMON/client-alpine.yaml` | `$NF_TEL_DAEMON/client-anvil.yaml` |
| `NF_TEL_REPO` | git checkout of this repo | `$NF_TEL_DAEMON/nextflow_telemetry` | `$NF_TEL_DAEMON/nextflow_telemetry` (absent) |
| `NF_TEL_NXF_HOME` | `NXF_HOME` (assets, plugins) | `/projects/seda0001_amc/nf_home` | `$HOME/nxf_home` |
| `NF_TEL_SCRATCH` | per-run workDir root, ephemeral | `/scratch/alpine/seda0001_amc/nf_worker` | `/anvil/scratch/x-seandavi/cmgd_data/work` |
| `NF_TEL_SIF_CACHE` | container image cache, ephemeral | `/scratch/alpine/seda0001_amc/apptainer_cache` | `/anvil/scratch/x-seandavi/singularity_cache` |
| `NF_TEL_CREDS` | GCS service-account json | `$HOME/curatedmetagenomicdata-*.json` | same |

Rule of thumb: **projects** = anything a run needs to resume or an operator
needs to read later. **scratch** = anything regenerable. **home** = credentials
only.

## Storage facts (2026-09-21)

| | Alpine | Anvil |
|---|---|---|
| home | 2 G quota, **84 % full** | 25 G quota, 9.3 G used, 8.2 G is `~/.apptainer` |
| projects | 250 G, 140 G used (56 %) | 5 T, 132 G used |
| scratch | 2.8 P shared, purged | 100 T, purged |
| nextflow | none on login node (`module load` or per-job) | pinned 23.10.1 at `$NF_TEL_DAEMON/nextflow` |
| repo checkout | `e36aef1` (2026-06-04, PR #110), behind main | none; `nf-client` installed to `~/.local/bin` |

## Housekeeping found during the survey

- Alpine scratch root holds ~200 `nxf-*`, `build-temp-*`, `bundle-temp-*` dirs
  from Sep–Dec 2025. Regenerable; delete when convenient.
- Anvil `~/.apptainer` (8.2 G) belongs on scratch. Move it and symlink, or set
  `APPTAINER_CACHEDIR=$NF_TEL_SIF_CACHE`.
- Anvil `submit_anvil.sh.j2` line 34 hard-codes `mkdir -p /anvil/scratch/x-seandavi/keep/store`; stale.
- `config/client-anvil.yaml.example` points `nextflow_bin` at `/anvil/scratch/x-seandavi/bin/nextflow`, which no longer exists. Live config uses `$NF_TEL_DAEMON/nextflow`.
- Alpine `~/.bash_profile` sets `SINGULARITY_CACHEDIR=/projects/$USER/.singularity`, which disagrees with the daemon's `singularity_cache` on scratch. The env file makes scratch win.

## Where the daemon lives

`tmux` on the login node (`login-ci4`, `login07`), started from `$NF_TEL_DAEMON`:

```bash
source ~/.nf_tel.env
cd $NF_TEL_DAEMON && tmux new -d -s nf 'nf-client daemon --config $NF_TEL_CONFIG'
tail -f $NF_TEL_DAEMON/daemon*.log
```
