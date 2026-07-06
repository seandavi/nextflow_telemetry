---
name: alpine-daemon
description: >
  Deploy, restart, or check the nf-client dispatch daemon on an HPC login node
  (CU Alpine, Anvil). Pull-mode orchestration: the daemon runs ON the cluster and
  reaches OUT to the telemetry API over HTTPS. Use when the daemon is down, needs
  restarting after a code update, isn't claiming jobs, or you're standing up a new
  cluster. Trigger words: daemon, nf-client, restart daemon, daemon down,
  redeploy, alpine, anvil, login node, not claiming, heartbeat.
---

# nf-client daemon on HPC

The daemon is the pull-mode worker: it runs on a cluster login node, polls the
telemetry API, claims jobs, renders a submit script, and `sbatch`es a Nextflow
driver per batch. See `telemetry-api` for the endpoints it calls.

## Why pull-mode (don't fight it)

The campus firewall **blocks outbound TCP/22 from onclappc02**, so the API host
can't SSH into clusters to push work. Instead daemons live on the clusters and
make **outbound HTTPS** to the API. Anything that assumes "the server connects to
the cluster" is wrong for this deployment.

## Cluster inventory (SSH, users, paths)

Two live SLURM clusters run `cmgd_nextflow`. Any triage/report agent needs this
to reach `sacct` / `squeue` / `srun`. The live roster + each daemon's full config
is also queryable at runtime — `GET /api/daemons/` returns `config_yaml`
(`profile`, `template_path`, `slurm_export_none`, batch size) per daemon.

| | Alpine (CU Boulder) | Anvil (Purdue) |
|---|---|---|
| SSH | `ssh alpine` (key-based, tailnet jump via dccapp720; works with `-o BatchMode=yes`) | `ssh anvil` (key-based; BatchMode ok) |
| Login / daemon host | `login-ci4` | `login07.anvil.rcac.purdue.edu` |
| Cluster user | `seda0001_amc` | `x-seandavi` |
| SLURM account | (default) | `cis240955` |
| Nextflow `-profile` | `alpine,gcs` | `anvil` |
| `store_dir` (persistent) | `/projects/seda0001_amc/cmgd/store` | `/anvil/projects/x-cis240955/cmgd/store` |
| Per-run workDir | `/scratch/alpine/$USER/nf_worker/$SLURM_JOB_ID` (ephemeral, `rm -rf` at job end; realpath `/gpfs/alpine1/scratch/...`) | `/anvil/scratch/x-seandavi/cmgd_data/work` |
| `slurm_export_none` | `true` (login env leaks to compute) | `false` (clean) |
| batch / max concurrent runs | 25 / 10 | 200 / 5 |
| Submit template | `/projects/seda0001_amc/nf_client/nextflow_telemetry/templates/submit_alpine.sh.j2` | `/anvil/projects/x-cis240955/cmgd/nf_worker/templates/submit_anvil.sh.j2` |
| Short test partition (`srun`) | `--partition=atesting --qos=testing` (1 h cap) | use a short-walltime partition |
| GCS access | `rclone gs1:` only (no gcloud/gsutil) | — |

`onclappc02` also appears in `/api/daemons/` — a dead `nf_testing` daemon; ignore
it (and its perpetual entry in `dispatchability.stuck`).

**Container repro must run on a compute node.** Login nodes have **no
`singularity`/`apptainer`** (`module load singularity` fails there); the binary
(`/usr/bin/singularity`) exists only on compute nodes. To reproduce a task's
container command, `srun` into a short partition (Alpine: `atesting`) and exec
there. Note `$TMPDIR` inside a task container points at a per-job path that is
**not** bind-mounted — tools that write scratch there (KMA) fail with
`Error: 2 (No such file or directory)` unless the process sets `TMPDIR="$PWD"`
(fixed pipeline 2.2.1).

**Anvil specifics:** nextflow is pinned to **23.10.1** (Java-11 only); the daemon
runs in `tmux` on login07; project space `/anvil/projects/x-cis240955` is
persistent (not scratch).

## Restart on Alpine

```bash
# On the Alpine login node (ssh alpine):
module load <whatever the job env needs>          # bare cluster python is 2.7 — see Gotchas
export PATH=$HOME/.local/bin:$PATH                # uv-tool-installed nf-client lives here
nf-client --version                               # sanity

# Start the daemon (continuous = keep polling when queue empty):
nf-client daemon --config client-alpine.yaml --batch-size 10
```
The daemon **reloads its YAML every poll cycle**, so config edits take effect
within one interval without a restart. For a code update (new nf-client), you DO
restart (and reinstall — below).

## Update nf-client (uv tool)

```bash
uv tool install --python 3.13 --from <path-or-git> nf-client    # or `uv tool upgrade`
which nf-client    # -> ~/.local/bin/nf-client
```
`uv tool install` brings its own managed Python 3.13 — it does not depend on the
cluster module python.

## Health check (from anywhere)

```bash
API=https://nf-telemetry.cancerdatasci.org
curl -s "$API/api/daemons/" | python3 -m json.tool        # last_heartbeat fresh?
curl -s "$API/api/admin/dispatchability" | python3 -m json.tool   # is cmgd stuck?
```
Fresh heartbeat + cmgd not in `stuck` = healthy. If cmgd is stuck with the daemon
up, check the daemon's `dispatch.workflow_id` filter.

## Client config surface (`client-alpine.yaml`)

Lives **on the cluster, outside the repo** (holds site paths/creds). Key fields:
- `server_url` (…/api), `weblog_url` (…/telemetry)
- `profile` — Nextflow `-profile` for this cluster, e.g. `alpine,gcs`
- `continuous: true` — keep polling when idle
- `dispatch.batch_size`, `dispatch.workflow_id` (restrict to `cmgd_nextflow`)
- `submission.mode: slurm`, `submission.template_path` (→ repo `templates/`)
- `submission.slurm_export_none: true` on Alpine (login env leaks to compute)
- `submission.defaults.*` — `mem`, `cpus`, `time`, `account`, `partition`,
  `store_dir`-adjacent paths, `google_credentials`, and `client_env_setup`
  (PATH snippet so `nf-client run-wrapper` resolves on the compute node).

Note: `params.store_dir` for the *pipeline* is set in the pipeline's
`conf/profiles/alpine.config` (`/projects/seda0001_amc/cmgd/store`), NOT in the
client yaml. See `cmgd-config`.

## Gotchas

- **Bare cluster `python` is 2.7.** Anything needing `nf_client` importable on a
  compute node (the `run-wrapper` driver) must use the uv-tool Python via the
  `client_env_setup` PATH hook, not the system python.
- **`--export=NONE` matters on Alpine** (`slurm_export_none: true`): the login
  environment otherwise leaks into compute jobs and breaks them. Anvil is clean —
  set false there.
- **GCS on Alpine: use `rclone` (`gs1:` remote). There is no `gsutil`/`gcloud`.**
  e.g. `rclone ls gs1:cmgd-data/results/cMDv4/cmgd_nextflow/<ver>`.
- **`store_dir` must be on a persistent project path, not `/scratch`** (purged).
  Alpine uses `/projects/seda0001_amc/cmgd/store` with a matching singularity bind.
- The daemon now **survives API outages** (retry loop + heartbeat guard) — a 5xx
  or network blip no longer kills it. After any restart, still confirm a fresh
  heartbeat landed.
- onclappc02's old `nf_testing` daemon is dead; it shows perpetually in
  `dispatchability.stuck`. Ignore it.
