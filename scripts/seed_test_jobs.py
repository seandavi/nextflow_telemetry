#!/usr/bin/env python3
"""Seed a small, controlled batch of pending jobs for one active workflow.

Used to drive an end-to-end telemetry test: insert (or requeue) a handful of
`pending` jobs so an idle daemon claims them and runs them through the full
lifecycle. Targets cmgd_nextflow 2.0.6 (workflow pk 12) by default.

Each sample can hold only one job per (workflow_id, version) — uq_job_composite —
so seeding is an UPSERT: insert pending if absent, else reset the existing job
(any status, including completed/failed) back to pending with a clean slate.

Dry run by default — prints the chosen samples and their current job status.
Pass --commit to actually write.

Usage (from the onclappc02 host; pg_main only resolves inside Docker, so use the
127.0.0.1 override):

    set -a; source deploy/onclappc02/.env; set +a
    SQLALCHEMY_URI="${SQLALCHEMY_URI/@pg_main:/@127.0.0.1:}" \
      uv run python scripts/seed_test_jobs.py --limit 3            # dry run
    SQLALCHEMY_URI="${SQLALCHEMY_URI/@pg_main:/@127.0.0.1:}" \
      uv run python scripts/seed_test_jobs.py --limit 3 --commit   # write
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
from pathlib import Path

import click
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nextflow_telemetry.db import jobs_tbl, workflows_tbl  # noqa: E402


async def _run(workflow_pk: int, limit: int, samples: tuple[str, ...], commit: bool) -> int:
    uri = os.environ.get("SQLALCHEMY_URI")
    if not uri:
        raise click.ClickException("SQLALCHEMY_URI not set")
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)

    now = datetime.datetime.now(datetime.timezone.utc)
    engine = create_async_engine(uri)
    try:
        async with engine.begin() as conn:
            wf = (await conn.execute(
                select(
                    workflows_tbl.c.workflow_id,
                    workflows_tbl.c.version,
                    workflows_tbl.c.status,
                ).where(workflows_tbl.c.id == workflow_pk)
            )).first()
            if not wf:
                raise click.ClickException(f"No workflow with pk={workflow_pk}")
            workflow_id, version, wf_status = wf
            click.echo(f"Target workflow: pk={workflow_pk} {workflow_id} {version} (status={wf_status})")
            if wf_status != "active":
                raise click.ClickException(
                    f"Workflow pk={workflow_pk} is {wf_status!r}, not active — refusing to seed."
                )

            # Choose samples. Explicit --sample wins; otherwise pick samples whose
            # existing job for this workflow (if any) is in a terminal/absent state,
            # so we never disturb work that is currently in flight.
            if samples:
                chosen = list(samples)
            else:
                rows = (await conn.execute(
                    text(
                        """
                        SELECT s.sample_id, j.status AS job_status
                        FROM samples s
                        LEFT JOIN jobs j
                          ON j.sample_id = s.sample_id
                         AND j.workflow_id = :wid
                         AND j.workflow_version = :ver
                        WHERE j.status IS NULL
                           OR j.status IN ('completed', 'failed')
                        ORDER BY (j.status IS NULL) DESC, s.sample_id
                        LIMIT :lim
                        """
                    ),
                    {"wid": workflow_id, "ver": version, "lim": limit},
                )).all()
                chosen = [r[0] for r in rows]

            if not chosen:
                click.echo("No eligible samples found (all have in-flight jobs?). Nothing to do.")
                return 0

            # Report current state of the chosen samples' jobs.
            existing = {
                r[0]: r[1]
                for r in (await conn.execute(
                    text(
                        """
                        SELECT sample_id, status FROM jobs
                        WHERE workflow_id = :wid AND workflow_version = :ver
                          AND sample_id = ANY(:ids)
                        """
                    ),
                    {"wid": workflow_id, "ver": version, "ids": chosen},
                )).all()
            }
            click.echo(f"\n{len(chosen)} sample(s) selected:")
            for sid in chosen:
                cur = existing.get(sid, "(no job — will insert)")
                click.echo(f"  {sid:<40} current: {cur} -> pending")

            if not commit:
                click.echo("\nDRY RUN — pass --commit to write.")
                return 0

            stmt = (
                pg_insert(jobs_tbl)
                .values([
                    {
                        "sample_id": sid,
                        "workflow_pk": workflow_pk,
                        "workflow_id": workflow_id,
                        "workflow_version": version,
                        "status": "pending",
                        "retry_count": 0,
                        "created_at": now,
                    }
                    for sid in chosen
                ])
                .on_conflict_do_update(
                    constraint="uq_job_composite",
                    set_={
                        "status": "pending",
                        "run_name": None,
                        "retry_count": 0,
                        "completed_at": None,
                        "failed_at": None,
                        "failure_reason": None,
                    },
                )
            )
            result = await conn.execute(stmt)
            click.echo(f"\nSeeded {result.rowcount} job(s) to pending for {workflow_id} {version}.")
        return 0
    finally:
        await engine.dispose()


@click.command()
@click.option("--workflow-pk", default=12, show_default=True, help="workflows.id of the target (active) workflow.")
@click.option("--limit", default=3, show_default=True, help="How many samples to seed when --sample not given.")
@click.option("--sample", "samples", multiple=True, help="Explicit sample_id(s) to seed (repeatable).")
@click.option("--commit", is_flag=True, default=False, help="Actually write (default: dry run).")
def main(workflow_pk: int, limit: int, samples: tuple[str, ...], commit: bool) -> None:
    raise SystemExit(asyncio.run(_run(workflow_pk, limit, samples, commit)))


if __name__ == "__main__":
    main()
