"""Job + run lifecycle transitions — the single owner of status writes.

This module is the ONE place that writes the ``status`` column of
``jobs_tbl`` and ``workflow_runs_tbl``. If you are about to write
``update(jobs_tbl).values(status=...)`` or
``update(workflow_runs_tbl).values(status=...)`` anywhere else in this
codebase, stop — add a function here instead and call it.

Vocabulary — the two enums below are the complete state sets. DB columns
stay plain ``String`` (no migration); the enums exist for the Python call
sites, not the schema.

    JobStatus: pending, claimed, submitted, running, completed, failed
    RunStatus: claimed, submitted, running, completed, expired, failed

Connection seam: every function takes ``conn: AsyncConnection`` as its
first argument and does not manage the transaction — the caller opens
``async with engine.begin() as conn:`` and passes it in, so multiple
lifecycle calls (and other statements) can share one atomic transaction.
No function here holds an ``AsyncEngine``.

Tolerant / guarded / idempotent contract: every write here encodes its
legal source state(s) in the ``WHERE`` clause. An event that arrives for a
job/run already past that state (or in some other state entirely) matches
zero rows and is a silent no-op — it NEVER raises. This is what lets the
callers absorb out-of-order or duplicate weblog/wrapper events without
special-casing them.

Threshold-free and HTTP-agnostic: nothing here reads a clock or holds a
policy constant (retry budgets live on ``workflows_tbl.max_retries``,
expiry windows live in the caller). Every function that needs "now" or a
cutoff takes it as a parameter. Return values are plain data (bool, int,
str | None) — routers are responsible for turning "no match" into a 404
where that's the right HTTP behaviour; this module itself never touches
HTTP.

Deliberate CARVE-OUTS — NOT owned by this module:
  - Job *birth*: creating the initial ``pending`` rows is
    ``ReconcileService.reconcile_jobs`` (services/reconcile.py). That's an
    INSERT, not a status transition, and reconciliation policy (the
    samples x active-workflows cross-product) doesn't belong here.
  - The retire-time pending-job purge (deleting still-pending jobs when a
    workflow version is retired) lives in ``WorkflowService``
    (services/workflow.py) — it's a DELETE tied to workflow lifecycle, not
    a job status transition.
  - Unrelated ``status`` columns on other tables (``task_executions``,
    ``submissions``, ``daemons``/``daemon_agents``, ``workflows``) are out
    of scope — this module only touches ``jobs_tbl.status`` and
    ``workflow_runs_tbl.status`` (plus the handful of columns that go with
    those transitions: run_name, retry_count, timestamps, dead_letter).
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TypedDict

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db import dead_letter_tbl, jobs_tbl, workflow_runs_tbl, workflows_tbl


class JobStatus(StrEnum):
    pending = "pending"
    claimed = "claimed"
    submitted = "submitted"
    running = "running"
    completed = "completed"
    failed = "failed"


class RunStatus(StrEnum):
    claimed = "claimed"
    submitted = "submitted"
    running = "running"
    completed = "completed"
    expired = "expired"
    failed = "failed"


# Run statuses that count as "already closed" — a run in one of these states
# has already gone through its terminal write. Public so callers (e.g.
# admin.close_run) test membership against this set instead of re-listing the
# strings, keeping the terminal vocabulary owned in one place.
RUN_TERMINAL_STATUSES = frozenset(
    {RunStatus.completed, RunStatus.failed, RunStatus.expired}
)

_SWEEP_REASON_DEFAULT = "run completed without MARK_COMPLETE"


class RunFields(TypedDict):
    """Fields for the workflow_runs row created by `claim`."""

    workflow_id: str
    workflow_version: str
    workflow_pk: int
    revision: str | None
    claimed_at: datetime


async def claim(
    conn: AsyncConnection,
    job_ids: list[int],
    run_name: str,
    run_fields: RunFields,
) -> None:
    """Create a workflow_runs row in `claimed` and claim the given jobs.

    Mirrors routers/dispatch.py `dispatch_batch`'s claim step.
    """
    await conn.execute(
        workflow_runs_tbl.insert().values(
            run_name=run_name,
            workflow_id=run_fields["workflow_id"],
            workflow_version=run_fields["workflow_version"],
            workflow_pk=run_fields["workflow_pk"],
            revision=run_fields["revision"],
            status=RunStatus.claimed,
            claimed_at=run_fields["claimed_at"],
        )
    )
    await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.id.in_(job_ids))
        .values(run_name=run_name, status=JobStatus.claimed)
    )


async def mark_submitted(
    conn: AsyncConnection,
    run_name: str,
    executor_job_id: str | None,
    now: datetime,
) -> bool:
    """Transition a run + its jobs from `claimed` to `submitted`.

    Returns True iff a workflow_runs row matched (was in `claimed`) and was
    updated; the caller maps False to a 404. Mirrors routers/dispatch.py
    `report_submitted`.
    """
    result = await conn.execute(
        update(workflow_runs_tbl)
        .where(
            workflow_runs_tbl.c.run_name == run_name,
            workflow_runs_tbl.c.status == RunStatus.claimed,
        )
        .values(
            status=RunStatus.submitted,
            submitted_at=now,
            executor_job_id=executor_job_id,
        )
        .returning(workflow_runs_tbl.c.run_name)
    )
    matched = result.fetchone() is not None
    if not matched:
        return False

    # Advance jobs from `claimed` to `submitted` — distinct from `running`,
    # which is set only when the weblog `started` event arrives.
    await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.status == JobStatus.claimed,
        )
        .values(status=JobStatus.submitted)
    )
    return True


async def mark_running(
    conn: AsyncConnection,
    run_name: str,
    run_id: str | None,
    now: datetime,
) -> None:
    """Run + jobs -> `running`, on receipt of the weblog `started` event.

    Jobs reach `running` from either `submitted` (normal flow) or `claimed`
    (defensive: out-of-order events). Mirrors services/telemetry.py's
    `started` handling.

    The run update is guarded against terminal states: a late/duplicate
    `started` event for a run the watchdog already closed (failed/expired) or
    that already completed will NOT resurrect it to `running`. Same
    no-clobber contract as close_run.
    """
    await conn.execute(
        update(workflow_runs_tbl)
        .where(
            workflow_runs_tbl.c.run_name == run_name,
            workflow_runs_tbl.c.status.notin_(RUN_TERMINAL_STATUSES),
        )
        .values(run_id=run_id, status=RunStatus.running, started_at=now)
    )
    await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.status.in_([JobStatus.claimed, JobStatus.submitted]),
        )
        .values(status=JobStatus.running)
    )


async def complete_sample(
    conn: AsyncConnection,
    run_name: str,
    sample_id: str,
    now: datetime,
) -> int:
    """One job (matched by run_name + sample_id) -> `completed`.

    Fires on the MARK_COMPLETE sentinel process. Returns the number of rows
    updated (0 or 1 in practice, given the uq_job_composite constraint).

    Guarded against terminal job states: a late MARK_COMPLETE will not flip a
    job that already `failed` (which would leave a `completed` row with
    failure fields still populated) or re-touch one already `completed`.
    """
    result = await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.sample_id == sample_id,
            jobs_tbl.c.status.notin_([JobStatus.completed, JobStatus.failed]),
        )
        .values(status=JobStatus.completed, completed_at=now)
    )
    return result.rowcount or 0


async def close_run(
    conn: AsyncConnection,
    run_name: str,
    terminal: RunStatus,
    now: datetime,
    reason: str | None = None,
) -> str | None:
    """Set a run's status to a terminal value, unless already terminal.

    Returns the PRIOR status (before this call), or None if no such run
    exists. Callers use the return value to distinguish "closed just now"
    from "was already closed" (see routers/admin.py `close_run`).
    Idempotent: if the run is already in a terminal state
    (completed/failed/expired), this is a no-op write (the prior status is
    still returned so the caller can report it).

    The row is locked FOR UPDATE for the duration of the caller's transaction
    so that a watchdog `close_run(..., failed)` and a telemetry
    `close_run(..., completed)` racing in separate transactions can't both
    read a non-terminal status and lost-update each other — the second waits,
    sees the now-terminal status, and no-ops.
    """
    row = (
        await conn.execute(
            select(workflow_runs_tbl.c.status)
            .where(workflow_runs_tbl.c.run_name == run_name)
            .with_for_update()
        )
    ).first()
    if row is None:
        return None
    prior_status = row[0]

    if prior_status in RUN_TERMINAL_STATUSES:
        return prior_status

    values: dict = {"status": terminal, "completed_at": now}
    if reason is not None:
        values["slurm_reason"] = reason

    await conn.execute(
        update(workflow_runs_tbl)
        .where(workflow_runs_tbl.c.run_name == run_name)
        .values(**values)
    )
    return prior_status


async def sweep_incomplete(
    conn: AsyncConnection,
    run_name: str,
    now: datetime,
    reason: str = _SWEEP_REASON_DEFAULT,
) -> int:
    """Sweep non-completed jobs for a run: retry within budget or send to DLQ.

    Jobs where retry_count < max_retries are reset to `pending` with
    run_name=NULL so they re-enter the dispatch pool. Jobs that have
    exhausted retries are marked `failed` and written to the dead-letter
    table. Returns the number of jobs swept.

    Idempotent: jobs already in a terminal state (completed, failed) are
    not touched. Moved verbatim from services/reconcile.py
    `sweep_run_incomplete`.
    """
    max_retries_subq = (
        select(workflows_tbl.c.max_retries)
        .where(workflows_tbl.c.id == jobs_tbl.c.workflow_pk)
        .scalar_subquery()
    )
    has_retries = jobs_tbl.c.retry_count < max_retries_subq

    result = await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.run_name == run_name,
            jobs_tbl.c.status.in_(
                [JobStatus.running, JobStatus.claimed, JobStatus.submitted]
            ),
        )
        .values(
            retry_count=jobs_tbl.c.retry_count + 1,
            status=case((has_retries, JobStatus.pending), else_=JobStatus.failed),
            run_name=case((has_retries, None), else_=jobs_tbl.c.run_name),
            failed_at=case((has_retries, None), else_=now),
            failure_reason=case(
                (has_retries, None),
                else_=reason,
            ),
        )
        .returning(
            jobs_tbl.c.id,
            jobs_tbl.c.sample_id,
            jobs_tbl.c.workflow_id,
            jobs_tbl.c.workflow_version,
            jobs_tbl.c.status,
        )
    )
    swept = result.mappings().all()

    dlq_rows = [r for r in swept if r["status"] == JobStatus.failed]
    if dlq_rows:
        await conn.execute(
            pg_insert(dead_letter_tbl)
            .values(
                [
                    {
                        "job_id": row["id"],
                        "run_name": run_name,
                        "sample_id": row["sample_id"],
                        "workflow_id": row["workflow_id"],
                        "workflow_version": row["workflow_version"],
                        "reason": reason,
                        "created_at": now,
                    }
                    for row in dlq_rows
                ]
            )
            .on_conflict_do_nothing(constraint="uq_dlq_job_id")
        )

    return len(swept)


async def requeue_expired(conn: AsyncConnection, cutoff: datetime) -> int:
    """Expire stale `claimed` runs and reset their jobs to `pending`.

    Mirrors routers/dispatch.py `requeue_expired`. Returns the count of
    runs expired.
    """
    result = await conn.execute(
        update(workflow_runs_tbl)
        .where(
            workflow_runs_tbl.c.status == RunStatus.claimed,
            workflow_runs_tbl.c.claimed_at < cutoff,
        )
        .values(status=RunStatus.expired)
        .returning(workflow_runs_tbl.c.run_name)
    )
    expired_run_names = [r[0] for r in result.fetchall()]

    if expired_run_names:
        await conn.execute(
            update(jobs_tbl)
            .where(
                jobs_tbl.c.run_name.in_(expired_run_names),
                jobs_tbl.c.status == JobStatus.claimed,
            )
            .values(status=JobStatus.pending, run_name=None)
        )

    return len(expired_run_names)


async def requeue_dead_letter(
    conn: AsyncConnection,
    job_ids: list[int],
    dlq_ids: list[int],
    now: datetime,
) -> int:
    """Reset dead-lettered jobs to `pending` and mark their DLQ rows resolved.

    The caller (routers/admin.py `requeue_dead_letter`) selects the
    unresolved `dead_letter` rows and passes their job/dlq ids in; this
    function performs the two writes. Returns len(job_ids) (mirrors the
    original endpoint's return value).
    """
    if not job_ids:
        return 0

    await conn.execute(
        update(jobs_tbl)
        .where(jobs_tbl.c.id.in_(job_ids))
        .values(
            status=JobStatus.pending,
            retry_count=0,
            run_name=None,
            failed_at=None,
            failure_reason=None,
        )
    )
    await conn.execute(
        update(dead_letter_tbl)
        .where(dead_letter_tbl.c.id.in_(dlq_ids))
        .values(resolved_at=now)
    )
    return len(job_ids)


async def reset_jobs_to_pending(
    conn: AsyncConnection,
    workflow_pk: int,
    from_statuses: list[JobStatus],
) -> int:
    """Reset jobs for a workflow from any of `from_statuses` back to `pending`.

    Mirrors routers/admin.py `reset_running` (called with
    [JobStatus.running, JobStatus.failed]). Returns the number of rows
    updated.
    """
    result = await conn.execute(
        update(jobs_tbl)
        .where(
            jobs_tbl.c.workflow_pk == workflow_pk,
            jobs_tbl.c.status.in_(from_statuses),
        )
        .values(
            status=JobStatus.pending,
            run_name=None,
            retry_count=0,
            failed_at=None,
            failure_reason=None,
        )
    )
    return result.rowcount or 0
