# HPC directory layout

Every SLURM cluster runs the same pieces; only values differ:

| Piece | Repo source | On the cluster |
|---|---|---|
| Paths, account, modules | `config/nf_tel.env.<cluster>` | `~/.nf_tel.env`, sourced from `~/.bash_profile` |
| v2 bearer token | GSM `cdsci-nf-telemetry-v2-api-token` (project `cdsci-infra`) | `~/.nf_tel.secrets` (mode 600, `export NF_OPERATOR_TOKEN=...`) |
| R2 S3 keys (`-profile r2`) | GSM `cdsci-r2-account-id`, `cdsci-r2-access-key-id`, `cdsci-r2-secret-access-key` | `~/.nf_tel.r2` (mode 600, `export R2_ACCOUNT_ID=...` etc.), sourced by the submit script ([ADR 0008](adr/0008-object-storage-on-r2.md)) |
| Client yaml | `config/client-<cluster>.yaml` | read in place: `$NF_TEL_CONFIG` = `$NF_TEL_REPO/config/client-<cluster>.yaml`, so the checkout *is* the config |
| Submit template | `templates/submit_slurm.sh.j2` | read from `$NF_TEL_REPO` |
| Daemon launcher | `config/nf_tel_daemon.sh` | run from `$NF_TEL_REPO` in tmux |

The client yaml references the environment as `${NAME}` (nf-client expands it at
load and fails on an unset variable), so the yaml carries only scheduler choices:
partition, qos, resources, batch sizes, `profile`. The submit script sources
`~/.nf_tel.env` itself, because batch shells are not login shells and Alpine
submits with `--export=NONE`.

Deploy after merging (installs the env file, checkout, client yaml and nf-client
at one commit, restarts the daemon, and checks its heartbeat):

```bash
just deploy-clients all            # or: alpine | anvil, optional ref (default origin/main)
```

### Where the client yaml comes from, and the accept list

The tracked `config/client-<cluster>.yaml` is the only source of truth. Before
#255 it was an untracked, hand-edited copy in `$NF_TEL_DAEMON` (seeded from a
`.example`); those copies and their `*.bak*` files are leftovers and are no
longer read once a cluster is deployed at or after #255. Change a setting by
PR, then `just deploy-clients`; never edit the file on the cluster (the next
deploy checks out over it and refuses a dirty checkout).

`dispatch.workflow_id` is the **accept list**: the daemon claims only those
workflow ids. Omit it to claim from every *active* registration (Alpine and Anvil do;
pausing or retiring a registration on the server is then the gate). A revision
bump (ADR-0004) keeps the workflow id, so it never needs an accept-list edit;
only a brand-new workflow id does. Bridges-2 claims nothing while its
`max_concurrent_runs` is 0.

Order of operations for a release: tag the pipeline, `PATCH /workflows/{pk}/revision`
(see the `cmgd-release` skill), merge any client yaml change, then
`just deploy-clients <cluster>`. The yaml is re-read every poll, so a yaml-only
change is live as soon as the checkout is at the new commit; nf-client code and
env-file changes need the restart that `deploy-clients` performs.

Restarting is safe with runs in flight: the daemon holds no run state, it counts
active runs from UUID-named wrapper jobs in `squeue` (`_count_active_slurm_jobs`),
and wrappers report to the server directly. `deploy-clients` still refuses when
the daemon reports active runs; pass `FORCE=1` for a code or env change you do
not want to wait out.

## Variables

