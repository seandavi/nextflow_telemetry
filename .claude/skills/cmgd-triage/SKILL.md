---
name: cmgd-triage
description: >
  Diagnose failed, stuck, or log-less Nextflow runs in the cmgd telemetry
  system. Use when runs are failing, jobs are stuck pending, there are dead-letter
  jobs, a run shows wrapper-failed or ended-no-log, tasks ABORTED with no logs, or
  the user says "lots of failures" / "no logs coming back" / "why isn't it
  running". Walks classification → root cause → the right log to pull.
  Trigger words: failed, failure, stuck, DLQ, dead letter, wrapper-failed,
  ended-no-log, ABORTED, no logs, requeue, why did it die, triage, debug run.
---

# cmgd run triage

Diagnose why cmgd_nextflow runs fail or stall. Uses `telemetry-api` for all
queries. Start with `scripts/triage.py <run_name>` for a one-shot dump, or follow
the decision tree below.

## Step 0 — orient

```bash
API=https://nf-telemetry.cancerdatasci.org
curl -s "$API/api/workflows?status=active" | python3 -m json.tool     # which pk is live
curl -s "$API/api/workflows/<pk>/job-summary" | python3 -m json.tool  # counts
curl -s "$API/api/admin/dispatchability" | python3 -m json.tool       # is it even being claimed
curl -s "$API/api/runs/?limit=80" | python3 -c 'import sys,json;from collections import Counter;r=json.load(sys.stdin);print(Counter(x["classification"] for x in r))'
```

## Fleet health report (read-only, unattended-safe)

A "how's it going + what's failing" sweep that **mutates nothing** — safe to run
on a schedule with no human present. Do NOT call the Recovery POSTs below in an
unattended run. Cluster-side detail (`sacct`/`squeue`) needs SSH — see
`alpine-daemon` → **Cluster inventory**.

**Ready-made:** `scripts/health_report.py` runs this whole sweep, applies the
signal-vs-noise rules below, prints a report, and exits `0`=GREEN / `1`=ATTENTION
(a regression signal fired) / `2`=API unreachable — the natural hook for an LLM
deep-dive. Stdlib only. `scripts/run_daily_report.sh` wraps it for scheduling
(logs to `~/.cmgd-health/`, drops `attention-latest.txt` when non-green) and, via
`scripts/post_github.py`, publishes to a **per-(workflow, version) GitHub tracking
issue** in `seandavi/nextflow_telemetry`:

- **One issue per version** (`cmgd_nextflow 2.2.1 — fleet health`), auto-rotating
  when a new version goes active after a release (the old issue is closed with a
  pointer). The issue is that version's whole-rollout health record.
- **Body is rewritten each run** (status + rolling table + latest report) —
  editing an issue body does **not** notify: a silent daily heartbeat.
- **A comment is posted only on non-green or recovery**, `@`-mentioning on
  ATTENTION/ERROR — so notification volume == bad days. Toggle with `CMGD_GITHUB=0`;
  override `CMGD_REPO` / `CMGD_MENTION`. `gh` token in `~/.config/gh/hosts.yml`
  works headless.
- **A flipping `status:green|attention|error` label** is kept in sync each run
  (label edits don't notify) so the issue list / project board is scannable at a
  glance. Set `CMGD_PROJECT=<number>` to auto-add each new version-issue to a
  GitHub Project; group that board's Board view by **Labels** (view creation is
  UI-only). Current deployment: project 19, owner `seandavi`.

On the onclappc02-adjacent workstation the wrapper runs as the
`cmgd-health-report.timer` systemd **user** timer (daily 08:00 MT, linger on;
`systemctl --user list-timers`). The timer runs a stable copy under
`~/.local/share/cmgd-health/`, so **re-copy all three scripts there after editing
them here**. The manual recipe below is the same read-only sweep by hand:

