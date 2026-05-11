# Orchestrator architecture

> Status: **draft** — control-plane design for HPC dispatch. Read alongside [`hpc-deployment.md`](./hpc-deployment.md) (operational guide for the current pull-mode setup) and [`publish-and-catalog-design.md`](./publish-and-catalog-design.md) (outputs catalog, unrelated to control plane).

## Framing

Today the orchestrator is one `nf-client` daemon per cluster, running on each cluster's login node inside a tmux session. The May 6 SSL crash on Alpine — silently dead for five days, only noticed when output stopped — is the load-bearing example of why this model has reached its limit. Login-node tmux is observable only by `ssh`-ing in; failure modes don't show up in any dashboard; restart requires a human at a terminal; auth state lives in unaudited shell profiles.

The fix is to move the control plane off-cluster, into the same host that runs the API and Postgres (onclappc02). One process, one place to monitor, one place to redeploy. SSH into the cluster (`ssh + ControlPersist`) replaces "log in to tmux."

But we also need to preserve the option of an on-cluster daemon, because we can't always SSH *in*. Long-term, the project will run on private clusters whose admins won't permit inbound SSH from an external orchestrator — same workload, but the control plane has to live inside the cluster's network. So this design supports **two deployment modes**, sharing as much code and identical API contracts.

## Two modes

```
                     ┌──────────────────────────────────┐
                     │  Telemetry API + Postgres        │
                     │  (onclappc02)                    │
                     │                                  │
                     │  jobs:    pending → claimed →    │
                     │           submitted → running →  │
                     │           completed | failed     │
                     └──────────┬───────────────────────┘
                                │
            ┌───────────────────┴───────────────────┐
            │                                       │
            ▼                                       ▼
    ┌───────────────────┐                ┌─────────────────────┐
    │  Push mode        │                │  Pull mode          │
    │  Orchestrator     │                │  nf-client          │
    │  on onclappc02    │                │  on cluster login   │
    │                   │                │                     │
    │  SSH → sbatch     │                │  local sbatch       │
    │  SSH → sacct      │                │  local sacct        │
    └───────┬───────────┘                └─────────┬───────────┘
            │                                       │
            ▼                                       ▼
        login node                              login node
            │                                       │
            ▼                                       ▼
        SLURM ─────────────► compute nodes ──────► weblog HTTPS push
                                                       │
                                                       └──► API
```

Both modes:

- Claim work via `POST /api/dispatch/batch`
- Confirm submission via `POST /api/dispatch/submitted`
- Send daemon heartbeats and run-lifecycle events
- Drive the same `jobs_tbl` state machine

The only thing that differs is **where the daemon process lives** and **who initiates the SSH** (or whether SSH is involved at all). The API code is unchanged across modes.

### Push mode (preferred where SSH is available)

- One process on onclappc02 per cluster (or one process iterating clusters in series — TBD by polling cost).
- Holds a ControlPersist'd SSH connection to the cluster's login node. Connection persists 10 min idle.
- For each poll cycle: claim batch → ssh-submit `sbatch wrapper.sh` → record SLURM jobid → ssh-poll `sacct` on the standard interval.
- Daemon is supervised by systemd on onclappc02. Failures are visible (`systemctl status`, journald, alerts).
- Credentials: per-cluster SSH key on onclappc02, owned by a single service user. Keys are added to each cluster's `authorized_keys` once.

### Pull mode (required where SSH-in isn't possible)

- One `nf-client` process on the cluster's login node, supervised by whatever the cluster permits (tmux/screen/cron @reboot/user-systemd if available).
- Same code path as today.
- Polls the API for claimable work; submits locally; polls `sacct` locally.
- Used for: private collaborator clusters; ACCESS sites that refuse inbound SSH; air-gapped or firewalled environments.

### When to use which