| Variable | What | Alpine | Anvil | Bridges-2 |
|---|---|---|---|---|
| `NF_TEL_ACCOUNT` | SLURM account for wrapper and task jobs | `amc-general` | `cis240955` | `cis260344p` |
| `NF_TEL_HOME` | Persistent root | `/projects/seda0001_amc/cmgd` | `/anvil/projects/x-$NF_TEL_ACCOUNT/cmgd` | `/ocean/projects/$NF_TEL_ACCOUNT/seandavi/cmgd` |
| `NF_TEL_STORE` | Nextflow `storeDir` (DBs, refs), bound at `/keep/store` | `$NF_TEL_HOME/store` | `$NF_TEL_HOME/store` | `$NF_TEL_HOME/store` |
| `NF_TEL_LOGS` | slurm/nf/nextflow logs per run | `$NF_TEL_HOME/job_logs` | `$NF_TEL_HOME/logs` | `$NF_TEL_HOME/logs` |
| `NF_TEL_DAEMON` | `daemon.log`, pinned `nextflow` | `/projects/seda0001_amc/nf_client` | `$NF_TEL_HOME/nf_worker` | `$NF_TEL_HOME/nf_worker` |
| `NF_TEL_CONFIG` | the client yaml the daemon runs with | `$NF_TEL_REPO/config/client-alpine.yaml` | `$NF_TEL_REPO/config/client-anvil.yaml` | `$NF_TEL_REPO/config/client-bridges2.yaml` |
| `NF_TEL_REPO` | git checkout of this repo | `$NF_TEL_DAEMON/nextflow_telemetry` | `$NF_TEL_DAEMON/nextflow_telemetry` | `$NF_TEL_DAEMON/nextflow_telemetry` |
| `NF_TEL_NXF_HOME` | `NXF_HOME` (assets, plugins) | `/projects/seda0001_amc/nf_home` | `$HOME/nxf_home` | `$HOME/nxf_home` |
| `NF_TEL_SCRATCH` | per-run launch dir root, ephemeral | `/scratch/alpine/seda0001_amc/nf_worker` | `/anvil/scratch/x-seandavi/cmgd_data` | `/ocean/projects/cis260344p/seandavi/scratch/cmgd_data` (not purged; counts toward quota) |
| `NF_TEL_SIF_CACHE` | container image cache, ephemeral | `/scratch/alpine/seda0001_amc/apptainer_cache` | `/anvil/scratch/x-seandavi/singularity_cache` | `/ocean/projects/cis260344p/seandavi/scratch/singularity_cache` |
| `NF_TEL_CREDS` | GCS service-account json (legacy `gcs` profile) | `$HOME/curatedmetagenomicdata-*.json` | same | same |
| `NF_TEL_MODULES` | `module load` list for the submit script | `singularity git jdk/18.0.1.1` (+ `NXF_VER=25.10.8`) | none; user-space JDK 21 at `$NF_TEL_DAEMON/jdk` (+ `NXF_VER=25.10.8`) | none; user-space JDK 21 at `$NF_TEL_DAEMON/jdk` (+ `NXF_VER=25.10.8`) |

Nextflow on both clusters is the standalone launcher at `$NF_TEL_DAEMON/nextflow`
(`curl -fsSL https://get.nextflow.io`), pinned by `NXF_VER`. Both clusters need >= 25.04
for the `r2` profile ([ADR 0008](adr/0008-object-storage-on-r2.md)) and < 26.04 until
the pipeline passes the strict parser. Anvil's modules stop at Java 11, so it runs a
user-space Temurin 21 JDK at `$NF_TEL_DAEMON/jdk` (`JAVA_HOME` in `~/.nf_tel.env`):

```bash
curl -fsSL -o /tmp/jdk.tgz https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse
mkdir $NF_TEL_DAEMON/jdk && tar -xzf /tmp/jdk.tgz -C $NF_TEL_DAEMON/jdk --strip-components=1
```

A launcher fetched for 23.x cannot boot 25.x (`exec: --: invalid option`); re-fetch it
from get.nextflow.io when crossing major versions. Never run Anvil on `anvil,gcs`:
that writes new outputs to GCS.

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
`config/client-<cluster>.yaml` with that cluster's partition, qos and
resources. No template or code changes are needed when the cluster runs SLURM.
Decide `slurm_export_none` by testing whether login-node modules leak into jobs,
and check `sbatch --test-only` with the rendered script before starting the daemon.

### PSC Bridges-2 (added 2026-10-13)