```bash
API=https://nf-telemetry.cancerdatasci.org
PK=$(curl -s "$API/api/workflows?status=active" | python3 -c 'import sys,json;print([w["id"] for w in json.load(sys.stdin) if w["workflow_id"]=="cmgd_nextflow"][0])')
V=$(curl -s "$API/api/workflows/$PK" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')

curl -s "$API/api/workflows/$PK/job-summary"                    # completion %, failed, dead_letter (authoritative)
curl -s "$API/api/admin/stats"                                  # global; watch dead_letter_unresolved trend
curl -s "$API/api/admin/dispatchability"                        # cmgd_nextflow must NOT be in stuck[]
curl -s "$API/api/daemons/"                                     # both cluster daemons: fresh last_seen_at
curl -s "$API/api/metrics/processes/running"                    # what stage the batch is in
curl -s "$API/api/metrics/processes/failure-signatures?workflow_version=$V&window_hours=24"
curl -s "$API/api/metrics/processes/timeline?workflow_version=$V&bucket=hour&window_hours=24"   # spike vs steady
# per suspect process, is the retry recovering? (failures by attempt)
curl -s "$API/api/metrics/processes/tasks?workflow_version=$V&process=<p>&status=FAILED&window_hours=24&limit=200"
```

Interpret failures against **Expected background failures** below before flagging
anything. `dead_letter_unresolved` in `/stats` is **global across all versions** —
compare to the active workflow's own `job-summary.dead_letter` to isolate the
current batch.

## Expected background failures (signal vs noise)

At 100k scale a few % of tasks fail-and-recover every hour. These are **normal**
— a daily report should NOT page on them:

- **metaphlan `*` exit 137 (OOM), action RETRY.** `bowtie2-align` OOM-killed
  (stderr: `bowtie2-align exited with value 137` + a downstream `BrokenPipeError`).
  Memory scales `N.GB * task.attempt`, so the retry gets more RAM and succeeds.
  **Healthy iff the failures are attempt-1 only** (check the `attempt` field). Page
  only if attempt-2+ failures appear (scaling not keeping up) or its DLQ grows.
- **fasterq_dump exit 3 (SRA/ENA download).** Interrupted read download; retried,
  then per-sample `ignore`. Persistently-undownloadable accessions exhaust job
  retries → land in the **DLQ**. That's a *data* problem (withdrawn/embargoed
  accession), not a pipeline bug. Page only if the *rate* spikes (ENA/SRA outage)
  or one study is ~100% failing.
- **Baseline:** ~0.5–2%/hr steady task-failure is normal; a `timeline` spike
  usually tracks a stage transition into compute-heavy metaphlan/resistome — cross
  check `failure-signatures` before assuming an incident.

**Regression signals — DO investigate:** `resistome_kma_*` failing at all (fixed
2.2.1, expect 0); any process ~100% failing across many samples; a brand-new exit
code dominating a process; DLQ climbing fast; a cluster daemon with a stale
`last_seen_at`; `cmgd_nextflow` in `dispatchability.stuck`.

## Decision tree by classification

### `wrapper-failed` (wrapper_exit_code ≠ 0)
The Nextflow driver finished but exited non-zero — i.e. at least one task failed
and `errorStrategy = finish` let siblings drain before exit.
1. `GET /api/runs/{run_name}` → read `task_status_counts`. The signal is the small
   `FAILED` count amid many `COMPLETED` (e.g. `{COMPLETED:130, FAILED:1}` = the
   pipeline ran nearly to the end and died on one process).
2. Find the failing process, pull its task logs:
   `GET /api/task-logs/{run_name}/{task_hash}?log_type=command_err` (and
   `command_out` — kraken2/metaphlan report on **stdout**, so `.command.err` may be
   empty).
3. The `.nextflow.log` is attached on the `wrapper_exited` event when present.