| Situation | Mode |
|---|---|
| ACCESS clusters we have SSH access to (Anvil, Alpine today) | push |
| ACCESS clusters with restrictive inbound SSH (Bridges, TACC — TBD) | push if feasible, else pull |
| Private collaborator clusters running private data | pull |
| Local laptop / dev cluster | either (push usually simpler) |

A cluster's mode is a deployment-time choice, not a per-job choice. The same cluster won't switch modes day to day.

## Push-mode design

### Where it runs

Same host as API + Postgres + frontend (`onclappc02`). New systemd unit:

```
/etc/systemd/system/nf-orchestrator.service
```

One process, supervised. Restart-on-failure. Logs to journald. The orchestrator is a peer of the API container, not part of it — separate `docker-compose` service. Shares the `.env` for `SQLALCHEMY_URI` so it can write directly to Postgres or call the API, whichever proves simpler (likely API for consistency).

### SSH posture

`~/.ssh/config` on the service account:

```
Host anvil
    HostName login07.anvil.rcac.purdue.edu
    User x-seandavi
    IdentityFile /etc/nf-orchestrator/keys/anvil_ed25519
    ControlMaster auto
    ControlPersist 10m
    ControlPath /run/nf-orchestrator/cm-%r@%h:%p
    ServerAliveInterval 30
    ServerAliveCountMax 4

Host alpine
    HostName login-ci4.rc.colorado.edu
    User seandavi@xsede.org
    IdentityFile /etc/nf-orchestrator/keys/alpine_ed25519
    ProxyJump dccapp720
    ControlMaster auto
    ControlPersist 10m
    ControlPath /run/nf-orchestrator/cm-%r@%h:%p
```

`ControlPath` lives in a tmpfs (`/run/...`) so it doesn't survive reboot. ControlPersist keeps the connection warm during normal operation; the first command after a restart pays a single auth roundtrip.

### Per-poll work

Pseudocode:

```python
async def poll_cluster(cfg: ClusterConfig) -> None:
    batch = await api.claim_batch(workflow_filter=cfg.workflow_filter,
                                  size=cfg.batch_size)
    for job in batch:
        wrapper = render_wrapper(job, cfg)
        slurm_id = await ssh.run(cfg.host, f"sbatch <<< '{wrapper}'")
        await api.mark_submitted(job.id, slurm_id)

    # On its own cadence (e.g. every 5 min), not every poll:
    if time_to_poll_sacct(cfg):
        states = await ssh.run(cfg.host,
            f"sacct -j {','.join(active_ids)} --format=JobID,State,... --parsable2")
        for jobid, state in parse_sacct(states):
            await api.report_slurm_state(jobid, state)
```

The shape is essentially what `nf-client` does today, with `ssh.run(host, cmd)` substituted for local subprocess.

### Cluster config

YAML, one file per cluster, mounted into the orchestrator container:

```yaml
host: anvil
workflow_filter:
  - cmgd_nextflow
  - cmgd_nextflow_with_rarefaction
batch_size: 25
max_concurrent_runs: 10
sacct_interval_seconds: 300
submit_rate_limit_seconds: 10

submission:
  template_path: templates/submit_anvil.sh.j2
  defaults:
    partition: shared
    time: "23:00:00"
    mem: 2G
    cpus: 1
    log_dir: /anvil/scratch/x-seandavi/cmgd/job_logs
    singularity_cache: /anvil/scratch/x-seandavi/apptainer_cache
    nxf_home: /anvil/scratch/x-seandavi/nf_home
```

Shape matches today's `client-alpine.yaml` / `client-anvil.yaml` — deliberately, so the code path is one substitution away from current. No adapter framework on day one; one cluster runner with config-driven differences.

## Pull-mode design

Unchanged from today. Documented in [`hpc-deployment.md`](./hpc-deployment.md). The `packages/nf_client` package stays as-is; its install path (cluster login node, supervised in tmux/screen/user-systemd) remains supported. No deprecation timeline.