| | |
|---|---|
| SSH | `ssh bridges2` lands on a round-robin login node (daemon on `br012`). PSC login needs interactive auth, so non-interactive commands and `deploy-clients` ride an open `ControlMaster` session (`ControlPersist 4h` in `~/.ssh/config`). `scp` is refused; pipe files instead: `ssh bridges2 'cat > ~/f' < f` |
| Allocation | `cis260344p`: 10,000 SU (RM, billed per core-hour) and 1000 GiB Ocean for store, SIF cache and run dirs together. `cis260183p` has no SUs |
| Partition | `RM-shared`, 3-day max walltime, at most 64 cores per job |
| Memory | Rejects any job above **2000 MB per core** ("mem-per-core higher than maximum"). The client yaml sets `max_mem_per_cpu_mb: 2000`, which makes the submit template raise each task's `--cpus-per-task` to `ceil(memory / 2000 MB)`. MetaPhlAn's 55 GiB therefore costs 29 cores, about 29 SU per hour |
| Java / containers | System java is 1.8; Temurin 21 at `$NF_TEL_DAEMON/jdk`. `singularity`/`apptainer` are on the login and compute PATH |
| Pipeline profile | `-profile bridges2,r2` needs `conf/profiles/bridges2.config` in the registered pipeline revision. Until then `max_concurrent_runs: 0` keeps the daemon heartbeating without claiming |

## Storage facts (2026-09-21)

| | Alpine | Anvil |
|---|---|---|
| home | 2 G quota, **84 % full** | 25 G quota, 9.3 G used, 8.2 G is `~/.apptainer` |
| projects | 250 G, 140 G used (56 %) | 5 T, 132 G used |
| scratch | 2.8 P shared, purged | 100 T, purged |
| nextflow | 25.10.8 launcher at `$NF_TEL_DAEMON/nextflow` (Java 18 module); the 24.04 module is not used | 25.10.8 launcher at `$NF_TEL_DAEMON/nextflow` (Temurin 21 at `$NF_TEL_DAEMON/jdk`) |
| partitions | `acpu`. Run wrappers use qos `cpu-long` at 4 days (must be >24h, 7-day cap, 20 nodes per user) because a wrapper waits on every child including retries; child tasks use the default qos (1 day). `amilan` is gone. | `shared` |

Scratch purges empty `$NF_TEL_SIF_CACHE`. A run that finds it cold builds each
image inside its wrapper job, and `mksquashfs` on the 1.6 GB MetaPhlAn image needs
more than 4G (#208). After a purge, refill the cache in one job instead of in
every run's wrapper (Anvil shown; the image list is the pipeline's `container` lines):

```bash
sbatch -A $NF_TEL_ACCOUNT -p shared -c4 --mem=16G -t 2:00:00 -J sif-prepull --wrap '
cd $NF_TEL_SIF_CACHE
for u in seandavi/curatedmetagenomics:metaphlan4.2.2 quay.io/biocontainers/kma:1.6.13--h118bc1c_0 staphb/bracken:2.9 staphb/kraken2:2.1.3; do
  n=$(echo $u | sed "s#[/:]#-#g").img; [ -s $n ] || { singularity pull --name $n.tmp docker://$u && mv $n.tmp $n; }
done'
```

### Run directories and cleanup

Everything a run writes lives under `$NF_TEL_SCRATCH/<slurm job id>/` (launch dir,
Nextflow `work/`, temp, the per-run pipeline clone). The batch script removes it on
every exit it sees: success, failure, `scancel`, and walltime (SLURM sends USR1 ten
minutes early via `--signal=B:USR1@600`). The login-node watchdog sweeps directories
whose job is no longer in `squeue` (node loss, `kill -9`), skipping anything less than
an hour old and anything not named by a job id.

| Variable (in `~/.nf_tel.env`) | Default | Effect |
|---|---|---|
| `NF_TEL_KEEP_FAILED` | unset | `1`: keep a failed run's directory (marked `.keep_failed`) for debugging |
| `NF_TEL_KEEP_FAILED_HOURS` | `48` | the watchdog deletes kept directories after this long |

Task logs and the run's `.nextflow.log`/wrapper output are uploaded to v2, so a
failure is diagnosable without the directory.

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

Update with `just deploy-clients <cluster>` from a workstation, not by hand: it
installs nf-client from `git+file://$NF_TEL_REPO@<sha>`, so the heartbeat's
`nf_client_version` reads `<version>+<sha7>` and the daily health report can
flag a daemon that is behind main (#254). The checkout is left detached at
that commit.

The daemon re-reads `$NF_TEL_CONFIG` every poll and the template on every
submit; only nf-client code changes and env-file changes need a restart.
