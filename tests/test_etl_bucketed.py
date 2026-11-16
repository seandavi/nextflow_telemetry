"""nf-etl bucketed: multi-file, accession-sorted release tables from a fixture lake."""
from __future__ import annotations

import csv
import gzip
import json

import duckdb
from cmgd_release_fixture import MPA, RELEASE_DAY, STUDIES, build_lake, sample_keys

from nextflow_telemetry.etl import bucketed
from nextflow_telemetry.etl.v2 import CompletedJob


def test_bucketed_tables_are_sorted_complete_and_many_to_many(tmp_path):
    con = build_lake(tmp_path / "lake")
    shared = sample_keys(MPA[0], "ZellerG_2014")[0]  # one sample in two studies
    jobs = [CompletedJob(k, None, None, k, (study, "Shared_2020") if k == shared else (study,))
            for study in STUDIES for k in sample_keys(MPA[0], study)]
    # 6 samples in buckets of 2, and a tiny target: every bucket is its own file ...
    m = bucketed.build(con, *MPA, tmp_path / "split", jobs=jobs, bucket_samples=2, target_bytes=1)
    # ... and a huge one: the buckets are concatenated back into one file
    one = bucketed.build(con, *MPA, tmp_path / "one", jobs=jobs, bucket_samples=2)
    tables = {t["table"]: t for t in m["tables"]}
    assert m["samples"] == 6 and len(tables["taxonomic_profile_metaphlan"]["files"]) == 3
    assert all(len(t["files"]) == 1 for t in one["tables"])
    assert "taxonomic_profile_metaphlan_by_study" in m["views"] and "sample_studies_by_study" not in m["views"]

    for manifest, out in ((m, tmp_path / "split"), (one, tmp_path / "one")):
        db = duckdb.connect(str(out / "cmgd.duckdb"), read_only=True)
        for t in manifest["tables"]:
            name, order = t["table"], ", ".join(t["sort_by"])
            # every lake row of the registration, once: a shared sample is not duplicated
            n = db.sql(f"SELECT count(*) FROM {name}").fetchall()[0][0]
            assert n == t["row_count"] == sum(f["row_count"] for f in t["files"])
            if name in bucketed.lake.SCHEMAS:
                assert n == con.execute(f"SELECT count(*) FROM lake.cmgd.{name} WHERE workflow_id = ? "
                                        "AND version = ?", list(MPA)).fetchall()[0][0]
                assert "study_name" not in db.sql(f"SELECT * FROM {name}").columns
            assert [c["name"] for c in t["columns"]] == db.sql(f"SELECT * FROM {name}").columns
            assert all(c["doc"] for c in t["columns"]) and t["description"] and t["license"]
            # files are disjoint accession ranges in order, and each is sorted within
            ranges = [(f["min_sample_accession"], f["max_sample_accession"]) for f in t["files"]]
            assert all(a[1] < b[0] for a, b in zip(ranges, ranges[1:]))
            for f in t["files"]:
                rows = db.sql(f"SELECT {order} FROM read_parquet('{out / f['path']}')").fetchall()
                assert rows == db.sql(f"SELECT {order} FROM read_parquet('{out / f['path']}') "
                                      f"ORDER BY {order} NULLS LAST").fetchall()
        # no BioSample in the fixture lake: the accession falls back to the run
        assert db.sql("SELECT count(*) FROM samples WHERE sample_accession LIKE 'ERR%'").fetchall() == [(6,)]
        assert db.sql("SELECT study_name FROM sample_studies WHERE sample_key = $k ORDER BY 1",
                      params={"k": shared}).fetchall() == [("Shared_2020",), ("ZellerG_2014",)]
        # a study's rows through the membership table: the shared sample counts in both
        assert db.sql("SELECT study_name, count(DISTINCT sample_key) FROM sample_studies "
                      "JOIN qc_metrics USING (sample_key) GROUP BY 1 ORDER BY 1").fetchall() == [
            ("ArtachoA_2021", 4), ("Shared_2020", 1), ("ZellerG_2014", 2)]
        # the by-study view: study_name joined in, the shared sample under both studies
        assert db.sql("SELECT study_name, count(DISTINCT sample_key) FROM taxonomic_profile_metaphlan_by_study "
                      "WHERE sample_key = $k GROUP BY 1 ORDER BY 1", params={"k": shared}).fetchall() == [
            ("Shared_2020", 1), ("ZellerG_2014", 1)]
        db.close()
    con.close()


