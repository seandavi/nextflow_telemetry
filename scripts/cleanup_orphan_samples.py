#!/usr/bin/env python3
"""Delete samples with empty/null ncbi_accession and their dependent rows.

Background: samples registered with no SRRs cause every dispatched run to fail
at fasterq_dump. Running once after the validation in routers/samples.py lands
is the one-time cleanup.

Cleanup target: samples where ncbi_accession IS NULL or trims to empty.

Cascades through (in FK-safe order):
  dead_letter        (FK: job_id → jobs.id)
  collection_samples (FK: sample_id → samples.sample_id)
  curated_sample_annotations  (no FK; soft join on sample_id)
  jobs               (FK: sample_id → samples.sample_id)
  samples

Left alone: workflow_runs, telemetry, task_logs — append-only event logs.

Usage:
    uv run python scripts/cleanup_orphan_samples.py             # dry run, default
    uv run python scripts/cleanup_orphan_samples.py --yes       # actually delete
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nextflow_telemetry.db import (  # noqa: E402
    collection_samples_tbl,
    curated_sample_annotations_tbl,
    dead_letter_tbl,
    jobs_tbl,
    samples_tbl,
)


def _orphan_predicate():
    """samples.ncbi_accession IS NULL OR trim(ncbi_accession) = ''."""
    return (samples_tbl.c.ncbi_accession.is_(None)) | (
        func.trim(samples_tbl.c.ncbi_accession) == ""
    )


async def _run(commit: bool) -> int:
    uri = os.environ.get("SQLALCHEMY_URI")
    if not uri:
        raise click.ClickException("SQLALCHEMY_URI not set")
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(uri)
    try:
        async with engine.begin() as conn:
            sample_ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(samples_tbl.c.sample_id).where(_orphan_predicate())
                    )
                ).all()
            ]
            if not sample_ids:
                click.echo("No orphan samples found.")
                return 0

            click.echo(f"Found {len(sample_ids)} orphan samples (empty ncbi_accession).")

            job_ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(jobs_tbl.c.id).where(jobs_tbl.c.sample_id.in_(sample_ids))
                    )
                ).all()
            ]
            click.echo(f"  - {len(job_ids)} jobs")

            dlq_count = (await conn.execute(
                select(func.count()).select_from(dead_letter_tbl).where(
                    dead_letter_tbl.c.job_id.in_(job_ids) if job_ids else False
                )
            )).scalar_one() if job_ids else 0
            click.echo(f"  - {dlq_count} dead_letter rows")

            cs_count = (await conn.execute(
                select(func.count()).select_from(collection_samples_tbl).where(
                    collection_samples_tbl.c.sample_id.in_(sample_ids)
                )
            )).scalar_one()
            click.echo(f"  - {cs_count} collection_samples rows")

            csa_count = (await conn.execute(
                select(func.count()).select_from(curated_sample_annotations_tbl).where(
                    curated_sample_annotations_tbl.c.sample_id.in_(sample_ids)
                )
            )).scalar_one()
            click.echo(f"  - {csa_count} curated_sample_annotations rows")

            if not commit:
                click.echo("\nDry run — no changes made. Re-run with --yes to delete.")
                return 0

            if job_ids:
                await conn.execute(
                    delete(dead_letter_tbl).where(dead_letter_tbl.c.job_id.in_(job_ids))
                )
            await conn.execute(
                delete(collection_samples_tbl).where(
                    collection_samples_tbl.c.sample_id.in_(sample_ids)
                )
            )
            await conn.execute(
                delete(curated_sample_annotations_tbl).where(
                    curated_sample_annotations_tbl.c.sample_id.in_(sample_ids)
                )
            )
            await conn.execute(
                delete(jobs_tbl).where(jobs_tbl.c.sample_id.in_(sample_ids))
            )
            await conn.execute(
                delete(samples_tbl).where(samples_tbl.c.sample_id.in_(sample_ids))
            )
            click.echo("\nDeleted.")
            return 0
    finally:
        await engine.dispose()


@click.command()
@click.option("--yes", is_flag=True, help="Actually delete (default is dry-run).")
def main(yes: bool) -> None:
    """Delete samples with empty/null ncbi_accession and their dependent rows."""
    sys.exit(asyncio.run(_run(commit=yes)))


if __name__ == "__main__":
    main()
