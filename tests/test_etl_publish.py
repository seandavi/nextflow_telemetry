"""nf-etl publish (ADR-0011): one public release per registration, built with
cdsci-lake's publish_release from a local fixture lake (cmgd_release_fixture)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import subprocess

import duckdb
import pytest
from cdsci.lake.publish.builder import LocalDirStore
from cdsci.lake.publish.frozen import frozen_ducklake_attach_sql
from cdsci.lake.publish.verify import verify_release
from cmgd_release_fixture import (  # noqa: F401 -- public_site is a fixture
    GF_FILE, GF_ROWS, HUMANN, LEGACY, SPECIES, STUDIES, build_lake, public_site, sample_keys,
)

from nextflow_telemetry.etl import cli, export, lake, publish
from nextflow_telemetry.etl.specs import SPECS


def _release(site, reg):
    m = site.manifests[reg]
    return m, site.root / m.dataset / m.release


def test_release_is_published_verified_and_indexed(public_site):
    for reg in (LEGACY, HUMANN):
        m, rdir = _release(public_site, reg)
        assert (m.dataset, m.release) == (publish.dataset_id(*reg), "2026-10-08")
        assert {t.name for t in m.tables} == set(publish.published_tables(*reg))
        assert verify_release(LocalDirStore(public_site.root), m.dataset, m.release,
                              contract=publish.dataset_contract(*reg)).passed
        latest = json.loads((public_site.root / m.dataset / "latest.json").read_text())
        assert latest["release"] == m.release
        assert {s.ref for s in m.source_asset_versions} >= {f"lake.cmgd.{t.name}" for t in m.tables}
        assert all(t.license == publish.CMGD_DATA_LICENSE for t in m.tables)
    # gene families are downloads, never a release table; pathways only with HUMAnN
    legacy, _ = _release(public_site, LEGACY)
    humann, _ = _release(public_site, HUMANN)
    assert "humann_genefamilies" not in {t.name for t in humann.tables}
    assert "humann_pathabundance" in {t.name for t in humann.tables}
    assert "humann_pathabundance" not in {t.name for t in legacy.tables}


def test_root_index_lists_every_published_dataset(public_site):
    idx = json.loads((public_site.root / "index.json").read_text())
    assert idx["spec_version"] == export.INDEX_SPEC_VERSION
    assert [d["id"] for d in idx["datasets"]] == sorted(publish.dataset_id(*r) for r in (LEGACY, HUMANN))
    for d in idx["datasets"]:
        latest = json.loads((public_site.root / d["latest"]).read_text())
        assert d["latest_release"] == latest["release"] == "2026-10-08"
        assert (d["workflow_id"], d["version"]) in (LEGACY, HUMANN) and d["updated_at"]
    assert not list(public_site.root.glob(".index-*"))


def test_root_index_merge_replaces_only_the_republished_dataset(tmp_path):
    publish.update_root_index(tmp_path, *LEGACY, "2026-10-08")
    publish.update_root_index(tmp_path, *HUMANN, "2026-10-08")
    publish.update_root_index(tmp_path, *LEGACY, "2026-10-08.2")
    ds = json.loads((tmp_path / "index.json").read_text())["datasets"]
    assert [(d["id"], d["latest_release"]) for d in ds] == [
        ("cmgd_humann3.9-2.3.0", "2026-10-08"), ("cmgd_nextflow-2.2.1", "2026-10-08.2")]


def test_release_holds_only_its_registration_sorted_by_study_sample_feature(public_site):
    _, rdir = _release(public_site, LEGACY)
    rows = duckdb.sql(
        f"SELECT workflow_id, version, study_name, sample_key, clade_name FROM "
        f"read_parquet('{rdir}/tables/taxonomic_profile_metaphlan/data/part-00000.parquet')").fetchall()
    assert {(r[0], r[1]) for r in rows} == {LEGACY}
    keys = [r[2:] for r in rows]
    assert keys == sorted(keys)


def test_frozen_catalog_attaches_over_http(public_site):
    m, _ = _release(public_site, HUMANN)
    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake; INSTALL httpfs; LOAD httpfs;")
    con.execute(frozen_ducklake_attach_sql(f"{public_site.base}/{m.dataset}/{m.release}", alias="cmgd"))
    for t in m.tables:
        assert con.sql(f"SELECT count(*) FROM cmgd.{t.name}").fetchone()[0] == t.row_count
    n = con.sql("SELECT count(*) FROM cmgd.qc_metrics WHERE study_name = 'ArtachoA_2021'").fetchone()[0]
    assert n == STUDIES["ArtachoA_2021"]


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_per_study_artifacts_and_index(public_site):
    _, rdir = _release(public_site, HUMANN)
    index = json.loads((rdir / "studies" / "index.json").read_text())
    assert index["spec_version"] == export.INDEX_SPEC_VERSION
    assert [(s["study_name"], s["n_samples"]) for s in index["studies"]] == sorted(STUDIES.items())
    for s in index["studies"]:
        names = {f["name"] for f in s["files"]}
        assert names == {"metaphlan_species.tsv.gz", "metaphlan.parquet", "bracken.parquet",
                         "resistome.parquet", "pathways.parquet", "qc.tsv"}
        for f in s["files"]:
            assert f["path"] == f"studies/{s['study_name']}/{f['name']}"
            assert (f["size"], f["sha256"]) == ((rdir / f["path"]).stat().st_size, _sha(rdir / f["path"]))
    assert set(index["file_descriptions"]) >= {"metaphlan_species.tsv.gz", "qc.tsv"}
    # the release's other index files, which the manifest can't list (cdsci-lake#134)
    [art] = index["artifacts"]
    assert art["path"] == "genefamilies/index.json"
    assert (art["size"], art["sha256"]) == ((rdir / art["path"]).stat().st_size, _sha(rdir / art["path"]))

    # species x samples, main pass, full_data, every sample a column (sorted), absent = 0
    study = "ArtachoA_2021"
    keys = sorted(sample_keys(HUMANN[0], study))
    with gzip.open(rdir / "studies" / study / "metaphlan_species.tsv.gz", "rt") as f:
        table = list(csv.reader(f, delimiter="\t"))
    assert table[0] == ["clade_name", *keys]
    assert [r[0] for r in table[1:]] == sorted(SPECIES)
    by_species = {r[0]: r[1:] for r in table[1:]}
    order = [sample_keys(HUMANN[0], study).index(k) for k in keys]  # fixture index i per column
    for j, clade in enumerate(SPECIES):
        assert [float(v) for v in by_species[clade]] == [10.0 * (j + 1) + i if j < i else 0.0 for i in order]
    # fixture sample 0 has no species rows: kept, as an all-zero column
    assert all(float(r[1 + order.index(0)]) == 0.0 for r in table[1:])

    long = duckdb.sql(f"SELECT DISTINCT study_name, humann_bundle FROM "
                      f"read_parquet('{rdir}/studies/{study}/metaphlan.parquet')").fetchall()
    assert set(long) == {(study, None), (study, "humann3.9")}  # long file keeps every pass
    qc = list(csv.DictReader(io.StringIO((rdir / "studies" / study / "qc.tsv").read_text()), delimiter="\t"))
    assert sorted(r["sample_key"] for r in qc) == keys

    _, legacy_dir = _release(public_site, LEGACY)
    legacy_index = json.loads((legacy_dir / "studies" / "index.json").read_text())
    assert "pathways.parquet" not in {f["name"] for f in legacy_index["studies"][0]["files"]}
    assert legacy_index["artifacts"] == []


def test_genefamilies_index_is_split_per_study(public_site):
    _, rdir = _release(public_site, HUMANN)
    idx = json.loads((rdir / "genefamilies" / "index.json").read_text())
    assert idx["spec_version"] == export.INDEX_SPEC_VERSION and idx["raw_base_url"] == public_site.raw
    assert [(s["study_name"], s["n_samples"], s["n_files"]) for s in idx["studies"]] == [
        (k, n, n) for k, n in sorted(STUDIES.items())]
    keys = []
    for s in idx["studies"]:
        assert s["path"] == f"genefamilies/{s['study_name']}.json"
        assert (s["size"], s["sha256"]) == ((rdir / s["path"]).stat().st_size, _sha(rdir / s["path"]))
        study = json.loads((rdir / s["path"]).read_text())
        assert study["spec_version"] == export.INDEX_SPEC_VERSION
        assert study["study_name"] == s["study_name"]
        for f in study["files"]:
            assert f["study_name"] == s["study_name"] and f["branch"] == "full_data"
            assert f["url"] == f"{public_site.raw}/{f['key']}"
            assert (f["size"], f["sha256"], f["rows"]) == (
                len(GF_FILE), hashlib.sha256(GF_FILE).hexdigest(), GF_ROWS)
            keys.append(f["key"])
    [art] = idx["artifacts"]
    assert art["path"] == "genefamilies/index.tsv"
    assert (art["size"], art["sha256"]) == ((rdir / art["path"]).stat().st_size, _sha(rdir / art["path"]))
    tsv = list(csv.DictReader(io.StringIO((rdir / "genefamilies" / "index.tsv").read_text()), delimiter="\t"))
    assert [r["key"] for r in tsv] == keys
    _, legacy_dir = _release(public_site, LEGACY)
    assert not (legacy_dir / "genefamilies").exists()


def test_genefamilies_url_is_null_without_raw_base(tmp_path):
    con = build_lake(tmp_path / "lake")
    m = publish.publish(con, *HUMANN, tmp_path / "store", raw_base_url=None)
    gf = tmp_path / "store" / m.dataset / m.release / "genefamilies"
    assert json.loads((gf / "index.json").read_text())["raw_base_url"] is None
    files = json.loads((gf / "ArtachoA_2021.json").read_text())["files"]
    assert all(f["url"] is None and f["key"] for f in files)
    assert not list((tmp_path / "store").glob(".staging-*"))  # staging cleaned up


def test_contract_documents_every_published_column():
    for reg in SPECS:
        for t in publish.published_tables(*reg):
            c = publish.table_contract(t)  # KeyError on an undocumented column/table
            assert [col.name for col in c.columns] == list(lake.SCHEMAS[t])


def test_parse_registration():
    assert publish.parse_registration("cmgd_humann3.9/2.3.0") == HUMANN
    with pytest.raises(ValueError, match="no OutputSpec"):
        publish.parse_registration("cmgd_nextflow/9.9.9")


def test_sync_uploads_releases_before_pointers(tmp_path, monkeypatch, capsys):
    cmds = publish.sync_commands(tmp_path, "cmgd_nextflow-2.2.1", "r2:cmgd-public")
    assert cmds[0][:3] == ["rclone", "copy", "--immutable"]
    assert cmds[0][-2:] == [f"{tmp_path}/cmgd_nextflow-2.2.1", "r2:cmgd-public/cmgd_nextflow-2.2.1"]
    assert [c[-2:] for c in cmds[1:]] == [
        [f"{tmp_path}/cmgd_nextflow-2.2.1/releases.json", "r2:cmgd-public/cmgd_nextflow-2.2.1/releases.json"],
        [f"{tmp_path}/cmgd_nextflow-2.2.1/latest.json", "r2:cmgd-public/cmgd_nextflow-2.2.1/latest.json"],
        [f"{tmp_path}/index.json", "r2:cmgd-public/index.json"]]
    with pytest.raises(ValueError):
        publish.sync_commands(tmp_path, "../x")

    def no_run(*a, **k):
        raise AssertionError("dry run must not call rclone")
    monkeypatch.setattr(subprocess, "run", no_run)
    cli.main(["publish", "--registration", "cmgd_nextflow/2.2.1", "--sync", "--dry-run",
              "--out", str(tmp_path)])
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4 and out[-1].endswith("r2:cmgd-public/index.json")


def test_cli_publish_builds_from_the_lake(tmp_path, monkeypatch, capsys):
    import cdsci.lake

    con = build_lake(tmp_path / "lake")
    monkeypatch.setattr(cdsci.lake, "lake_connect", lambda read_only: con)
    cli.main(["publish", "--registration", "cmgd_nextflow/2.2.1", "--out", str(tmp_path / "store")])
    assert "cmgd_nextflow-2.2.1" in capsys.readouterr().out
    assert json.loads((tmp_path / "store" / "cmgd_nextflow-2.2.1" / "latest.json").read_text())["release"]
