"""Integration tests for the submission core (real DB, mocked ENA).

Only the external ENA fetch is stubbed; sample/collection/membership/submission
writes hit real Postgres so the content-addressed grouping and idempotency are
exercised end-to-end.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.db import (
    collection_samples_tbl,
    collections_tbl,
    samples_tbl,
    submissions_tbl,
)
from nextflow_telemetry.services import submission as sub_mod
from nextflow_telemetry.services.submission import AccessionError, SubmissionService

# Two runs share one SRA sample (SRS1), a third is its own (SRS2).
_ENA_ROWS = [
    {"run_accession": "SRR2", "secondary_sample_accession": "SRS1", "sample_accession": "SAMN1",
     "study_accession": "PRJNA1", "secondary_study_accession": "SRP1"},
    {"run_accession": "SRR1", "secondary_sample_accession": "SRS1", "sample_accession": "SAMN1",
     "study_accession": "PRJNA1", "secondary_study_accession": "SRP1"},
    {"run_accession": "SRR3", "secondary_sample_accession": "SRS2", "sample_accession": "SAMN2",
     "study_accession": "PRJNA1", "secondary_study_accession": "SRP1"},
]


def _run(coro):
    return asyncio.run(coro)


async def _scalar(db_url, stmt):
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one()
    finally:
        await engine.dispose()


def test_submission_registers_and_is_idempotent(db_url, monkeypatch):
    async def fake_fetch(accession):
        return _ENA_ROWS

    monkeypatch.setattr(sub_mod, "_fetch_runs", fake_fetch)

    async def scenario():
        engine = create_async_engine(db_url)
        try:
            svc = SubmissionService(engine=engine)

            first = await svc.register_from_accession("PRJNA1", submitted_by="a@b.c")
            assert first["status"] == "succeeded"
            assert first["source"] == "bioproject" and first["type"] == "project"
            assert first["samples_found"] == 2
            assert first["samples_added"] == 2
            assert first["samples_existing"] == 0
            assert first["submission_id"]

            # Second submission: same accession → nothing new, but still recorded.
            second = await svc.register_from_accession("PRJNA1", submitted_by="a@b.c")
            assert second["samples_found"] == 2
            assert second["samples_added"] == 0
            assert second["samples_existing"] == 2
            assert second["submission_id"] != first["submission_id"]
        finally:
            await engine.dispose()

    _run(scenario())

    # DB state: 2 samples, 1 collection, 2 memberships, 2 submission rows.
    assert _run(_scalar(db_url, select(func.count()).select_from(samples_tbl))) == 2
    assert _run(_scalar(db_url, select(func.count()).select_from(collections_tbl).where(
        collections_tbl.c.collection_id == "PRJNA1"))) == 1
    assert _run(_scalar(db_url, select(collections_tbl.c.type).where(
        collections_tbl.c.collection_id == "PRJNA1"))) == "project"
    assert _run(_scalar(db_url, select(func.count()).select_from(collection_samples_tbl).where(
        collection_samples_tbl.c.collection_id == "PRJNA1"))) == 2
    assert _run(_scalar(db_url, select(func.count()).select_from(submissions_tbl))) == 2
    # The no-op second submission is on record with 0 added.
    assert _run(_scalar(db_url, select(func.count()).select_from(submissions_tbl).where(
        submissions_tbl.c.samples_added == 0))) == 1


def test_library_composition_flags_amplicon():
    wgs = [{"library_strategy": "WGS", "library_selection": "RANDOM",
            "library_source": "METAGENOMIC", "instrument_platform": "ILLUMINA"}] * 3
    comp = sub_mod._library_composition(wgs)
    assert comp["library_strategy"] == {"WGS": 3}
    assert comp["library_selection"] == {"RANDOM": 3}
    assert comp["warnings"] == []

    mixed = wgs + [{"library_strategy": "AMPLICON", "library_selection": "PCR"}]
    comp2 = sub_mod._library_composition(mixed)
    assert comp2["library_strategy"] == {"WGS": 3, "AMPLICON": 1}
    assert comp2["warnings"] and "amplicon" in comp2["warnings"][0].lower()


def test_composition_in_receipt(db_url, monkeypatch):
    rows = [dict(r, library_strategy="WGS", library_selection="RANDOM",
                 library_source="METAGENOMIC", instrument_platform="ILLUMINA")
            for r in _ENA_ROWS]

    async def fake_fetch(accession):
        return rows

    monkeypatch.setattr(sub_mod, "_fetch_runs", fake_fetch)

    async def scenario():
        engine = create_async_engine(db_url)
        try:
            svc = SubmissionService(engine=engine)
            r = await svc.register_from_accession("PRJNA1", submitted_by="a@b.c", dry_run=True)
            assert r["library_composition"]["library_strategy"] == {"WGS": 3}
            assert r["library_composition"]["library_selection"] == {"RANDOM": 3}
            assert r["warnings"] == []
        finally:
            await engine.dispose()

    _run(scenario())


def test_dry_run_previews_without_writing(db_url, monkeypatch):
    async def fake_fetch(accession):
        return _ENA_ROWS

    monkeypatch.setattr(sub_mod, "_fetch_runs", fake_fetch)

    async def scenario():
        engine = create_async_engine(db_url)
        try:
            svc = SubmissionService(engine=engine)
            r = await svc.register_from_accession("PRJNA1", submitted_by="a@b.c", dry_run=True)
            assert r["status"] == "dry_run"
            assert r["submission_id"] == ""
            assert r["samples_found"] == 2 and r["samples_added"] == 2 and r["samples_existing"] == 0
        finally:
            await engine.dispose()

    _run(scenario())

    # Nothing was written — no samples, no collection, no submission row.
    assert _run(_scalar(db_url, select(func.count()).select_from(samples_tbl))) == 0
    assert _run(_scalar(db_url, select(func.count()).select_from(submissions_tbl))) == 0


def test_bad_accession_records_failed_submission(db_url):
    async def scenario():
        engine = create_async_engine(db_url)
        try:
            svc = SubmissionService(engine=engine)
            with pytest.raises(AccessionError):
                await svc.register_from_accession("SRR999", submitted_by="a@b.c")
        finally:
            await engine.dispose()

    _run(scenario())

    # A failed attempt is still on record, with status=failed and no counts.
    assert _run(_scalar(db_url, select(submissions_tbl.c.status).where(
        submissions_tbl.c.accession == "SRR999"))) == "failed"
    assert _run(_scalar(db_url, select(submissions_tbl.c.samples_added).where(
        submissions_tbl.c.accession == "SRR999"))) is None
