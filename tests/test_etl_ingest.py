"""End-to-end ingest into a cdsci.lake *local* backend lake (DuckDB-file
catalog, local parquet, sibling ops.duckdb ledger). Published trees are built
from the real excerpts in tests/fixtures/etl/; object storage is the local
filesystem (source._exists/_cat patched), so no rclone, R2, GCS or
Postgres.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import time
from pathlib import Path

import httpx
import pytest
from cdsci.lake import Settings, ops
from test_etl_parsers import FIX, PRESENCE, manifest_230

from nextflow_telemetry.etl import engine, lake, source, v2
from nextflow_telemetry.etl import parsers as P
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
        _put(d / branch / "metaphlan_markers/marker_presence.tsv.gz", gzip.compress(PRESENCE))
    _put(d / "manifest.json", manifest)
    if humann:
        humann = dict(humann)
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


H39_FILES = {"bundle": "humann3.9", "gf": "out_genefamilies.tsv", "pa": "out_pathabundance.tsv",
             "pc": "out_pathcoverage.tsv"}


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    r2, gcs = tmp_path / "r2", tmp_path / "gcs"
    monkeypatch.setattr(source, "SOURCE_BASE", str(r2))
    monkeypatch.setattr(source, "LEGACY_BASES", {("cmgd_nextflow", "2.2.1"): (str(gcs),)})
    monkeypatch.setattr(source, "_exists", os.path.exists)
    monkeypatch.setattr(source, "_cat", lambda p: Path(p).read_bytes() if Path(p).is_file() else None)

    publish(r2, H39, RS, manifest_230("humann3.9", "mpa4.1.1_vJun23"), H39_FILES)
    publish(r2, H4, RS, manifest_230("humann4.0.0a1", "mpa4.1.1_vOct22"),
            {"bundle": "humann4.0.0a1", "gf": "out_2_genefamilies.tsv", "pa": "out_4_pathabundance.tsv"})
    publish(r2, MPA, RS, manifest_230(None))
    publish(r2, MPA, "RS.unpublished", manifest_230(None), complete=False)
    publish(gcs, LEGACY, MD5, (FIX / "2.2.1" / "manifest.json").read_bytes())  # GCS only
    return r2, gcs


@pytest.fixture()
def con(tmp_path):
    c = lake.connect(Settings(_env_file=None, lake_backend="local",
                              storage_base_uri=f"file://{tmp_path}/lake"))
    yield c
    c.close()


def counts(con, table: str) -> dict[str, int]:
    return dict(con.execute(
        f"SELECT workflow_id, count(*) FROM lake.cmgd.{table} GROUP BY 1").fetchall())


TABLES = ["taxonomic_profile_metaphlan", "taxonomic_profile_bracken", "resistome", "qc_metrics",
          "marker_abundance", "marker_presence", "humann_pathabundance", "humann_pathcoverage",
          "humann_genefamilies_files"]


def test_ingest_is_idempotent_and_isolated_per_registration(storage, con):
    r2, _ = storage
    lake.ensure_schema(con)  # second call is a no-op (tables exist, sort already set)

    s39 = engine.process(con, H39, [job(RS, None)])
    assert s39["ingested"] == 1 and s39["skipped_unpublished"] == 0
    assert s39["tables"]["humann_pathabundance"] == 5 and s39["tables"]["humann_pathcoverage"] == 5
    engine.process(con, H4, [job(RS, None)])
    smpa = engine.process(con, MPA, [job(RS, None), job("RS.unpublished", None)])
    assert (smpa["ingested"], smpa["skipped_unpublished"]) == (1, 1)
    engine.process(con, LEGACY, [job(MD5, MD5)])

    before = {t: counts(con, t) for t in TABLES}
    main_mpa = 2 * 6  # two branches x six profile rows
    assert before["taxonomic_profile_metaphlan"] == {
        "cmgd_humann3.9": main_mpa + 3, "cmgd_humann4a1": main_mpa + 3,
        "cmgd_mpa4.2": main_mpa, "cmgd_nextflow": main_mpa}
    assert before["humann_pathcoverage"] == {"cmgd_humann3.9": 5}  # HUMAnN 4 has none
    # Markers are in the lake for every registration (two branches x two rows).
    every = {"cmgd_humann3.9": 4, "cmgd_humann4a1": 4, "cmgd_mpa4.2": 4, "cmgd_nextflow": 4}
    assert before["marker_abundance"] == every and before["marker_presence"] == every

    # Gene families: never loaded, only indexed (one object per HUMAnN sample).
    assert not con.execute("SELECT 1 FROM duckdb_tables() WHERE database_name = 'lake' "
                           "AND table_name = 'humann_genefamilies'").fetchall()
    gf = con.execute("SELECT * FROM lake.cmgd.humann_genefamilies_files ORDER BY workflow_id").fetchall()
    assert [d[0] for d in con.description] == [  # the #229 publication contract
        "sample_key", "readset_id", "workflow_id", "version", "humann_bundle", "branch", "key",
        "size", "sha256", "rows"]
    k39 = f"cmgd_humann3.9/2.3.0/{RS}/humann/humann3.9/out_genefamilies.tsv.gz"
    k4 = f"cmgd_humann4a1/2.3.0/{RS}/humann/humann4.0.0a1/out_2_genefamilies.tsv.gz"
    rows39 = sum(1 for _ in P.parse_humann_genefamilies((FIX / "humann3.9" / "out_genefamilies.tsv").read_bytes()))
    rows4 = sum(1 for _ in P.parse_humann_genefamilies((FIX / "humann4.0.0a1" / "out_2_genefamilies.tsv").read_bytes()))
    assert rows39 and rows4
    assert gf == [
        (RS, RS, "cmgd_humann3.9", "2.3.0", "humann3.9", "full_data", k39,
         os.path.getsize(r2 / k39), hashlib.sha256((r2 / k39).read_bytes()).hexdigest(), rows39),
        (RS, RS, "cmgd_humann4a1", "2.3.0", "humann4.0.0a1", "full_data", k4,
         os.path.getsize(r2 / k4), hashlib.sha256((r2 / k4).read_bytes()).hexdigest(), rows4),
    ]

    # Re-ingest one registration: same rows, no duplicates, and the other
    # registrations sharing the sample key untouched.
    engine.process(con, H39, [job(RS, None)])
    assert {t: counts(con, t) for t in TABLES} == before

    # The lake is the done-set: everything published is ingested, nothing pending.
    assert lake.ingested_keys(con, "cmgd_mpa4.2", "2.3.0") == {RS}
    assert engine.pending([job(RS, None), job("RS.unpublished", None)],
                          lake.ingested_keys(con, "cmgd_mpa4.2", "2.3.0")) == [job("RS.unpublished", None)]

    profiles = con.execute(
        "SELECT DISTINCT workflow_id, data_type, metaphlan_profile, humann_bundle "
        "FROM lake.cmgd.taxonomic_profile_metaphlan ORDER BY ALL").fetchall()
    assert ("cmgd_humann3.9", "full_data", "mpa4.1.1_vJun23", "humann3.9") in profiles
    assert ("cmgd_humann4a1", "full_data", "mpa4.1.1_vOct22", "humann4.0.0a1") in profiles
    assert ("cmgd_humann3.9", "rarefied_data", "mpa4.2.2_vJan25", None) in profiles
    assert ("cmgd_nextflow", "full_data", "mpa4.2.2_vJan25", None) in profiles

    qc = con.execute(
        "SELECT workflow_id, sample_key, sample_id, readset_id, study_name, metaphlan_index, "
        "metaphlan_profile, humann_bundle FROM lake.cmgd.qc_metrics ORDER BY workflow_id").fetchall()
    assert qc == [
        ("cmgd_humann3.9", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", "humann3.9"),
        ("cmgd_humann4a1", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", "humann4.0.0a1"),
        ("cmgd_mpa4.2", RS, None, RS, "ZellerG_2014", None, "mpa4.2.2_vJan25", None),
        ("cmgd_nextflow", MD5, MD5, None, "ZellerG_2014", "mpa_vJan25_CHOCOPhlAnSGB_202503", None, None),
    ]


def test_batches_are_ops_runs_with_attributed_snapshots(storage, con):
    r2, _ = storage
    keys = [f"RS.batch{i}" for i in range(5)]
    for k in keys:
        publish(r2, H39, k, manifest_230("humann3.9", "mpa4.1.1_vJun23"), H39_FILES)
    s = engine.process(con, H39, [job(k, None) for k in keys], batch_size=2)
    assert s["ingested"] == 5 and len(s["runs"]) == 3  # 2 + 2 + 1

    runs = con.execute("SELECT run_id, source, target, version, status, rows_after "
                       "FROM ops.lake_ops.run ORDER BY started_at").fetchall()
    assert [r[0] for r in runs] == s["runs"]
    assert {r[1:5] for r in runs} == {("cmgd", "lake.cmgd", "cmgd_humann3.9/2.3.0", "success")}
    assert sum(r[5] for r in runs) == sum(s["tables"].values())
    assert ops.list_sources(con) == [{"name": "cmgd", "lake_schema": "cmgd",
                                      "description": lake.SOURCE.description,
                                      "cadence": lake.SOURCE.cadence,
                                      "writer": "nextflow_telemetry"}]
    assert ops.get_watermark(con, "cmgd", "cmgd_humann3.9/2.3.0") == {
        "samples": 5, "last_completed_at": "2026-10-07T00:00:00Z"}
    # One snapshot per batch, attributed to the producer.
    authors = con.execute("SELECT author FROM lake.snapshots() WHERE author IS NOT NULL").fetchall()
    assert authors == [("nextflow_telemetry:cmgd",)] * 3
    # Each batch is one insert per table, sorted by (study, sample, feature).
    lake_rows = con.execute("SELECT sample_key, marker_name FROM lake.cmgd.marker_presence").fetchall()
    assert lake_rows == sorted(lake_rows)


def test_volumes_report(storage, con):
    engine.process(con, H39, [job(RS, None)])
    engine.process(con, LEGACY, [job(MD5, MD5)])
    con.execute("CALL ducklake_flush_inlined_data('lake')")  # tiny fixtures are inlined
    report = lake.volumes(con)
    lines = report.splitlines()
    assert lines[0].startswith("| table | registration | samples | rows/sample | bytes/row")
    assert "@200k samples" in lines[0] and "@400k samples" in lines[0]
    md = {(c[0], c[1]): c for c in (
        [x.strip() for x in line.strip("|").split("|")] for line in lines[2:])}
    assert md[("marker_abundance", "cmgd_nextflow 2.2.1")][2:4] == ["1", "4.0"]
    assert md[("marker_abundance", "cmgd_nextflow 2.2.1")][4] != "–"  # measured from files
    gf = md[("gene families (cmgd-raw files)", "cmgd_humann3.9 2.3.0")]
    assert gf[2] == "1" and float(gf[3]) > 0 and gf[4] != "–"  # rows/sample, bytes/row
    assert ("**lake total**", "cmgd_humann3.9 2.3.0") in md
    assert not any(k[1] == "cmgd_nextflow 2.2.1" and k[0].startswith("gene families") for k in md)


def test_stage_is_bulk(con):
    """50k rows stage in well under 10 s (row-wise parameter binding took ~50 s)."""
    rows = [{"sample_key": f"S{i // 1000}", "study_name": "ZellerG_2014", "workflow_id": "w",
             "version": "v", "data_type": "full_data", "marker_name": f"M{i}", "value": i / 7}
            for i in range(50_000)]
    t = time.monotonic()
    lake.stage(con, "marker_abundance", rows)
    assert time.monotonic() - t < 10
    assert con.execute("SELECT count(*), sum(value) FROM stage_marker_abundance").fetchone() == (
        50_000, pytest.approx(sum(r["value"] for r in rows)))


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
