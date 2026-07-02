#!/usr/bin/env python3
"""Seed a study collection from the study's OWN sample list (m2m-correct).

Unlike ``seed_collection_from_cohort.py`` — which derives membership from the
scalar ``samples.metadata.cohort`` and therefore *undercounts* any sample shared
across studies (a scalar field can only name one cohort) — this builds each
study's collection from the study's authoritative sample list (its curation TSV).
A sample whose content-addressed ``sample_id`` appears in several studies gets a
membership row in *each*, which is the genuine many-to-many relationship
(docs/study-sample-version-identity.md, Decision 2).

Only samples that already exist in the ``samples`` table are added (the FK
requires it), so run this after the samples have been registered.

Idempotent: upserts the collection and skips existing memberships.

Usage (from onclappc02; pg_main resolves only inside Docker → use 127.0.0.1):

    set -a; source deploy/onclappc02/.env; set +a
    SQLALCHEMY_URI="${SQLALCHEMY_URI/@pg_main:/@127.0.0.1:}" \
      uv run --with httpx python scripts/seed_collection_from_study_tsv.py \
        --study WirbelJ_2018 --study GuptaA_2019 --commit
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
import os
import sys
import urllib.request
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
from nextflow_telemetry.utils import parse_srrs, srrs_to_sample_id  # noqa: E402

DEFAULT_BASE = (
    "https://raw.githubusercontent.com/waldronlab/"
    "curatedMetagenomicDataCuration/master/inst/curated"
)


def _study_sample_ids(base: str, study: str) -> list[str]:
    """Content-addressed sample_ids for every TSV row with an ncbi_accession."""
    url = f"{base}/{study}/{study}_sample.tsv"
    raw = urllib.request.urlopen(url, timeout=30).read().decode()
    ids: list[str] = []
    for row in csv.DictReader(io.StringIO(raw), delimiter="\t"):
        acc = (row.get("ncbi_accession") or "").strip()
        if not acc:
            continue
        srrs = parse_srrs(acc)
        if srrs:
            ids.append(srrs_to_sample_id(srrs))
    # de-dupe, preserve order
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


async def _run(studies: tuple[str, ...], base: str, source: str, commit: bool) -> int:
    uri = os.environ.get("SQLALCHEMY_URI")
    if not uri:
        raise click.ClickException("SQLALCHEMY_URI not set")
    if uri.startswith("postgresql://"):
        uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)

    now = datetime.datetime.now(datetime.timezone.utc)
    engine = create_async_engine(uri)
    try:
        for study in studies:
            tsv_ids = _study_sample_ids(base, study)
            async with engine.begin() as conn:
                # Restrict to samples that actually exist (FK on membership).
                existing = [
                    r[0]
                    for r in (
                        await conn.execute(
                            select(samples_tbl.c.sample_id).where(
                                samples_tbl.c.sample_id.in_(tsv_ids)
                            )
                        )
                    ).all()
                ]
                if not existing:
                    click.echo(f"{study}: 0 of {len(tsv_ids)} TSV samples exist yet — skipped.")
                    continue

                if not commit:
                    click.echo(f"{study}: would add {len(existing)} memberships "
                               f"({len(tsv_ids)} in TSV). DRY RUN.")
                    continue

                await conn.execute(
                    pg_insert(collections_tbl)
                    .values(
                        collection_id=study, source=source, label=study,
                        metadata_={"origin": "seed_collection_from_study_tsv"},
                        created_at=now, updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[collections_tbl.c.collection_id],
                        set_={"updated_at": now},
                    )
                )
                result = await conn.execute(
                    pg_insert(collection_samples_tbl)
                    .values([{"collection_id": study, "sample_id": sid} for sid in existing])
                    .on_conflict_do_nothing(constraint="uq_collection_sample")
                )
                click.echo(f"{study}: +{result.rowcount} new memberships "
                           f"({len(existing)} existing samples of {len(tsv_ids)} in TSV).")
        return 0
    finally:
        await engine.dispose()


@click.command()
@click.option("--study", "studies", multiple=True, required=True, help="Study name (repeatable).")
@click.option("--base", default=DEFAULT_BASE, show_default=False, help="Raw base URL for curated TSVs.")
@click.option("--source", default="sra_study", show_default=True, help="collections.source value.")
@click.option("--commit", is_flag=True, default=False, help="Actually write (default: dry run).")
def main(studies: tuple[str, ...], base: str, source: str, commit: bool) -> None:
    raise SystemExit(asyncio.run(_run(studies, base, source, commit)))


if __name__ == "__main__":
    main()