### `ended-no-log` (terminal, but no `.nextflow.log` uploaded)
The driver died before/without uploading its log — usually the driver job itself
was killed, not a task.
- **Tasks ABORTED + 0 FAILED + no logs ⇒ the driver (wrapper) SLURM job was
  killed, classically OOM.** Do NOT look in telemetry for a failed task — there
  isn't one. Check SLURM directly (SSH to the right cluster — `alpine-daemon` →
  Cluster inventory for the `ssh` alias + user): `sacct -j <driver_jobid> --format=JobID,State,ExitCode,MaxRSS,ReqMem`. Exit `1:0` / `OUT_OF_MEMORY` → bump driver mem in the
  client yaml (`submission.defaults.mem`) and resubmit.

### `stalled` (non-terminal, heartbeat > 15 min old)
Driver alive in SLURM but not heart-beating, or hung.
- `squeue -j <jobid>` — if PENDING, it's queue wait (cluster busy), not a bug; the
  gap between `submitted` and `running` IS the scheduler queue wait.
- If RUNNING but silent, check the wrapper's captured stdout (`wrapper_output_log`).

### stuck `pending` (dispatchability says so)
No active daemon claims this workflow_id.
- Is the daemon up + heart-beating? `GET /api/daemons/`.
- Does the daemon's `dispatch.workflow_id` filter include this workflow?
- Ignore `nf_testing` in the stuck list — that's the retired onclappc02 daemon.

## Container / pipeline failure fingerprints (hard-won)

- **`manifest unknown` at image pull** → a biocontainer tag that doesn't exist.
  Verify the exact tag on quay.io before trusting it (RGI cost us this:
  `6.0.5--pyha8f3691_0` didn't exist; `6.0.5--pyh05cac1d_0` did). One bad tag
  fails the process at pull and (pre-`finish`) terminated the whole run.
- **"no <X> mapping/db found" when the file was definitely downloaded** → a
  `find <staged-symlink-dir>` that won't descend a symlink without `-L`. Nextflow
  stages `path` inputs as symlinks. Reference the file by known name instead of
  `find`-ing for it.
- **`unrecognized arguments` from a vendored `bin/` script** → the container bakes
  an OLDER copy of the same-named script into `/usr/local/bin`, which shadows the
  pipeline's `bin/` on PATH. Confirm with
  `docker run --rm <image> which <script>.py` and `--help`. Fix: rename the
  vendored script to something unique (e.g. `cmgd_*`).

## Recovery actions

**These mutate dispatch state — never run them in an unattended report.** Reserve
for a human-driven recovery; after any of them, confirm via `job-summary`.

```bash
API=https://nf-telemetry.cancerdatasci.org
curl -s -X POST "$API/api/admin/requeue-dead-letter"             # DLQ → pending
curl -s -X POST "$API/api/admin/expire-stale-runs?older_than_hours=2"  # close zombies, sweep
curl -s -X POST "$API/api/admin/reset-running"                   # last resort: running → pending
```
After requeue/reset, run `reconcile-jobs` is NOT needed (jobs already exist); the
daemon will reclaim on its next poll. Confirm via job-summary.

## Gotchas

- **`errorStrategy` is tiered, not plain `finish`.** Per-sample processes: retry
  OOM/kill (exit 137–140) up to 4×, else retry twice, then **`ignore`** — drop
  just that sample so its batch-mates finish. DB-setup and finalize processes
  (publish/manifest, MARK_COMPLETE) end in **`finish`** instead — a broken shared
  DB or failed publish must stop the run rather than silently corrupt output. Net:
  one bad per-sample task no longer aborts the batch, so `wrapper-failed` with a
  high `COMPLETED` count is "almost worked," not "catastrophe."
- Per-task `.command.out` (stdout) is uploaded since pipeline 2.0.1; before that,
  stdout-only tools (kraken2) were log-less. If a run predates 2.0.1, missing
  stdout is expected.
- A job is only `completed` when the `MARK_COMPLETE` sentinel process emits
  `status=COMPLETED`. All tasks green but no MARK_COMPLETE ⇒ job won't flip to
  completed.
