# 0003. Dispatchability detection (pending work with no active daemon)

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** Sean Davis

## Context

In pull-mode ([0001](0001-pull-mode-orchestration.md)), pending jobs are only
worked if an active daemon claims them. A daemon can be absent for ordinary
reasons — it was stopped, it crashed, or its `workflow_id` claim filter does not
include the workflow that has pending work. When that happens, jobs sit in
`pending` indefinitely while every individual subsystem looks healthy. This
exact situation was misread as a reconciliation bug and cost an hour: reconcile
correctly created the jobs, but nothing was claiming them because the cluster
daemon had stopped heartbeating.

The pieces to detect it already exist: pending counts per active workflow, and
daemon heartbeats with their claim filters (a daemon is "active" if seen within
the last 2 minutes; its `workflow_id` is a comma list, or empty = claims any).

## Decision

We will add `GET /admin/dispatchability`, which joins pending jobs on **active**
workflows against **active** daemons and their claim filters, and returns the
workflows that have pending work no active daemon will claim. The Overview page
shows a banner when this set is non-empty. This is the first-class answer to
"why isn't anything running?".

## Alternatives considered

- **Rely on the daemon list page** — the staleness was already shown there, yet
  still missed because it required knowing to look and cross-referencing against
  pending work. Rejected as insufficient: the signal must be pushed to the
  Overview, pre-correlated with pending jobs.
- **Alert purely on daemon staleness** — rejected: a stale daemon with no
  pending work for it is not a problem; the actionable condition is pending work
  *and* no claimant. Correlating the two removes false alarms.
- **Block/winddown on the condition** — rejected: detection and surfacing is
  enough; the operator decides whether to restart a daemon or adjust filters.

## Consequences

- Stuck-pending situations surface immediately and in plain language, instead of
  presenting as a phantom reconcile/dispatch bug.
- The check is computed on demand from current rows; no new state, no migration.
- It depends on the 2-minute daemon-active threshold staying in sync with the
  daemons router (a shared constant / documented coupling).
- It only flags *active* workflows; pending jobs on retired/paused workflows are
  intentionally ignored (they are not meant to run).

## References

- `src/nextflow_telemetry/routers/admin.py` (`GET /admin/dispatchability`).
- seandavi/nextflow_telemetry#104 (implementation + Overview banner).
- [0001](0001-pull-mode-orchestration.md) (why daemon liveness is load-bearing).
