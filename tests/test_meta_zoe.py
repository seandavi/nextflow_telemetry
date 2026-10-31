"""ZOE loader: parse rules, and load through a real catalog + local lake from retained bytes."""
from __future__ import annotations

import asyncio
import bz2
import hashlib

import pytest
from cdsci.lake import Settings
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.meta import store, zoe
from nextflow_telemetry.meta.catalog import CatalogError, SourceCatalog, SourceManifest

HEADER = "sampleID\tage\tsex\tbmi\n"


def rows(text, **kw):
    kw = {"cohort": "P3_X", "scheme": "ena_sample_alias", "bioproject": "PRJEB1", "sha256": "ab" * 32, **kw}
    return list(zoe.parse(text.splitlines(keepends=True), **kw))


def test_parse_keeps_raw_values_ids_and_provenance():
    out = rows(HEADER + "P3_s1\t43\tfemale\t27.45\nP3_s2\t32\tmale\tNA\nP3_s3\t50\t\t\n")
    got = {(r["source_sample"], r["field"]): r["value_raw"] for r in out}
    assert got == {("P3_s1", "age"): "43", ("P3_s1", "sex"): "female", ("P3_s1", "bmi"): "27.45",
                   ("P3_s2", "age"): "32", ("P3_s2", "sex"): "male", ("P3_s3", "age"): "50"}
    assert {r["id_scheme"] for r in out} == {"ena_sample_alias"}
    assert {r["snapshot_sha256"] for r in out} == {"ab" * 32}
    assert next(r for r in out if r["field"] == "bmi")["value_unit"] == "kg/m2"
    # An implausible BMI is data, not an error: flagging belongs downstream.
    assert next(r for r in rows(HEADER + "x\t30\tF\t107.9\n") if r["field"] == "bmi")["value_raw"] == "107.9"


@pytest.mark.parametrize("text,msg", [
    (HEADER + "a\t1\tF\t2\na\t3\tM\t4\n", "duplicate"),
    (HEADER + "\t1\tF\t2\n", "empty"),
    ("sampleID\tage\tsex\tbmi\tdiet\na\t1\tF\t2\tx\n", "unexpected columns"),
    ("sampleID\tage\tsex\na\t1\tF\n", "unexpected columns"),
])
def test_parse_rejects_changed_or_ambiguous_layout(text, msg):
    with pytest.raises(ValueError, match=msg):
        rows(text)


def _capture_all(db_url, tmp_path, files):
    async def go():
        e = create_async_engine(db_url)
        try:
            cat = SourceCatalog(e)
            m = SourceManifest.model_validate({
                "source": {"source_id": zoe.SOURCE_ID, "title": "ZOE", "kind": "zenodo",
                           "url": "https://zenodo.org/records/15308000", "access": "public"},
                "assets": [{"asset_id": a, "locator": f"{a}.tsv.bz2", "role": "sample_metadata"}
                           for a in zoe.COHORTS]})
            await cat.apply(m, actor="t", commit=True)
            for asset, data in files.items():
                f = tmp_path / f"{asset}.bz2"
                f.write_bytes(bz2.compress(data.encode()))
                await cat.capture(zoe.SOURCE_ID, asset, version=zoe.VERSION, file=f,
                                  archive_dir=tmp_path / "archive", actor="t", commit=True)
        finally:
            await e.dispose()
    asyncio.run(go())


def test_load_reads_only_retained_bytes_and_is_idempotent(db_url, tmp_path):
    files = {a: HEADER + f"{c}_1\t40\tF\t22.0\n{c}_2\t41\tM\t\n" for a, (c, _, _) in zoe.COHORTS.items()}
    _capture_all(db_url, tmp_path, files)
    con = store.connect(Settings(_env_file=None, lake_backend="local",
                                 storage_base_uri=f"file://{tmp_path}/lake"))
    try:
        assert zoe.load_zoe(con, db_url) == {"rows": 6 * 5, "samples": 12}
        assert zoe.load_zoe(con, db_url)["rows"] == 6 * 5  # same MERGE, same rows
        n = con.execute(f"SELECT count(*), count(DISTINCT snapshot_sha256) FROM {store.S}.zoe_sample_values").fetchone()
        assert n == (30, 6)
        digests = [r[0] for r in con.execute(
            f"SELECT DISTINCT snapshot_sha256 FROM {store.S}.zoe_sample_values").fetchall()]
        assert con.execute(f"SELECT source, release, license, doi, url, pinned, sha256 "
                           f"FROM {store.S}.source_releases WHERE source = 'zoe'").fetchall() == [
            ("zoe", zoe.VERSION, "CC-BY-4.0", zoe.DOI, zoe.URL, True,
             hashlib.sha256("\n".join(sorted(digests)).encode()).hexdigest())]
        sha = hashlib.sha256(bz2.compress(files["p1-metadata"].encode())).hexdigest()
        schemes = dict(con.execute(
            f"SELECT cohort, any_value(id_scheme) FROM {store.S}.zoe_sample_values GROUP BY 1").fetchall())
        assert schemes["P1"] == "biosample" and schemes["P2"] == "source_local"
        assert con.execute(f"SELECT DISTINCT snapshot_sha256 FROM {store.S}.zoe_sample_values "
                           "WHERE cohort = 'P1'").fetchall() == [(sha,)]
        # Corrupt a retained blob: the load must fail, not parse whatever is there.
        blob = tmp_path / "archive" / "sha256" / sha
        blob.chmod(0o600)
        blob.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="checksum mismatch"):
            zoe.load_zoe(con, db_url)
    finally:
        con.close()


def test_load_requires_every_cohort_to_be_captured(db_url, tmp_path):
    _capture_all(db_url, tmp_path, {"p1-metadata": HEADER + "a\t1\tF\t2\n"})
    con = store.connect(Settings(_env_file=None, lake_backend="local",
                                 storage_base_uri=f"file://{tmp_path}/lake"))
    try:
        with pytest.raises(CatalogError, match="capture it first"):
            zoe.load_zoe(con, db_url)
    finally:
        con.close()


def test_load_zoe_refuses_an_unknown_version():
    """DOI/URL are published as the release's attribution; a new version must update them (#388)."""
    import pytest

    from nextflow_telemetry.meta import zoe

    with pytest.raises(ValueError, match="update VERSION, DOI and URL"):
        zoe.load_zoe(None, "postgresql://unused", version="zenodo-15308000-v2")  # type: ignore[arg-type]


def test_harmonized_table_cites_zoe():
    from nextflow_telemetry.etl import publish

    assert "10.5281/zenodo.15308000" in publish.CITATION["zoe"]
    assert "zenodo.15308000" in publish._TABLES["sample_metadata_harmonized"][0]
