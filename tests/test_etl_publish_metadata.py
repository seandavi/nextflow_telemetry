"""nf-etl publish with the metadata store (lake.cmgd_meta) present: per-source
metadata tables, per-table licences, the samples xref, provenance and pages."""
from __future__ import annotations

import json

import duckdb
from cdsci.lake.publish.builder import LocalDirStore
from cdsci.lake.publish.verify import verify_release
from cmgd_release_fixture import MPA, RELEASE_DAY, _rows, build_lake

from nextflow_telemetry.etl import publish

M = "lake.cmgd_meta"


def _meta_store(con, keys: list[str], runs: list[str]) -> None:
    """lake.cmgd_meta shaped like the metadata store's views (as plain tables)."""
    con.execute(f"CREATE SCHEMA {M}")
    con.execute(f"CREATE TABLE {M}.source_releases (source VARCHAR, release VARCHAR, "
                "package_version VARCHAR, doi VARCHAR, license VARCHAR, url VARCHAR, sha256 VARCHAR, "
                "loaded_at TIMESTAMPTZ, pinned BOOLEAN)")
    con.execute(f"INSERT INTO {M}.source_releases VALUES "
                "('cmd', 'abc123', NULL, NULL, 'Artistic-2.0', 'u', NULL, now(), true), "
                "('olm', 'R2026.25', '1.20.0', '10.5281/zenodo.23087712', 'CC-BY-4.0', 'u', 'x', now(), true), "
                "('olm', 'R2026.20', '1.15.0', NULL, 'CC-BY-4.0', 'u', 'y', now(), false)")
    con.execute(f"CREATE TABLE {M}.sample_xref (readset_id VARCHAR, run_accession VARCHAR, "
                "biosample VARCHAR, sra_sample VARCHAR, bioproject VARCHAR)")
    con.execute(f"CREATE TABLE {M}.sample_values (source VARCHAR, release VARCHAR, study VARCHAR, "
                "sample VARCHAR, biosample VARCHAR, run_accessions VARCHAR[], field VARCHAR, value VARCHAR, "
                "source_field VARCHAR, value_raw VARCHAR, value_unit VARCHAR, value_ontology VARCHAR, "
                "confidence DOUBLE, route VARCHAR, evidence_source VARCHAR, determined_by VARCHAR, "
                "curator VARCHAR)")
    con.execute(f"CREATE TABLE {M}.sample_harmonized (readset_id VARCHAR, field VARCHAR, value VARCHAR, "
                "source VARCHAR, release VARCHAR, route VARCHAR, confidence DOUBLE)")
    con.execute(f"CREATE TABLE {M}.compare_cmd_olm (study VARCHAR, field VARCHAR, olm_rows VARCHAR, "
                "n_both BIGINT, n_agree BIGINT, n_disagree BIGINT, n_cmd_only BIGINT, n_olm_only BIGINT, "
                "agreement DOUBLE)")
    for i, (k, run) in enumerate(zip(keys, runs)):
        con.execute(f"INSERT INTO {M}.sample_xref VALUES (?, ?, ?, NULL, 'PRJEB1')", [k, run, f"SAMEA{i}"])
        con.execute(f"INSERT INTO {M}.sample_values VALUES ('cmd', 'abc123', 'ZellerG_2014', ?, NULL, [?], "
                    "'sex', 'female', 'gender', 'female', NULL, NULL, NULL, 'curated', NULL, NULL, 'lw')",
                    [f"cmd{i}", run])
        for rel in ("R2026.25", "R2026.20"):  # only the pinned release is published
            con.execute(f"INSERT INTO {M}.sample_values VALUES ('olm', ?, 'PRJEB1', ?, ?, [?], 'age_years', "
                        "'40', 'age_at_collection_days', '14610', NULL, NULL, 0.9, 'R3', 'paper', 'llm', NULL)",
                        [rel, f"olm{i}", f"SAMEA{i}", run])
        con.execute(f"INSERT INTO {M}.sample_harmonized VALUES (?, 'sex', 'female', 'cmd', 'abc123', "
                    "'curated', NULL), (?, 'age_years', '40', 'olm', 'R2026.25', 'R3', 0.9)", [k, k])
    con.execute(f"INSERT INTO {M}.compare_cmd_olm VALUES ('ZellerG_2014', 'sex', 'all', 2, 2, 0, 0, 0, 1.0), "
                "('NotInRelease', 'sex', 'all', 1, 1, 0, 0, 0, 1.0)")


def test_release_with_metadata_tables(tmp_path):
    con = build_lake(tmp_path / "lake")
    qc = [r for r in _rows(*MPA)["qc_metrics"] if r["study_name"] == "ZellerG_2014"]
    _meta_store(con, [r["sample_key"] for r in qc], [r["run_ids"] for r in qc])
    m = publish.publish(con, *MPA, tmp_path / "store", today=RELEASE_DAY)
    rdir = tmp_path / "store" / m.dataset / m.release
    tables = {t.name: t for t in m.tables}
    assert {t: tables[t].license for t in publish.META_TABLES} == {
        "sample_metadata_cmd": "CC0-1.0", "sample_metadata_olm": "CC-BY-4.0",
        "sample_metadata_harmonized": "CC-BY-4.0", "metadata_comparison": "CC-BY-4.0",
        "metadata_sources": "CC0-1.0"}
    assert "zenodo.23087712" in tables["sample_metadata_olm"].description
    n = len(qc)
    assert (tables["sample_metadata_cmd"].row_count, tables["sample_metadata_olm"].row_count,
            tables["sample_metadata_harmonized"].row_count, tables["metadata_comparison"].row_count,
            tables["metadata_sources"].row_count) == (n, n, 2 * n, 1, 3)
    olm = duckdb.sql(f"SELECT * FROM read_parquet('{rdir}/tables/sample_metadata_olm/data/part-00000.parquet')")
    assert "evidence_quote" not in olm.columns
    assert {r[0] for r in olm.select("release").fetchall()} == {"R2026.25"}
    samples = duckdb.sql(f"SELECT biosample, bioproject FROM read_parquet('{rdir}/tables/samples/data/"
                         "part-00000.parquet') WHERE study_name = 'ZellerG_2014'").fetchall()
    assert samples and all(b.startswith("SAMEA") and p == "PRJEB1" for b, p in samples)
    assert verify_release(LocalDirStore(tmp_path / "store"), m.dataset, m.release).passed
    prov = json.loads((rdir / "provenance.json").read_text())["cmgd"]
    assert ("olm", "R2026.25") in {(s["source"], s["release"]) for s in prov["metadata_sources"]}
    page = (rdir / "studies" / "ZellerG_2014" / "index.html").read_text()
    assert "Where the values come from" in page and "R2026.25" in page
    assert "CC-BY-4.0" in (tmp_path / "store" / "index.html").read_text()
    assert (rdir / "studies" / "ZellerG_2014" / "metadata.parquet").exists()