def test_preview_release_is_its_own_dataset_with_studies_and_croissant(tmp_path):
    con = build_lake(tmp_path / "lake")
    shared = sample_keys(MPA[0], "ZellerG_2014")[0]
    jobs = [CompletedJob(k, None, None, k, (study, "Shared_2020", "scratch") if k == shared else (study,))
            for study in STUDIES for k in sample_keys(MPA[0], study)]
    root, base = tmp_path / "store", "https://example.org/public"
    m = bucketed.release(con, *MPA, root, jobs=jobs, public_base_url=base, today=RELEASE_DAY,
                         bucket_samples=2, target_bytes=1, exclude_studies=["scratch"])
    con.close()
    did = "cmgd_mpa4.2-2.3.0-preview"
    rdir = root / did / "2026-10-08"
    assert (m["dataset"], m["release"], m["base_url"]) == (did, "2026-10-08", f"{base}/{did}/2026-10-08")
    assert m == json.loads((rdir / "manifest.json").read_text()) and m["git_sha"]
    # its own pointers; the store's root index and site are not written
    assert json.loads((root / did / "latest.json").read_text())["release"] == "2026-10-08"
    assert not (root / "index.json").exists() and not list(root.glob(".staging-*"))
    assert bucketed.sync_commands(root, *MPA, "r2:b")[0][:3] == ["rclone", "copy", "--immutable"]
    assert len(bucketed.sync_commands(root, *MPA, "r2:b")) == 3
    # views.sql names the public URLs; against the local files it answers queries
    views = (rdir / "views.sql").read_text()
    assert f"{base}/{did}/2026-10-08/tables/samples/data/part-00000.parquet" in views
    db = duckdb.connect()
    db.execute(views.replace(m["base_url"], str(rdir)))
    assert db.sql("SELECT DISTINCT study_name FROM sample_studies ORDER BY 1").fetchall() == [
        ("ArtachoA_2021",), ("Shared_2020",), ("ZellerG_2014",)]  # 'scratch' excluded
    # per-study TSVs through the membership table: the shared sample is in both studies
    index = {s["study_name"]: s for s in json.loads((rdir / "studies" / "index.json").read_text())["studies"]}
    assert {k: v["n_samples"] for k, v in index.items()} == {"ArtachoA_2021": 4, "Shared_2020": 1, "ZellerG_2014": 2}
    for study in ("Shared_2020", "ZellerG_2014"):
        with gzip.open(rdir / "studies" / study / "metaphlan_species.tsv.gz", "rt") as f:
            assert shared in next(csv.reader(f, delimiter="\t"))
        qc = list(csv.DictReader((rdir / "studies" / study / "qc.tsv").open(), delimiter="\t"))
        assert len(qc) == index[study]["n_samples"] and qc[0]["run_accessions"].startswith("ERR")
    assert [f["name"] for f in index["ZellerG_2014"]["files"]] == ["metaphlan_species.tsv.gz", "qc.tsv"]
    # Croissant: every part file listed with its checksum, multi-file tables read through a FileSet
    doc = json.loads((rdir / "croissant.json").read_text())
    assert (doc["version"], doc["url"]) == (m["release"], m["base_url"])
    parts = {f["@id"]: f for f in doc["distribution"] if f["@type"] == "cr:FileObject"}
    for t in m["tables"]:
        assert all(parts[f["path"]]["sha256"] == f["sha256"] for f in t["files"])
        fields = next(rs for rs in doc["recordSet"] if rs["@id"] == f"main.{t['table']}")["field"]
        want = ({"fileSet": {"@id": f"tables/{t['table']}/data"}} if len(t["files"]) > 1
                else {"fileObject": {"@id": t["files"][0]["path"]}})
        assert all({k: v for k, v in f["source"].items() if k != "extract"} == want for f in fields)
    page = (rdir / "index.html").read_text()
    assert "Preview of the new release layout" in page and "application/ld+json" in page
    assert "<code>sample_studies</code>" in page and "`" not in page.split("<h2>Query")[0].split("What changed")[1]
    assert "taxonomic_profile_metaphlan_by_study" in (rdir / "README.md").read_text()
    prov = json.loads((rdir / "provenance.json").read_text())["cmgd"]
    assert prov["registration"]["workflow_id"] == MPA[0] and prov["pipeline_runs"][0]["n_samples"] == 6
