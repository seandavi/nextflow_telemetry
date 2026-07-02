#!/usr/bin/env python3
"""Create a collection (cohort) and map every sample tagged with that cohort.

Samples seeded from a curation TSV carry the cohort name as a plain JSONB field
(`metadata.cohort`). This is not the same as a first-class `collections` row, so
the cohort dashboards have nothing to render. This one-off backfills a
`collections` row plus `collection_samples` membership for every sample whose
`metadata.cohort` matches.

Idempotent: re-running upserts the collection and skips existing memberships.

Usage (from the onclappc02 host; pg_main only resolves inside Docker, so use the
127.0.0.1 override):

    set -a; source deploy/onclappc02/.env; set +a
    SQLALCHEMY_URI="${SQLALCHEMY_URI/@pg_main:/@127.0.0.1:}" \
      uv run python scripts/seed_collection_from_cohort.py --cohort ArtachoA_2021

    # add --commit to actually write (default is a dry run)
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
from pathlib import Path

import click
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nextflow_telemetry.db import (  # noqa: E402
    collection_samples_tbl,
    collections_tbl,
    samples_tbl,
)


async def _run(cohort: str, source: str, label: str | None, commit: bool) -> int:
    uri = os.environ.get("SQLALCHEMY_URI")
    if not uri:
        raise click.ClickException("SQLALCHEMY_URI not set")
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)

    now = datetime.datetime.now(datetime.timezone.utc)
    engine = create_async_engine(uri)
    try:
        async with engine.begin() as conn:
            # Samples whose metadata.cohort matches the requested cohort.
            sample_ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(samples_tbl.c.sample_id).where(
                            samples_tbl.c.metadata_["cohort"].astext == cohort
                        )
                    )
                ).all()
            ]
            if not sample_ids:
                click.echo(f"No samples found with metadata.cohort == {cohort!r}. Nothing to do.")
                return 0

            click.echo(f"Cohort {cohort!r}: {len(sample_ids)} matching samples.")

            if not commit:
                click.echo("DRY RUN — pass --commit to write the collection + memberships.")
                return 0

            # 1. Upsert the collection row.
            await conn.execute(
                pg_insert(collections_tbl)
                .values(
                    collection_id=cohort,
                    source=source,
                    label=label or cohort,
                    metadata_={"origin": "seed_collection_from_cohort", "cohort_field": cohort},
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[collections_tbl.c.collection_id],
                    set_={"label": label or cohort, "updated_at": now},
                )
            )

            # 2. Insert memberships, skipping any that already exist.
            result = await conn.execute(
                pg_insert(collection_samples_tbl)
                .values([{"collection_id": cohort, "sample_id": sid} for sid in sample_ids])
                .on_conflict_do_nothing(constraint="uq_collection_sample")
            )
            click.echo(f"Collection {cohort!r} upserted; {result.rowcount} new memberships added "
                       f"({len(sample_ids)} total in cohort).")
        return 0
    finally:
        await engine.dispose()


@click.command()
@click.option("--cohort", required=True, help="Value of samples.metadata.cohort to collect.")
@click.option("--source", default="manual", show_default=True, help="collections.source value.")
@click.option("--label", default=None, help="Human label (defaults to the cohort name).")
@click.option("--commit", is_flag=True, default=False, help="Actually write (default: dry run).")
def main(cohort: str, source: str, label: str | None, commit: bool) -> None:
    raise SystemExit(asyncio.run(_run(cohort, source, label, commit)))


if __name__ == "__main__":
    main()
