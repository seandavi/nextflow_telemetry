---
name: alpine-daemon
description: >
  Deploy, restart, or check the nf-client dispatch daemon on an HPC login node
  (CU Alpine, Anvil, later PSC Bridges-2). Pull-mode orchestration: the daemon runs
  ON the cluster and reaches OUT to the telemetry API over HTTPS. Use when the daemon
  is down, needs restarting after a code update, isn't claiming jobs, or you're
  standing up a new cluster. Trigger words: daemon, nf-client, restart daemon, daemon
  down, redeploy, alpine, anvil, bridges, login node, not claiming, heartbeat.
---

# nf-client daemon on HPC

The daemon is the pull-mode worker: it runs on a cluster login node, polls the
control plane, claims jobs, renders a submit script, and `sbatch`es a Nextflow
driver per batch. See `telemetry-api` for the endpoints it calls.

## Why pull-mode (don't fight it)

The campus firewall **blocks outbound TCP/22 from onclappc02**, so nothing can SSH
into clusters to push work. Daemons live on the clusters and make **outbound
HTTPS** to the API.

## Every cluster is set up the same way

Source of truth: `docs/hpc-layout.md` (variables, files) and
`docs/hpc-deployment.md` (first-time setup).

| Piece | Where |
|---|---|
| Paths, `NF_TEL_ACCOUNT`, `NF_TEL_MODULES` | `~/.nf_tel.env` ← `config/nf_tel.env.<cluster>` |
| v2 bearer token | `~/.nf_tel.secrets` (600) ← GSM `cdsci-nf-telemetry-v2-api-token` |
| R2 keys (`-profile r2`) | `~/.nf_tel.r2` (600) ← GSM `cdsci-r2-*`; sourced by the submit template, which refuses r2 runs without it |
| Client yaml | `$NF_TEL_CONFIG` ← `config/client-<cluster>.yaml.example` (uses `${NF_TEL_*}`) |
| Submit template | `$NF_TEL_REPO/templates/submit_slurm.sh.j2` (one for all SLURM clusters) |
| Launcher | `$NF_TEL_REPO/config/nf_tel_daemon.sh` |
| Log | `$NF_TEL_DAEMON/daemon.log` |

Non-interactive `ssh host 'cmd'` does **not** source `~/.bash_profile`; start
remote commands with `source ~/.nf_tel.env`.

| | Alpine (CU Boulder) | Anvil (Purdue) |
|---|---|---|
| SSH | `ssh alpine` (tailnet jump via dccapp720; BatchMode ok) | `ssh anvil` (pinned to login07; BatchMode ok) |
| Cluster user | `seda0001_amc` | `x-seandavi` |
| `NF_TEL_ACCOUNT` | `amc-general` | `cis240955` (ACCESS; `NF_TEL_HOME` derives from it) |
| Partition / qos | `acpu` / `cpu-normal` (1 day); `amilan` is gone | `shared` |
| Nextflow `-profile` | `alpine,r2` (R2 `s3://cmgd-raw`, ADR 0008) | daemon stopped since 2026-10-03; do **not** restart on `anvil,gcs` (new GCS writes violate ADR 0008). Needs Java 17+ for Nextflow >= 25.04 and `anvil,r2` first |
| `slurm_export_none` | `true` (login env leaks to compute) | `false` |
| Nextflow | 25.10.8 launcher in `$NF_TEL_DAEMON` (`NXF_VER`, `jdk/18.0.1.1`); not the 24.04 module | pinned 23.10.1 launcher in `$NF_TEL_DAEMON` (Java 11 only) |
| Short test partition | `atesting`, qos `testing` (1 h) | — |
| GCS access | `rclone gs1:` only (no gcloud/gsutil) | — |

## Start / stop / restart

```bash
ssh <cluster>
source ~/.nf_tel.env
tmux kill-session -t nf 2>/dev/null
sleep 3   # killing the last session stops the tmux server; an immediate new-session dies with it
tmux new-session -d -s nf "$NF_TEL_REPO/config/nf_tel_daemon.sh"
tail -n 20 $NF_TEL_DAEMON/daemon.log
```

Confirm from a fresh connection that the process survived, not just the log:
`tmux ls; pgrep -af '^/.*nf-client daemon'`.

No `tmux send-keys` (Anvil's slow login rc eats the keystrokes) and no `&`
inside `ssh host '...'` (the ssh session hangs on the held fd).

The daemon reloads its yaml every poll and the template on every submit. Restart
only after an nf-client update or an env-file change.

## Update nf-client

```bash
source ~/.nf_tel.env && cd $NF_TEL_REPO && git pull --ff-only
uv tool install --force --python 3.13 $NF_TEL_REPO/packages/nf_client   # Alpine: UV_CACHE_DIR=/scratch/alpine/$USER/uv_cache
```

## Validate before starting

Render the template and run `sbatch --test-only` on it. Do not use
`nf-client submit --dry-run`: it claims a batch.

## Health check (from anywhere)

```bash
API=https://nf-telemetry.seandavi.workers.dev/api
curl -s "$API/daemons" | python3 -m json.tool              # last_heartbeat fresh?
curl -s "$API/admin/dispatchability" | python3 -m json.tool
curl -s "$API/admin/stats"
```

## Gotchas

- **Container repro must run on a compute node.** Login nodes have no
  `singularity`/`apptainer`. `srun` into a short partition and exec there.
- **Bare cluster `python` is 2.7.** `~/.nf_tel.env` puts `~/.local/bin`
  (uv-tool nf-client with its own Python 3.13) on PATH for jobs.
- **`store_dir` must be persistent.** The template sets it from `$NF_TEL_STORE`
  and binds it at `/keep/store`, overriding the pipeline profile's hard-coded path.
- **New pipeline revision → `nextflow pull` on each cluster** with its `NXF_HOME`;
  otherwise runs fail fast with `Cannot find revision`.
- **Alpine home is ~84 % full.** Keep caches on scratch.
