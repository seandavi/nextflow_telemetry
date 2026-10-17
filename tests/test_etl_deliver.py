"""nf-etl deliver (#302): wide-table merge, coverage counting, README/manifest
writer, and one build against stubbed cmgd-raw and metadata (no network)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import tarfile
from datetime import date

from nextflow_telemetry.etl import deliver, parsers, source
from nextflow_telemetry.etl.v2 import Registration

REG = Registration(7, "cmgd_mpa4.2", "2.3.0", {"metaphlan_profile": "mpa4.2.6_vJan26"}, "2.3.2",
                   "https://github.com/seandavi/curatedMetagenomicsNextflow")
TODAY = date(2026, 10, 17)

METAPHLAN = {
    "RS.a": b"#mpa_vJan26\n#SampleID\tMetaphlan_Analysis\nUNCLASSIFIED\t-1\t10.0\t-\t7\n"
            b"k__Bacteria\t2\t90.0\t-\t80\nk__Bacteria|s__X\t2|9\t90.0\t-\t80\n",
    "RS.b": b"#mpa_vJan26\nUNCLASSIFIED\t-1\t40.0\t-\t7\nk__Archaea\t2157\t60.0\t-\t9\n",
}


def _manifest(key: str, commit: str) -> bytes:
    return json.dumps({
        "provenance": {"pipeline_version": "2.3.2", "git_commit": commit},
        "parameters": {"metaphlan_profile": "mpa4.2.6_vJan26"},
        "read_accounting": {"raw": {"number_reads": 100}, "decontaminated": {"number_reads": 90},
                            "reads_surviving_fraction": 0.9},
        "software_versions": {"metaphlan": "4.2.5", "awscli": f"aws-cli host-{key}"},
    }).encode()


def _item(key, status, *collections):
    return {"job_id": 1, "sample_key": key, "readset_id": key, "status": status,
            "ncbi_accession": f"SRR{key[-1]}", "collections": list(collections)}


ITEMS = [_item("RS.a", "completed", "S1"), _item("RS.b", "completed", "S1", "S2"),
         _item("RS.c", "completed", "S2"), _item("RS.d", "failed", "S2"),
         _item("RS.e", "pending", "S2"), _item("RS.f", "submitted", "S2"),
         _item("RS.g", "completed", "OTHER")]


def test_wide_is_samples_by_union_of_features_zero_filled():
    header, rows = deliver.wide({"RS.b": {"y": 2.0}, "RS.a": {"x": 1.0, "y": 3.0}})
    assert header == ["readset_id", "x", "y"]
    assert rows == [["RS.a", 1.0, 3.0], ["RS.b", 0, 2.0]]


def test_coverage_counts_per_requested_collection():
    cov = deliver.coverage(ITEMS, ["S1", "S2", "NOPE"], delivered={"RS.a", "RS.b"})
    assert cov == [
        {"collection": "S1", "registered": 2, "completed": 2, "delivered": 2,
         "dead_lettered": 0, "pending": 0, "running": 0},
        {"collection": "S2", "registered": 5, "completed": 2, "delivered": 1,
         "dead_lettered": 1, "pending": 1, "running": 1},
        {"collection": "NOPE", "registered": 0, "completed": 0, "delivered": 0,
         "dead_lettered": 0, "pending": 0, "running": 0},
    ]


def test_versions_and_readme_flag_mixed_versions():
    vers = deliver.versions([json.loads(_manifest("a", "c1")), json.loads(_manifest("b", "c2"))])
    assert "awscli" not in vers  # host-dependent noise
    assert vers["metaphlan"] == {"4.2.5": 2} and vers["git_commit"] == {"c1": 1, "c2": 1}
    cov = deliver.coverage(ITEMS, ["S1"], {"RS.a"})
    text = deliver.readme("t", TODAY, REG, cov, vers, [("f.tsv", "doc")], ["abc123"])
    assert "expires on **2027-01-15**" in text and "not a release" in text
    assert "**Mixed versions:** git_commit" in text
    assert "| metaphlan | 4.2.5 (2) |" in text and "cMD curation commit abc123" in text
    same = deliver.readme("t", TODAY, REG, cov, deliver.versions([json.loads(_manifest("a", "c1"))]),
                          [], [])
    assert "directly comparable" in same and "**not included**" in same


def test_manifest_and_tarball(tmp_path):
    (tmp_path / "a.tsv").write_text("x\n")
    (tmp_path / "README.md").write_text("r\n")
    deliver.write_manifest(tmp_path, "t", TODAY)
    lines = (tmp_path / "MANIFEST.sha256").read_text().splitlines()
    assert lines == [f"{hashlib.sha256(b'r' + chr(10).encode()).hexdigest()}  README.md",
                     f"{hashlib.sha256(b'x' + chr(10).encode()).hexdigest()}  a.tsv"]
    with tarfile.open(tmp_path / "t-2026-10-17.tar.gz") as t:
        assert sorted(t.getnames()) == ["t-2026-10-17/MANIFEST.sha256", "t-2026-10-17/README.md",
                                        "t-2026-10-17/a.tsv"]


def test_parse_baqlava_profile_keeps_vgb_totals():
    raw = (b"BAQLaVa VGB\tdemo_Abundance-RPKs\tReference Species\tTaxonomy\tOther\n"
           b"VGB_1593\t43.5\tVGB_1593\tr__a;k__b\t\n"
           b"VGB_1593|nucleotide\t13.1\tVGB_1593\tr__a;k__b\t\n"
           b"VGB_1593|translated\t43.5\tVGB_1593\tr__a;k__b\t\n")
    assert list(parsers.parse_baqlava_profile(raw)) == [
        {"vgb": "VGB_1593", "abundance": 43.5, "reference_species": "VGB_1593"}]


def test_build_writes_only_present_outputs(tmp_path, monkeypatch):
    def fetch(prefix, sub):
        key = prefix.rsplit("/", 1)[1]
        if sub == "manifest.json":
            return _manifest(key, "c1")
        if sub.endswith("marker_rel_ab_w_read_stats.tsv.gz"):
            return gzip.compress(METAPHLAN[key]) if key in METAPHLAN else None
        return None  # no BAQLaVa

    monkeypatch.setattr(source, "locate", lambda w, v, k: None if k == "RS.c" else f"r2:x/{k}")
    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(deliver, "metadata", lambda samples: (
        {"RS.a": {"disease": {"healthy"}, "zfield": {"1"}}}, ["abc"]))
    d = deliver.build(REG, ITEMS, ["S1", "S2"], "t", tmp_path, today=TODAY)

    assert d == tmp_path / "t" / "2026-10-17"
    assert sorted(p.name for p in d.iterdir()) == [
        "MANIFEST.sha256", "README.md", "coverage.tsv", "metaphlan_mpa4.2.6_vJan26.tsv",
        "samples.tsv", "t-2026-10-17.tar.gz"]
    mpa = list(csv.reader((d / "metaphlan_mpa4.2.6_vJan26.tsv").open(), delimiter="\t"))
    assert mpa[0] == ["readset_id", "UNCLASSIFIED", "k__Archaea", "k__Bacteria", "k__Bacteria|s__X"]
    assert mpa[2] == ["RS.b", "40.0", "60.0", "0", "0"]
    samples = list(csv.DictReader((d / "samples.tsv").open(), delimiter="\t"))
    assert [(s["readset_id"], s["collection"], s["disease"]) for s in samples] == [
        ("RS.a", "S1", "healthy"), ("RS.b", "S1;S2", "")]
    assert list(samples[0])[-2:] == ["disease", "zfield"]  # FIRST_FIELDS first, rest sorted
    assert samples[0]["reads_raw"] == "100"
    cov = list(csv.DictReader((d / "coverage.tsv").open(), delimiter="\t"))
    assert [(c["collection"], c["completed"], c["delivered"]) for c in cov] == [
        ("S1", "2", "2"), ("S2", "2", "1")]
