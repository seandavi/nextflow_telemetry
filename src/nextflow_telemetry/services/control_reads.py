"""Control-plane reads: runs, cohorts, stats, dispatchability and the dead-letter list (ADR-0013 #373)."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..control_db import (
    collection_samples_tbl,
    collections_tbl,
    daemons_tbl,
    dead_letter_tbl,
    jobs_tbl,
    run_failed_tasks_tbl,
    run_task_counts_tbl,
    runs_tbl,
    samples_tbl,
    workflows_tbl,
)
from .fleet import ACTIVE_WINDOW
from .registry import collections_of
from .transitions import JOB_INFLIGHT

RUN_STALE = timedelta(minutes=15)
JOB_STATUSES = ("pending", "claimed", "submitted", "running", "completed", "failed")

_LEADERBOARD = text(
    """
    with m as (
      select cs.collection_id, j.sample_id, j.status, j.completed_at
        from collection_samples cs
        join jobs j on j.sample_id = cs.sample_id
        join workflows w on w.id = j.workflow_pk and w.status = 'active')
    select c.collection_id, c.source, c.label,
           (select count(*) from collection_samples x where x.collection_id = c.collection_id) as sample_count,
           count(distinct m.sample_id) filter (where m.status = 'completed') as samples_completed,
           count(distinct m.sample_id) filter (where m.status = 'failed') as samples_failed,
           count(distinct m.sample_id) filter (where m.status in ('claimed','submitted','running')) as samples_running,
           max(m.completed_at) filter (where m.status = 'completed') as last_completed_at
      from collections c left join m on m.collection_id = c.collection_id
     group by c.collection_id
    """
)


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 2) if whole else 0


def classify_run(row: Mapping[Any, Any], now: datetime) -> str:
    """Derived run state: a driver that exited non-zero, a run still queued or stalled, or one that
    ended without ever uploading its .nextflow.log (a hard-killed allocation)."""
    exit_code = row.get("wrapper_exit_code")
    if exit_code is not None and exit_code != 0:
        return "wrapper-failed"
    if row["status"] in JOB_INFLIGHT:
        # No wrapper heartbeat yet: the batch is waiting in the scheduler queue, which can take days.
        if row["status"] != "running" and row.get("last_heartbeat_at") is None:
            return "queued"
        seen = row.get("last_heartbeat_at") or row.get("submitted_at") or row.get("claimed_at")
        return "stalled" if seen is not None and now - seen > RUN_STALE else "active"
    if row.get("nextflow_log_uploaded_at") is None:
        return "ended-no-log"
    return str(row["status"]) if row["status"] else "unknown"


async def list_runs(
    conn: AsyncConnection, *, status: str | None, workflow_id: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    clauses = []
    if status:
        clauses.append(runs_tbl.c.status == status)
    if workflow_id:
        clauses.append(runs_tbl.c.workflow_id == workflow_id)
    total = (await conn.execute(select(func.count()).select_from(runs_tbl).where(*clauses))).scalar_one()
    rows = await conn.execute(
        select(runs_tbl)
        .where(*clauses)
        .order_by(runs_tbl.c.claimed_at.desc().nulls_last(), runs_tbl.c.run_name.desc())
        .limit(limit)
        .offset(offset)
    )
    now = datetime.now(timezone.utc)
    return [{**r, "classification": classify_run(r, now)} for r in rows.mappings()], total


async def get_run(conn: AsyncConnection, run_name: str, now: datetime) -> dict[str, Any] | None:
    row = (await conn.execute(select(runs_tbl).where(runs_tbl.c.run_name == run_name))).mappings().first()
    if row is None:
        return None
    jobs = await conn.execute(
        select(jobs_tbl.c.status, func.count()).where(jobs_tbl.c.run_name == run_name).group_by(jobs_tbl.c.status)
    )
    tasks = await conn.execute(
        select(run_task_counts_tbl.c.status, run_task_counts_tbl.c.n).where(run_task_counts_tbl.c.run_name == run_name)
    )
    failed = await conn.execute(
        select(
            run_failed_tasks_tbl.c.process,
            run_failed_tasks_tbl.c.sample_id,
            run_failed_tasks_tbl.c.exit_code,
            run_failed_tasks_tbl.c.task_hash,
            run_failed_tasks_tbl.c.attempt,
            run_failed_tasks_tbl.c.error_action,
        )
        .where(run_failed_tasks_tbl.c.run_name == run_name)
        .order_by(run_failed_tasks_tbl.c.utc_time.desc(), run_failed_tasks_tbl.c.id.desc())
    )
    return {
        **row,
        "classification": classify_run(row, now),
        "job_status_counts": {status: n for status, n in jobs},
        "task_status_counts": {status: n for status, n in tasks},
        "failed_tasks": [dict(r) for r in failed.mappings()],
    }


async def list_cohorts(conn: AsyncConnection) -> list[dict[str, Any]]:
    members = (
        select(func.count())
        .where(collection_samples_tbl.c.collection_id == collections_tbl.c.collection_id)
        .scalar_subquery()
    )
    rows = await conn.execute(
        select(
            collections_tbl.c.collection_id,
            collections_tbl.c.source,
            collections_tbl.c.label,
            members.label("sample_count"),
            collections_tbl.c.created_at,
            collections_tbl.c.updated_at,
        ).order_by(collections_tbl.c.created_at.desc())
    )
    return [dict(r) for r in rows.mappings()]


async def leaderboard(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = []
    for r in (await conn.execute(_LEADERBOARD)).mappings():
        rows.append(
            {
                **r,
                "samples_remaining": r["sample_count"] - r["samples_completed"] - r["samples_failed"],
                "completion_pct": _pct(r["samples_completed"], r["sample_count"]),
            }
        )
    return sorted(rows, key=lambda r: (r["completion_pct"], -r["sample_count"]))


async def cohort_summary(
    conn: AsyncConnection,
    collection_id: str,
    *,
    workflow_id: str | None,
    workflow_version: str | None,
    all_workflows: bool,
    now: datetime,
) -> dict[str, Any] | None:
    cohort = (
        await conn.execute(select(collections_tbl).where(collections_tbl.c.collection_id == collection_id))
    ).mappings().first()
    if cohort is None:
        return None
    clauses: list[ColumnElement[bool]] = [collection_samples_tbl.c.collection_id == collection_id]
    if not all_workflows and not workflow_id:
        clauses.append(workflows_tbl.c.status == "active")
    if workflow_id:
        clauses.append(jobs_tbl.c.workflow_id == workflow_id)
    if workflow_version:
        clauses.append(jobs_tbl.c.workflow_version == workflow_version)
    scope = jobs_tbl.join(collection_samples_tbl, collection_samples_tbl.c.sample_id == jobs_tbl.c.sample_id).join(
        workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk
    )
    counted = await conn.execute(
        select(jobs_tbl.c.status, func.count()).select_from(scope).where(*clauses).group_by(jobs_tbl.c.status)
    )
    counts = {status: n for status, n in counted}
    completed = (
        await conn.execute(
            select(func.count(func.distinct(jobs_tbl.c.sample_id)))
            .select_from(scope)
            .where(*clauses, jobs_tbl.c.status == "completed")
        )
    ).scalar_one()
    sample_count = (
        await conn.execute(
            select(func.count()).select_from(collection_samples_tbl).where(collection_samples_tbl.c.collection_id == collection_id)
        )
    ).scalar_one()
    return {
        "collection_id": collection_id,
        "source": cohort["source"],
        "label": cohort["label"],
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "sample_count": sample_count,
        "samples_completed": completed,
        "total_jobs": sum(counts.values()),
        "job_status_counts": {s: counts.get(s, 0) for s in JOB_STATUSES},
        "completion_pct": _pct(completed, sample_count),
        # Needs per-task telemetry: the historical tier, not this one.
        "failure_by_process": [],
        "generated_at_utc": now,
    }


async def stats(conn: AsyncConnection) -> dict[str, Any]:
    async def by_status(stmt: Any) -> dict[str, int]:
        return {status: n for status, n in await conn.execute(stmt)}

    return {
        "samples": (await conn.execute(select(func.count()).select_from(samples_tbl))).scalar_one(),
        "workflows": (await conn.execute(select(func.count()).select_from(workflows_tbl))).scalar_one(),
        "jobs_by_status": await by_status(select(jobs_tbl.c.status, func.count()).group_by(jobs_tbl.c.status)),
        "jobs_by_status_active": await by_status(
            select(jobs_tbl.c.status, func.count())
            .select_from(jobs_tbl.join(workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk))
            .where(workflows_tbl.c.status == "active")
            .group_by(jobs_tbl.c.status)
        ),
        "runs_by_status": await by_status(select(runs_tbl.c.status, func.count()).group_by(runs_tbl.c.status)),
        "dead_letter_unresolved": (
            await conn.execute(select(func.count()).select_from(dead_letter_tbl).where(dead_letter_tbl.c.resolved_at.is_(None)))
        ).scalar_one(),
    }


async def dispatchability(conn: AsyncConnection, now: datetime) -> dict[str, Any]:
    """Pending work on active workflows that no live daemon is configured to claim."""
    pending = (
        await conn.execute(
            select(jobs_tbl.c.workflow_pk, workflows_tbl.c.workflow_id, func.count().label("pending"))
            .select_from(jobs_tbl.join(workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk))
            .where(jobs_tbl.c.status == "pending", workflows_tbl.c.status == "active")
            .group_by(jobs_tbl.c.workflow_pk, workflows_tbl.c.workflow_id)
        )
    ).mappings().all()
    daemons = await conn.execute(
        select(daemons_tbl.c.workflow_id).where(
            daemons_tbl.c.status != "stopped", daemons_tbl.c.last_seen_at > now - ACTIVE_WINDOW
        )
    )
    # None means the daemon claims every workflow.
    filters = [
        {w.strip() for w in wf.split(",") if w.strip()} if wf and wf.strip() else None
        for (wf,) in daemons
    ]
    stuck = [
        {**p, "reason": "no active daemon claims this workflow"}
        for p in pending
        if not any(f is None or p["workflow_id"] in f for f in filters)
    ]
    return {
        "checked_at": now,
        "active_daemons": len(filters),
        "stuck": stuck,
        "stuck_pending_total": sum(s["pending"] for s in stuck),
    }


async def active_run_names(conn: AsyncConnection) -> list[str]:
    return list((await conn.execute(select(runs_tbl.c.run_name).where(runs_tbl.c.status == "running"))).scalars())


async def list_dead_letter(
    conn: AsyncConnection,
    *,
    workflow_pk: int | None,
    collection: str | None,
    resolved: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    d = dead_letter_tbl
    source = d.join(jobs_tbl, jobs_tbl.c.id == d.c.job_id).outerjoin(samples_tbl, samples_tbl.c.sample_id == d.c.sample_id)
    clauses: list[ColumnElement[bool]] = []
    if workflow_pk is not None:
        clauses.append(jobs_tbl.c.workflow_pk == workflow_pk)
    if collection is not None:
        clauses.append(
            exists().where(
                and_(collection_samples_tbl.c.sample_id == d.c.sample_id, collection_samples_tbl.c.collection_id == collection)
            )
        )
    if resolved is not None:
        clauses.append(d.c.resolved_at.is_not(None) if resolved else d.c.resolved_at.is_(None))
    total = (await conn.execute(select(func.count()).select_from(source).where(*clauses))).scalar_one()
    rows = await conn.execute(
        select(
            d.c.id,
            d.c.job_id,
            jobs_tbl.c.workflow_pk,
            d.c.workflow_id,
            d.c.workflow_version,
            d.c.sample_id,
            samples_tbl.c.readset_id,
            collections_of(d.c.sample_id),
            d.c.run_name,
            d.c.reason,
            d.c.created_at,
            d.c.resolved_at,
            jobs_tbl.c.status.label("job_status"),
            jobs_tbl.c.retry_count,
        )
        .select_from(source)
        .where(*clauses)
        .order_by(d.c.created_at.desc(), d.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [dict(r) for r in rows.mappings()], total
