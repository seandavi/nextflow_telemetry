# 0002. Run death classification

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** Sean Davis

## Context

When a Nextflow run dies, the raw signals are ambiguous. A driver (the
`nf-client` wrapper sbatch job running the Nextflow process) that is hard-killed
— OOM, `scancel`, node failure, or its own wall-time — cancels all in-flight
task jobs. Those tasks surface as `ABORTED` with **zero `FAILED`**, and the
wrapper never gets to upload its `.nextflow.log`. From the telemetry alone this
is indistinguishable from a healthy run at a glance, and diagnosing it meant
SSHing to the cluster and running `sacct` by hand.

The `workflow_runs` table already records the signals needed to disambiguate:
`status`, `wrapper_exit_code`, `last_heartbeat_at`, and
`nextflow_log_uploaded_at`. They were just not interpreted anywhere.

## Decision

We will derive a single **`classification`** for each run from its
`workflow_runs` columns and expose it on `GET /runs/` and `GET /runs/{name}`.
The values and their precedence:

- **`wrapper-failed`** — `wrapper_exit_code` is non-zero (authoritative even if
  `status` never advanced; the driver itself failed).
- **`stalled`** — non-terminal status but no heartbeat for > 15 min.
- **`active`** — non-terminal with a recent heartbeat.
- **`ended-no-log`** — terminal but no `.nextflow.log` was ever uploaded: the
  signature of a driver/allocation hard-kill before its exit handler ran.
- **`completed` / `failed` / `expired`** — otherwise, mirroring `status`.

The run detail endpoint also returns per-run task-status counts, so an
all-`ABORTED`/zero-`FAILED` run is visibly a driver death, not a pipeline bug.

## Alternatives considered

- **Expose raw `status` only** — the status quo. Rejected: it cannot express
  "terminal but the driver was killed", which is the case that actually cost
  debugging time.
- **Compute the classification client-side in the frontend** — rejected: the
  rule should be one authoritative definition usable by any consumer (UI, alerts,
  scripts), not duplicated per client.
- **Infer death purely from task ABORTED/FAILED counts** — rejected as the
  primary signal: it requires scanning telemetry events and misses runs that
  died before emitting any, whereas `workflow_runs` columns are always present.

## Consequences

- The two driver-death shapes (`wrapper-failed`, `ended-no-log`) are now visible
  in the app, pointing the operator at SLURM instead of leaving them to guess.
- The classification is a derived view, not stored — it always reflects current
  columns and needs no migration, but it is only as good as those columns
  (a run that never reports a heartbeat or exit code leans on `ended-no-log`).
- The 15-minute stall threshold is a heuristic that may need per-site tuning.

## References

- `src/nextflow_telemetry/routers/runs.py` (`_classify_run`, `GET /runs`).
- seandavi/nextflow_telemetry#105 (implementation + Runs page).
- Related pipeline-side policy: `curatedMetagenomicsNextflow` ADR-0009.
