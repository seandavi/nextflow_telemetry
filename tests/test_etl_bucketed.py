"""nf-etl bucketed: multi-file, accession-sorted release tables from a fixture lake."""
from __future__ import annotations

import duckdb
from cmgd_release_fixture import MPA, STUDIES, build_lake, sample_keys

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
        db.close()
    con.close()
