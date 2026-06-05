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
  isn't one. Check SLURM directly: `sacct -j <driver_jobid> --format=JobID,State,ExitCode,MaxRSS,ReqMem`. Exit `1:0` / `OUT_OF_MEMORY` → bump driver mem in the
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

```bash
API=https://nf-telemetry.cancerdatasci.org
curl -s -X POST "$API/api/admin/requeue-dead-letter"             # DLQ → pending
curl -s -X POST "$API/api/admin/expire-stale-runs?older_than_hours=2"  # close zombies, sweep
curl -s -X POST "$API/api/admin/reset-running"                   # last resort: running → pending
```
After requeue/reset, run `reconcile-jobs` is NOT needed (jobs already exist); the
daemon will reclaim on its next poll. Confirm via job-summary.

## Gotchas

- `errorStrategy` is `finish` (since 2.0.4): one bad task no longer aborts the
  batch — so `wrapper-failed` with a high `COMPLETED` count is "almost worked,"
  not "catastrophe."
- Per-task `.command.out` (stdout) is uploaded since pipeline 2.0.1; before that,
  stdout-only tools (kraken2) were log-less. If a run predates 2.0.1, missing
  stdout is expected.
- A job is only `completed` when the `MARK_COMPLETE` sentinel process emits
  `status=COMPLETED`. All tasks green but no MARK_COMPLETE ⇒ job won't flip to
  completed.