The only ongoing investment in pull mode is keeping it pinned to the same dispatch API contract as push mode. As long as both modes call the same `/api/dispatch/*` endpoints, they're interchangeable from the server's view, and we can deploy whichever fits the cluster's access posture.

## Telemetry path is unchanged in both modes

Compute nodes already POST per-task events to the API via Nextflow's `-with-weblog`. That path stays as-is:

```
compute node ──(HTTPS POST /telemetry)──► API
```

The orchestrator's `sacct` polling is a **scheduler-state** stream, complementary to weblog's **task-state** stream. Don't conflate them: weblog gives us per-process cpu/mem/exit at the moment the task finishes; sacct gives us the SLURM-side view (queued/running/failed/OOM) at 5-minute resolution. Both are needed for the current dashboards.

## Open questions

1. **SSH on PSC Bridges / TACC**: do they permit long-lived passwordless SSH from an external IP, or require Duo / SSH certs / Globus auth? Reconnaissance question, not a design question — but the answer determines whether those sites get push or pull mode. Defer until we actually have an allocation there.
2. **Service-account credentials on each cluster**: today the keys are personal (your user account on Anvil, your user account on Alpine). Long-term, a shared service account per cluster is cleaner — survives team changes, can be audited. But ACCESS allocations are user-bound, so this may not be feasible at every site.
3. **Orchestrator-to-API: REST or direct DB?** Same process boundary on onclappc02 means we could write to Postgres directly. Cleaner to keep going through the API — one source of business logic, no risk of orchestrator and API drifting in their state-machine interpretation. Lean: REST.
4. **Adapter abstraction**: not on day one. Two clusters with config-driven differences is below the threshold for polymorphism. The third cluster (whether ACCESS or private) is the right time to decide whether a `ClusterAdapter` interface is paying for itself.
5. **What happens when SSH is down**: orchestrator retries with backoff; nothing dispatches; `pending` queue grows. Eventually SSH recovers and dispatch resumes. No buffering needed beyond what the existing `jobs_tbl` already provides. Worth exercising in a test, but no code changes implied.
6. **ControlPersist + Duo interaction at non-Anvil/Alpine sites**: ControlPersist saves you within its window, but the moment the daemon restarts you re-auth. If a site requires interactive Duo per fresh SSH, push mode is effectively unusable there.

## What this design explicitly does NOT do

- No queue middleware (NATS / RabbitMQ / SQS). The existing `jobs_tbl pending` query *is* the queue. Adding a broker on top of a SQL queue at this scale is pure complexity.
- No replacement of weblog push with sacct pull. They observe different layers; keep both.
- No `ClusterAdapter` framework until the third cluster forces real polymorphism.
- No deprecation of pull mode. Both modes are first-class.
- No change to the dispatch API contract. The whole point is that the API is mode-agnostic.

## Suggested next steps

Each independently shippable. Smallest first.

1. **SSH reconnaissance from onclappc02** (xs). From `onclappc02`, can we `ssh anvil 'true'` and `ssh alpine 'true'` cleanly using a service-account key? Sets up the rest.
2. **Lift current daemon into push mode** (medium). Most of the work is wrapping the current local `subprocess.run("sbatch ...")` into an `ssh.run(host, "sbatch ...")` over an asyncssh connection. ~200 lines of new code; the rest is config restructuring.
3. **Systemd unit + docker-compose entry** for `nf-orchestrator` on onclappc02 (small).
4. **Parity validation** (small): run push-mode orchestrator and one of the existing pull-mode daemons in parallel against the same cluster but disjoint `workflow_id` filters, compare event streams.
5. **Decommission Anvil + Alpine tmux daemons** (xs), once push-mode parity is proven.
6. **Revisit on cluster #3** (deferred): if it's another ACCESS site with SSH access, just add a config file. If it's a private cluster, deploy pull mode there. If it's something weirder (Bridges with Duo-per-auth?), that's when the adapter abstraction conversation happens.
