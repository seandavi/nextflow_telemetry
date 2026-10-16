"""Submission service — register a study/BioProject's samples, with provenance.

Every registration attempt mints a ``submission_id`` and writes an append-only
``submissions`` row (success or failure), so a no-op re-submission is still a
recorded, attributable event. The actual sample/collection writes go through one
shared core; input adapters (ENA accession now, curation TSV later) just produce
the sample list the core registers.
"""
from __future__ import annotations

import asyncio
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import samples_tbl, submissions_tbl
from ..log import logger
from ..utils import normalize_srrs, srrs_to_sample_id
from .collection import add_to_collection

ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
ENA_ATTEMPTS, ENA_RETRY_DELAY_S = 4, 10.0
ENA_FIELDS = (
    "run_accession,secondary_sample_accession,sample_accession,"
    "study_accession,secondary_study_accession,sample_title,"
    "library_strategy,library_selection,library_source,instrument_platform,"
    "library_layout,base_count,read_count"
)

# Cost constants for the add-study estimate (docs/plans/onboarding-and-publication.md §2c).
# ponytail: fixed constants; refit them from the MVP cohort's telemetry (#269).
CORE_H_PER_SAMPLE = 24  # cmgd_mpa4.2 2.3.x rehearsal
SCRATCH_GB_PER_SAMPLE = 50  # peak: fastq + kneaddata are held until the run ends
SU_INTERCEPT, SU_PER_M_READS = 6.59, 0.198  # docs/research/resource-tuning-2.3.0.md

# Library tags that mark a study as NOT shotgun metagenomics (16S/amplicon).
_AMPLICON_STRATEGY = {"AMPLICON"}
_AMPLICON_SELECTION = {"PCR"}


class AccessionError(ValueError):
    """Raised for an accession the endpoint won't accept (bad type / no runs)."""


def _library_composition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-run library metadata so an approver can eyeball study type
    (shotgun WGS vs 16S/amplicon). Advisory only — never blocks a submission."""
    def tally(field: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in rows:
            v = (r.get(field) or "").strip() or "unknown"
            c[v] = c.get(v, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    strat, sel = tally("library_strategy"), tally("library_selection")
    n_amplicon = sum(n for k, n in strat.items() if k.upper() in _AMPLICON_STRATEGY)
    n_pcr = sum(n for k, n in sel.items() if k.upper() in _AMPLICON_SELECTION)
    warnings = []
    if n_amplicon or n_pcr:
        warnings.append(
            f"{max(n_amplicon, n_pcr)} of {len(rows)} run(s) look like amplicon/16S "
            "(library_strategy=AMPLICON or library_selection=PCR), not shotgun metagenomics"
        )
    return {
        "library_strategy": strat,
        "library_selection": sel,
        "library_source": tally("library_source"),
        "instrument_platform": tally("instrument_platform"),
        "warnings": warnings,
    }


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
    async with httpx.AsyncClient(timeout=120) as client:
        # ENA's filereport returns intermittent 500s for some projects (#269):
        # retry 5xx and timeouts a few times before giving up.
        for attempt in range(ENA_ATTEMPTS):
            try:
                resp = await client.get(ENA_FILEREPORT, params=params)
                if resp.status_code < 500:
                    break
            except httpx.TransportError:
                if attempt == ENA_ATTEMPTS - 1:
                    raise
            await asyncio.sleep(ENA_RETRY_DELAY_S * (attempt + 1))
    resp.raise_for_status()  # before the empty check: a bodiless 500 is an error, not "no runs"
    if resp.status_code == 204 or not resp.content:
        return []
    return resp.json()


def _group_samples(rows: list[dict[str, Any]], collection_id: str) -> list[dict[str, Any]]:
    """Group ENA run rows into samples keyed by SRA sample (secondary_sample_accession)."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        srr = (r.get("run_accession") or "").strip()
        srs = (r.get("secondary_sample_accession") or "").strip()
        if not srr or not srs:
            continue
        g = groups.setdefault(srs, {"srrs": [], "row": r, "bases": 0, "reads": 0})
        g["srrs"].append(srr)
        # Raw reads count both mates of a paired run; ENA read_count is spots.
        mates = 2 if (r.get("library_layout") or "").upper() == "PAIRED" else 1
        g["bases"] += int(r.get("base_count") or 0)
        g["reads"] += int(r.get("read_count") or 0) * mates

    samples = []
    for srs, g in groups.items():
        r = g["row"]
        samples.append({
            "sample_id": srrs_to_sample_id(g["srrs"]),
            "ncbi_accession": normalize_srrs(g["srrs"]),
            "biosample_id": (r.get("sample_accession") or "").strip() or None,
            "bases": g["bases"],
            "reads": g["reads"],
            "metadata": {
                "sra_sample": srs,
                "biosample": (r.get("sample_accession") or "").strip() or None,
                "sra_study": (r.get("secondary_study_accession") or "").strip() or None,
                "bioproject": (r.get("study_accession") or "").strip() or None,
                "collection": collection_id,
            },
        })
    return samples


