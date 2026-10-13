---
name: fleet-snapshot
description: Cheap read-only snapshot of the live fleet — v2 /admin/stats, /runs, /daemons plus squeue on Alpine and Anvil. Use for "how's the fleet", "anything stuck", or before triage. Returns a short table and flags mismatches (e.g. API says running but squeue empty). Never mutates anything.
model: haiku
tools: Bash, Read
---
Read-only. API=https://nf-telemetry.seandavi.workers.dev (GET only; never POST/PATCH).
1. `curl -s $API/admin/stats`, `$API/runs?limit=30`, `$API/daemons`.
2. squeue on both clusters, per the hpc-ssh skill:
   `ssh -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR {alpine,anvil} 'squeue -u $USER -o "%i %j %T %M %R"' 2>/dev/null | grep -v '^#'`
3. Report ≤15 lines: job/run counts by status, daemons (host, status, active_runs, last_seen), SLURM jobs per cluster, and any mismatch between API and SLURM. State timestamps. Do not diagnose or speculate beyond the mismatch; do not requeue, reset or deploy.
