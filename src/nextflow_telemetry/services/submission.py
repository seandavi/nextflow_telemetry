"""Submission service — register a study/BioProject's samples, with provenance.

Every registration attempt mints a ``submission_id`` and writes an append-only
``submissions`` row (success or failure), so a no-op re-submission is still a
recorded, attributable event. The actual sample/collection writes go through one
shared core; input adapters (ENA accession now, curation TSV later) just produce
the sample list the core registers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import collection_samples_tbl, collections_tbl, samples_tbl, submissions_tbl
from ..utils import normalize_srrs, srrs_to_sample_id

ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
ENA_FIELDS = (
    "run_accession,secondary_sample_accession,sample_accession,"
    "study_accession,secondary_study_accession,sample_title"
)


class AccessionError(ValueError):
    """Raised for an accession the endpoint won't accept (bad type / no runs)."""


def classify_source(accession: str) -> str:
    """Return the ``collections.source`` for a study/BioProject accession.

    Rejects run/sample/experiment accessions — a collection must be a study or
    project, not a single run.
    """
    acc = accession.strip().upper()
    if acc.startswith("PRJ"):
        return "bioproject"
    if acc[:3] in ("SRP", "ERP", "DRP"):
        return "sra_study"
    raise AccessionError(
        f"'{accession}' is not a study/BioProject accession "
        "(expected PRJ… or SRP/ERP/DRP…)"
    )


async def _fetch_runs(accession: str) -> list[dict[str, Any]]:
    params = {"accession": accession, "result": "read_run", "fields": ENA_FIELDS, "format": "json"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(ENA_FILEREPORT, params=params)
    if resp.status_code == 204 or not resp.content:
        return []
    resp.raise_for_status()
    return resp.json()


def _group_samples(rows: list[dict[str, Any]], collection_id: str) -> list[dict[str, Any]]:
    """Group ENA run rows into samples keyed by SRA sample (secondary_sample_accession)."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        srr = (r.get("run_accession") or "").strip()
        srs = (r.get("secondary_sample_accession") or "").strip()
        if not srr or not srs:
            continue
        g = groups.setdefault(srs, {"srrs": [], "row": r})
        g["srrs"].append(srr)

    samples = []
    for srs, g in groups.items():
        r = g["row"]
        samples.append({
            "sample_id": srrs_to_sample_id(g["srrs"]),
            "ncbi_accession": normalize_srrs(g["srrs"]),
            "biosample_id": (r.get("sample_accession") or "").strip() or None,
            "metadata": {
                "sra_sample": srs,
                "biosample": (r.get("sample_accession") or "").strip() or None,
                "sra_study": (r.get("secondary_study_accession") or "").strip() or None,
                "bioproject": (r.get("study_accession") or "").strip() or None,
                "collection": collection_id,
            },
        })
    return samples


@dataclass
class SubmissionService:
    engine: AsyncEngine

    async def register_from_accession(
        self, accession: str, submitted_by: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Fetch runs for a study/BioProject, register new samples, record the submission.

        Existing samples keep their metadata (only a membership row is added), so a
        sample shared across studies isn't clobbered. Both success and failure write
        a submissions row. When ``dry_run`` is set, nothing is written (no samples,
        no collection, no submission row) — just the preview counts are returned with
        status "dry_run" and an empty submission_id. Returns the submission receipt.
        """
        accession = accession.strip()
        submission_id = "" if dry_run else uuid.uuid4().hex
        collection_id = accession.upper()
        try:
            source = classify_source(accession)
            rows = await _fetch_runs(accession)
            samples = _group_samples(rows, collection_id)
            if not samples:
                raise AccessionError(f"no runs found for '{accession}'")
            if dry_run:
                counts = await self._count_new(samples)
                status = "dry_run"
            else:
                counts = await self._register(
                    submission_id=submission_id,
                    method="ena_accession",
                    accession=accession,
                    collection_id=collection_id,
                    source=source,
                    type_="project",
                    submitted_by=submitted_by,
                    samples=samples,
                )
                status = "succeeded"
        except (AccessionError, httpx.HTTPError) as e:
            if not dry_run:
                await self._record_failure(
                    submission_id, "ena_accession", accession, collection_id, submitted_by, str(e)
                )
            raise
        return {
            "submission_id": submission_id,
            "collection_id": collection_id,
            "source": source,
            "type": "project",
            "status": status,
            **counts,
        }

    async def _count_new(self, samples: list[dict[str, Any]]) -> dict[str, int]:
        """Preview counts for a dry run — read-only, no writes."""
        by_id = {s["sample_id"]: s for s in samples}
        async with self.engine.connect() as conn:
            existing = {
                r[0] for r in (await conn.execute(
                    select(samples_tbl.c.sample_id).where(samples_tbl.c.sample_id.in_(list(by_id)))
                )).all()
            }
        return {
            "samples_found": len(by_id),
            "samples_added": len(by_id) - len(existing),
            "samples_existing": len(existing),
        }

    async def _register(
        self, *, submission_id: str, method: str, accession: str | None,
        collection_id: str, source: str | None, type_: str | None,
        submitted_by: str, samples: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Shared core: upsert new samples, the collection, membership, and the submission row."""
        by_id = {s["sample_id"]: s for s in samples}
        now = datetime.now(timezone.utc)

        async with self.engine.begin() as conn:
            existing = {
                r[0] for r in (await conn.execute(
                    select(samples_tbl.c.sample_id).where(samples_tbl.c.sample_id.in_(list(by_id)))
                )).all()
            }
            new_ids = [sid for sid in by_id if sid not in existing]

            if new_ids:
                await conn.execute(
                    samples_tbl.insert(),
                    [{
                        "sample_id": sid,
                        "ncbi_accession": by_id[sid]["ncbi_accession"],
                        "biosample_id": by_id[sid]["biosample_id"],
                        "metadata_": by_id[sid]["metadata"],
                        "created_at": now,
                        "updated_at": now,
                    } for sid in new_ids],
                )

            await conn.execute(
                pg_insert(collections_tbl)
                .values(
                    collection_id=collection_id, source=source, type=type_, label=collection_id,
                    metadata_={"origin": "submission", "accession": accession},
                    created_at=now, updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[collections_tbl.c.collection_id],
                    set_={"updated_at": now},
                )
            )
            await conn.execute(
                pg_insert(collection_samples_tbl)
                .values([{"collection_id": collection_id, "sample_id": sid} for sid in by_id])
                .on_conflict_do_nothing(constraint="uq_collection_sample")
            )

            counts = {
                "samples_found": len(by_id),
                "samples_added": len(new_ids),
                "samples_existing": len(existing),
            }
            await conn.execute(submissions_tbl.insert().values(
                submission_id=submission_id, method=method, accession=accession,
                collection_id=collection_id, source=source, type=type_,
                submitted_by=submitted_by, status="succeeded", error=None,
                metadata_=None, created_at=now, **counts,
            ))
        return counts

    async def _record_failure(
        self, submission_id: str, method: str, accession: str | None,
        collection_id: str | None, submitted_by: str, error: str,
    ) -> None:
        """Record a failed attempt in its own transaction (the main one rolled back)."""
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            await conn.execute(submissions_tbl.insert().values(
                submission_id=submission_id, method=method, accession=accession,
                collection_id=collection_id, source=None, type=None,
                submitted_by=submitted_by, status="failed", error=error,
                samples_found=None, samples_added=None, samples_existing=None,
                metadata_=None, created_at=now,
            ))

    async def get(self, submission_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(submissions_tbl).where(submissions_tbl.c.submission_id == submission_id)
            )).mappings().one_or_none()
            return dict(row) if row else None