def estimate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost of running ``samples`` (from ``_group_samples``) through cmgd_mpa4.2."""
    gbp = [s["bases"] / 1e9 for s in samples]
    su = sum(SU_INTERCEPT + SU_PER_M_READS * s["reads"] / 1e6 for s in samples)
    return {
        "samples": len(samples),
        "gbp": round(sum(gbp), 1),
        "median_gbp": round(statistics.median(gbp), 2) if gbp else 0.0,
        "core_h": CORE_H_PER_SAMPLE * len(samples),
        "anvil_su": round(su),
        "peak_scratch_gb_per_sample": SCRATCH_GB_PER_SAMPLE,
    }


@dataclass
class SubmissionService:
    engine: AsyncEngine
    # v2 control plane (the Worker). With both set, a registration writes through
    # to v2 POST /samples after the v1 transaction commits (#195). Empty: v1 only.
    v2_url: str = ""
    v2_token: str = ""

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
            composition = _library_composition(rows)
            warnings = composition.pop("warnings")
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
        # After the v1 commit: a v2 failure is reported in the receipt, never rolls v1 back.
        v2 = {} if dry_run else await self._write_through(samples, collection_id)
        return {
            "submission_id": submission_id,
            "collection_id": collection_id,
            "source": source,
            "type": "project",
            "status": status,
            "library_composition": composition,
            "warnings": warnings,
            "estimate": estimate(samples),
            **counts,
            **v2,
        }

    async def _write_through(self, samples: list[dict[str, Any]], collection_id: str) -> dict[str, Any]:
        """Register each sample and its membership in v2. Re-submitting retries a
        failure: both sides are idempotent. A sample v2 already has keeps its
        metadata, because v2's upsert would otherwise replace it."""
        if not (self.v2_url and self.v2_token):
            return {}
        failed: list[str] = []
        async with httpx.AsyncClient(
            base_url=self.v2_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.v2_token}"},
            timeout=30,
        ) as client:
            for s in samples:
                try:
                    cur = await client.get(f"samples/{s['sample_id']}")
                    meta = cur.json().get("metadata") if cur.status_code == 200 else None
                    resp = await client.post("samples", json={
                        "sample_id": s["sample_id"],
                        "ncbi_accession": s["ncbi_accession"],
                        "biosample_id": s["biosample_id"],
                        "metadata": meta or s["metadata"],
                        "collection": collection_id,
                    })
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    logger.warning("submission.v2_write_failed",
                                   extra={"sample_id": s["sample_id"], "error": str(e)})
                    failed.append(s["sample_id"])
        return {"v2_written": len(samples) - len(failed), "v2_failed": failed}

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
        collection_id: str, source: str, type_: str | None,
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

            await add_to_collection(
                conn, collection_id, source=source, type_=type_, label=collection_id,
                sample_ids=list(by_id),
                metadata={"origin": "submission", "accession": accession},
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
