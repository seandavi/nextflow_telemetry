"""End-to-end ingest into a local DuckLake (DuckDB-file catalog, local data
path) with the watermark in the test Postgres. Published trees are built from
the real excerpts in tests/fixtures/etl/; object storage is the local
filesystem (source._exists/_cat patched), so no rclone, R2 or GCS.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from test_etl_parsers import FIX, manifest_230

from nextflow_telemetry.etl import engine, lake, source, v2, watermark
from nextflow_telemetry.etl.specs import BRANCHES

RS = "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"   # ADR-0007 golden vector
MD5 = "05f281407e15a03e65eba0dd74f30fae"

H39 = v2.Registration(2, "cmgd_humann3.9", "2.3.0", {"humann_bundle": "humann3.9", "skip_humann": False})
H4 = v2.Registration(3, "cmgd_humann4a1", "2.3.0", {"humann_bundle": "humann4.0.0a1", "skip_humann": False})
MPA = v2.Registration(4, "cmgd_mpa4.2", "2.3.0", {"skip_humann": True})
LEGACY = v2.Registration(1, "cmgd_nextflow", "2.2.1", {})


def _put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def publish(base: Path, reg: v2.Registration, key: str, manifest: bytes,
            humann: dict[str, str] | None = None, complete: bool = True) -> None:
    """Lay out one sample the way the pipeline publishes it."""
    d = base / reg.workflow_id / reg.version / key
    for branch in BRANCHES:
        for src, dst in (("marker_rel_ab_w_read_stats.tsv", "metaphlan_markers/marker_rel_ab_w_read_stats.tsv.gz"),
                         ("marker_abundance.tsv", "metaphlan_markers/marker_abundance.tsv.gz"),
                         ("bracken.species.txt", "kraken/bracken.species.txt.gz"),
                         ("card_kma.res", "resistome/card_kma.res.gz")):
            _put(d / branch / dst, gzip.compress((FIX / "2.2.1" / src).read_bytes()))
    _put(d / "manifest.json", manifest)
    if humann:
        bundle = humann.pop("bundle")
        h = d / "humann" / bundle
        _put(h / "metaphlan" / "metaphlan_rel_ab_w_read_stats.tsv",
             (FIX / bundle / "metaphlan_rel_ab_w_read_stats.tsv").read_bytes())
        for name in humann.values():
            _put(h / f"{name}.gz", gzip.compress((FIX / bundle / name).read_bytes()))
    if complete:
        _put(d / "MARK_COMPLETE", f"{key} 2026-10-07T00:00:00Z".encode())


def job(key: str, sample_id: str | None, collections=("ZellerG_2014",)) -> v2.CompletedJob:
    return v2.job_from_item({"job_id": 1, "sample_key": key, "completed_at": "2026-10-07T00:00:00Z",
                             "sample_id": sample_id, "collections": list(collections)})


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    r2, gcs = tmp_path / "r2", tmp_path / "gcs"
    monkeypatch.setattr(source, "SOURCE_BASE", str(r2))
    monkeypatch.setattr(source, "LEGACY_BASES", {("cmgd_nextflow", "2.2.1"): (str(gcs),)})
    monkeypatch.setattr(source, "_exists", os.path.exists)
    monkeypatch.setattr(source, "_cat", lambda p: Path(p).read_bytes() if Path(p).is_file() else None)
    monkeypatch.setattr(lake, "CATALOG_PG_DB", None)
    monkeypatch.setattr(lake, "CATALOG", str(tmp_path / "lake" / "cmgd.ducklake"))
    monkeypatch.setattr(lake, "DATA_PATH", str(tmp_path / "lake" / "data"))

    publish(r2, H39, RS, manifest_230("humann3.9", "mpa4.1.1_vJun23"),
            {"bundle": "humann3.9", "gf": "out_genefamilies.tsv", "pa": "out_pathabundance.tsv",
             "pc": "out_pathcoverage.tsv"})
    publish(r2, H4, RS, manifest_230("humann4.0.0a1", "mpa4.1.1_vOct22"),
            {"bundle": "humann4.0.0a1", "gf": "out_2_genefamilies.tsv", "pa": "out_4_pathabundance.tsv"})
    publish(r2, MPA, RS, manifest_230(None))
    publish(r2, MPA, "RS.unpublished", manifest_230(None), complete=False)
    publish(gcs, LEGACY, MD5, (FIX / "2.2.1" / "manifest.json").read_bytes())  # GCS only
    return r2, gcs


@pytest.fixture()
async def pg(db_url):
    conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
    await watermark.ensure_table(conn)
    yield conn
    await conn.close()


def counts(con, table: str) -> dict[str, int]:
    return dict(con.execute(f"SELECT workflow_id, count(*) FROM lake.{table} GROUP BY 1").fetchall())


async def test_ingest_is_idempotent_and_isolated_per_registration(storage, pg):
    con = lake.connect()
    lake.ensure_schema(con)
    lake.ensure_schema(con)  # second call is a no-op (tables exist, sort already set)

    s39 = await engine.process(pg, con, H39, [job(RS, None)])
    assert s39["ingested"] == 1 and s39["skipped_unpublished"] == 0
    assert "humann_genefamilies" not in s39["tables"]  # deferred by default
    assert s39["tables"]["humann_pathabundance"] == 5 and s39["tables"]["humann_pathcoverage"] == 5
    await engine.process(pg, con, H4, [job(RS, None)])
    smpa = await engine.process(pg, con, MPA, [job(RS, None), job("RS.unpublished", None)])
    assert smpa == {"ingested": 1, "skipped_unpublished": 1, "tables": smpa["tables"]}
    await engine.process(pg, con, LEGACY, [job(MD5, MD5)])

    tables = ["taxonomic_profile_metaphlan", "taxonomic_profile_bracken", "resistome", "qc_metrics",
              "humann_pathabundance", "humann_pathcoverage"]
    before = {t: counts(con, t) for t in tables}
    main_mpa = 2 * 6  # two branches x six profile rows
    assert before["taxonomic_profile_metaphlan"] == {
        "cmgd_humann3.9": main_mpa + 3, "cmgd_humann4a1": main_mpa + 3,
        "cmgd_mpa4.2": main_mpa, "cmgd_nextflow": main_mpa}
    assert before["humann_pathcoverage"] == {"cmgd_humann3.9": 5}  # HUMAnN 4 has none

    # Re-ingest one registration (a crash before the watermark write): same rows,
    # no duplicates, and the other registrations sharing the sample key untouched.
    await engine.process(pg, con, H39, [job(RS, None)])
    assert {t: counts(con, t) for t in tables} == before

    # Watermark: everything published is recorded, nothing is pending.
    assert await watermark.ingested_keys(pg, "cmgd_mpa4.2", "2.3.0") == {RS}
    assert engine.pending([job(RS, None), job("RS.unpublished", None)],
                          await watermark.ingested_keys(pg, "cmgd_mpa4.2", "2.3.0")) == [job("RS.unpublished", None)]

    profiles = con.execute(
        "SELECT DISTINCT workflow_id, data_type, metaphlan_profile, humann_bundle "
        "FROM lake.taxonomic_profile_metaphlan ORDER BY ALL").fetchall()
    assert ("cmgd_humann3.9", "full_data", "mpa4.1.1_vJun23", "humann3.9") in profiles
    assert ("cmgd_humann4a1", "full_data", "mpa4.1.1_vOct22", "humann4.0.0a1") in profiles
    assert ("cmgd_humann3.9", "rarefied_data", "mpa4.2.2_vJan25", None) in profiles
    assert ("cmgd_nextflow", "full_data", "mpa4.2.2_vJan25", None) in profiles

    qc = con.execute(
        "SELECT workflow_id, sample_key, sample_id, readset_id, study_name, metaphlan_index, "
        "metaphlan_profile, humann_bundle FROM lake.qc_metrics ORDER BY workflow_id").fetchall()
    assert qc == [
        ("cmgd_humann3.9", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", "humann3.9"),
        ("cmgd_humann4a1", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", "humann4.0.0a1"),
        ("cmgd_mpa4.2", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", None),
        ("cmgd_nextflow", MD5, MD5, None, "ZellerG_2014", "mpa_vJan25_CHOCOPhlAnSGB_202503", None, None),
    ]

    # Deferred tables on request; gene families stay scoped to their registration.
    await engine.process(pg, con, H39, [job(RS, None)], include_deferred=True)
    assert counts(con, "humann_genefamilies") == {"cmgd_humann3.9": 5}
    assert counts(con, "marker_abundance") == {"cmgd_humann3.9": 2 * 2}
    con.close()


def test_locate_prefers_r2_then_legacy_gcs(storage):
    r2, gcs = storage
    assert source.locate("cmgd_nextflow", "2.2.1", MD5) == f"{gcs}/cmgd_nextflow/2.2.1/{MD5}"
    publish(r2, LEGACY, MD5, b"{}")
    assert source.locate("cmgd_nextflow", "2.2.1", MD5) == f"{r2}/cmgd_nextflow/2.2.1/{MD5}"
    # No legacy base for 2.3.0 registrations, and no MARK_COMPLETE means not published.
    assert source.locate("cmgd_mpa4.2", "2.3.0", "RS.unpublished") is None


def test_completed_jobs_pages_by_job_id(monkeypatch):
    monkeypatch.setattr(v2, "PAGE", 2)
    items = [{"job_id": i, "sample_key": k, "completed_at": None, "sample_id": s, "collections": c}
             for i, k, s, c in ((5, MD5, MD5, ["B", "A"]), (7, RS, None, []), (9, "x" * 32, None, []))]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workflows/4/jobs"
        assert request.url.params["status"] == "completed"
        after = int(request.url.params["after"])
        seen.append(after)
        return httpx.Response(200, json={"items": [i for i in items if i["job_id"] > after][:2]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        jobs = v2.completed_jobs(client, MPA)
    assert seen == [0, 7]
    assert [(j.sample_key, j.sample_id, j.readset_id) for j in jobs] == [
        (MD5, MD5, None), (RS, None, RS), ("x" * 32, "x" * 32, None)]
    assert jobs[0].study_name == "B"  # first collection as returned (the API sorts them)
