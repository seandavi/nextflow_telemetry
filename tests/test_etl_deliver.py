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


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_manifest_and_tarball(tmp_path):
    (tmp_path / "a.tsv").write_text("readset_id\tf\nRS.a\t1\nRS.b\t2\n")
    (tmp_path / "README.md").write_text("r\n")
    deliver.write_manifest(tmp_path, "t", TODAY)
    tsv = (tmp_path / "MANIFEST.tsv").read_bytes()
    assert tsv.decode().splitlines() == [
        "file\tbytes\tsha256\trows\tcols",
        f"README.md\t2\t{_sha(b'r' + chr(10).encode())}\t\t",
        f"a.tsv\t27\t{_sha((tmp_path / 'a.tsv').read_bytes())}\t2\t2"]
    lines = (tmp_path / "MANIFEST.sha256").read_text().splitlines()
    assert lines == [f"{_sha(tsv)}  MANIFEST.tsv",
                     f"{_sha(b'r' + chr(10).encode())}  README.md",
                     f"{_sha((tmp_path / 'a.tsv').read_bytes())}  a.tsv"]
    with tarfile.open(tmp_path / "t-2026-10-17.tar.gz") as t:
        assert sorted(t.getnames()) == ["t-2026-10-17/MANIFEST.sha256", "t-2026-10-17/MANIFEST.tsv",
                                        "t-2026-10-17/README.md", "t-2026-10-17/a.tsv"]


def test_gzipped_tsv_is_reproducible_and_counted_inside(tmp_path):
    p = tmp_path / "b.tsv.gz"
    deliver.write_tsv(p, ["readset_id", "f", "g"], [["RS.a", 1, None], ["RS.b", 2, 3]])
    first = p.read_bytes()
    deliver.write_tsv(p, ["readset_id", "f", "g"], [["RS.a", 1, None], ["RS.b", 2, 3]])
    assert p.read_bytes() == first  # mtime 0: same data, same sha256
    assert gzip.decompress(first) == b"readset_id\tf\tg\nRS.a\t1\t\nRS.b\t2\t3\n"
    deliver.write_manifest(tmp_path, "t", TODAY)
    row = (tmp_path / "MANIFEST.tsv").read_text().splitlines()[1].split("\t")
    assert row == ["b.tsv.gz", str(len(first)), _sha(first), "2", "3"]


def test_low_features_flag_column_and_capped_caveat(monkeypatch):
    def table(counts):  # sample -> number of non-zero features out of 4
        return ["readset_id", *"wxyz"], [[k, *([1] * n + [0] * (4 - n))] for k, n in counts.items()]
    tables = {"a.tsv.gz": table({"RS.1": 4, "RS.2": 4, "RS.3": 1, "RS.4": 1, "RS.5": 4}),
              "a_relab.tsv.gz": table({"RS.1": 0, "RS.2": 0, "RS.3": 0}),  # skipped
              "b.tsv.gz": table({"RS.1": 4, "RS.2": 1, "RS.3": 1, "RS.4": 1, "RS.5": 4,
                                 "RS.6": 4, "RS.7": 4})}
    mids, low = deliver.low_features(tables)
    assert mids == {"a.tsv.gz": 4, "b.tsv.gz": 4}
    assert low == {"RS.3": ["a.tsv.gz", "b.tsv.gz"], "RS.4": ["a.tsv.gz", "b.tsv.gz"],
                   "RS.2": ["b.tsv.gz"]}
    monkeypatch.setattr(deliver, "LOW_FEATURE_LIST", 1)
    text = " ".join(deliver.caveats(REG, {}, TODAY, mids, low))
    assert "2 in `a.tsv.gz` (median 4); 3 in `b.tsv.gz` (median 4)" in text
    assert "RS.3 (`a.tsv.gz`, `b.tsv.gz`) and 1 more" in text
    assert "RS.2" not in text and "RS.4" not in text  # in samples.tsv.gz only


# Real-shaped BAQLaVa v1.2.0-devel output (cmgd_humann4a2/2.4.2, trimmed): the sample
# column is named after the sample, and the total is max(nucleotide, translated).
BAQLAVA = (
    "BAQLaVa VGB\t{key}\tReference Species\tTaxonomy\tOther ICTV Genomes in VGB\n"
    "VGB_1\t62.5775156082\tCanhaevirus hiberniae\tr__duplodnaviria;k__heunggongvirae;"
    "p__uroviricota\tCanhaevirus hiberniae, Culoivirus faecalis\n"
    "VGB_1|translated\t62.5775156082\tCanhaevirus hiberniae\tr__duplodnaviria\t\n"
    "VGB_100062\t26.4528426797\tVGB_100062\tr__viruses_unclassified\t\n"
    "VGB_100062|nucleotide\t26.4528426797\tVGB_100062\tr__viruses_unclassified\t\n"
    "VGB_1593\t43.5\tVGB_1593\tr__a\t\n"
    "VGB_1593|nucleotide\t13.1\tVGB_1593\tr__a\t\n"
    "VGB_1593|translated\t43.5\tVGB_1593\tr__a\t\n")


def test_parse_baqlava_profile_keeps_vgb_totals():
    raw = BAQLAVA.format(key="RS.6ZeJGceRLYkm8H0zYj50aGrQ06BaYKZK").encode()
    assert list(parsers.parse_baqlava_profile(raw)) == [
        {"vgb": "VGB_1", "abundance": 62.5775156082, "reference_species": "Canhaevirus hiberniae"},
        {"vgb": "VGB_100062", "abundance": 26.4528426797, "reference_species": "VGB_100062"},
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
        "MANIFEST.sha256", "MANIFEST.tsv", "METHODS.md", "README.md", "coverage.tsv.gz",
        "index.html", "metaphlan_mpa4.2.6_vJan26.tsv.gz", "samples.tsv.gz", "t-2026-10-17.tar.gz"]
    assert "Includes UNCLASSIFIED" in (d / "README.md").read_text()
    mpa = list(csv.reader(deliver.read_text(d / "metaphlan_mpa4.2.6_vJan26.tsv.gz"), delimiter="\t"))
    assert mpa[0] == ["readset_id", "UNCLASSIFIED", "k__Archaea", "k__Bacteria", "k__Bacteria|s__X"]
    assert mpa[2] == ["RS.b", "40.0", "60.0", "0", "0"]
    samples = list(csv.DictReader(deliver.read_text(d / "samples.tsv.gz"), delimiter="\t"))
    assert [(s["readset_id"], s["collection"], s["disease"]) for s in samples] == [
        ("RS.a", "S1", "healthy"), ("RS.b", "S1;S2", "")]
    assert list(samples[0])[-2:] == ["disease", "zfield"]  # FIRST_FIELDS first, rest sorted
    assert samples[0]["reads_raw"] == "100"
    cov = list(csv.DictReader(deliver.read_text(d / "coverage.tsv.gz"), delimiter="\t"))
    assert [(c["collection"], c["completed"], c["delivered"]) for c in cov] == [
        ("S1", "2", "2"), ("S2", "2", "1")]


REG4 = Registration(14, "cmgd_humann4a2", "2.4.2",
                    {"metaphlan_profile": "mpa4.1.1_vOct22", "humann_bundle": "humann4.0.0a2",
                     "skip_baqlava": False}, "2.4.3",
                    "https://github.com/seandavi/curatedMetagenomicsNextflow")
H4 = "humann/humann4.0.0a2/"
H4_FILES = {
    f"{H4}out_4_pathabundance.tsv.gz": "# Pathway HUMAnN v4.0.0.alpha.2\tout_Abundance\n"
                                       "UNMAPPED\t20\nUNINTEGRATED\t70\nPWY-1: a\t6\n"
                                       "PWY-1: a|s__X\t6\nPWY-2: b\t4\n",
    f"{H4}out_4_pathabundance_relab_unstratified.tsv.gz": (
        "# Pathway HUMAnN v4.0.0.alpha.2\tout_Abundance\nUNMAPPED\t0.2\nUNINTEGRATED\t0.7\n"
        "PWY-1: a\t0.06\nPWY-2: b\t0.04\n"),
    f"{H4}out_3_reactions_unstratified.tsv.gz": "# Reaction HUMAnN v4.0.0.alpha.2\tout\n"
                                                "UNMAPPED\t10\nUNGROUPED\t30\nRXN-1\t10\n",
}
VERSIONS4 = {"RS.p": ("2.4.2", "06f4f47aaaa"), "RS.q": ("2.4.3", "94b9ea9bbbb")}


def _manifest4(key: str, version: str, commit: str) -> bytes:
    return json.dumps({
        "provenance": {"pipeline_version": version, "git_commit": commit, "git_revision": version,
                       "nextflow_version": "25.10.8", "run_name": f"r-{key}",
                       "start_time": "2026-10-17T15:40:14-04:00",
                       "container": "docker://ghcr.io/seandavi/curatedmetagenomics:metaphlan4.2.6"},
        "parameters": {"metaphlan_profile": "mpa4.1.1_vOct22", "humann_bundle": "humann4.0.0a2"},
        "read_accounting": {"raw": {"number_reads": 60_000_000},
                            "decontaminated": {"number_reads": 57_000_000},
                            "reads_surviving_fraction": 0.95},
        "software_versions": {"awscli": "aws-cli/1", "fastqc": "v0.12.1", "fasterq-dump": "3.2.1",
                              "kneaddata": "v0.12.3", "trimmomatic": "0.39", "kraken2": "2.1.3",
                              "metaphlan": "4.1.1", "bowtie2": "2.5.4",
                              "humann": "v4.0.0.alpha.2", "baqlava": "v1.2.0-devel",
                              "baqlava_image": "ghcr.io/seandavi/humann-baqlava:4.0.0a2-1.2.0"},
    }).encode()


def _build4(tmp_path, monkeypatch):
    fetched = []

    def fetch(prefix, sub):
        key = prefix.rsplit("/", 1)[1]
        fetched.append(sub)
        if sub == "manifest.json":
            return _manifest4(key, *VERSIONS4[key])
        if sub == "full_data/metaphlan_markers/marker_rel_ab_w_read_stats.tsv.gz":
            return gzip.compress(b"#mpa_vOct22_CHOCOPhlAnSGB_202403\nk__Bacteria\t2\t100.0\t-\t9\n")
        if sub == f"baqlava/{key}_BAQLaVa_profile.txt":
            return BAQLAVA.format(key=key).encode()
        return gzip.compress(H4_FILES[sub].encode()) if sub in H4_FILES else None

    items = [_item("RS.p", "completed", "ZhangX_2015"), _item("RS.q", "completed", "baqlava_test")]
    items[0]["completed_at"] = "2026-10-17T20:44:05Z"
    monkeypatch.setattr(source, "locate", lambda w, v, k: f"r2:cmgd-raw/{w}/{v}/{k}")
    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(deliver, "metadata", lambda samples: ({}, []))
    monkeypatch.setattr(deliver, "digest", lambda image: "sha256:f00" if "quay" in image else None)
    return deliver.build(REG4, items, ["ZhangX_2015", "baqlava_test"], "p", tmp_path,
                         today=TODAY), fetched


def _tsv(path):
    return list(csv.reader(deliver.read_text(path), delimiter="\t"))


def test_build_humann4a2_pilot(tmp_path, monkeypatch):
    """cmgd_humann4a2/2.4.2: BAQLaVa at its key-templated path, HUMAnN raw + relab
    pathway and reaction tables, run/raw links, MANIFEST.tsv and index.html."""
    d, fetched = _build4(tmp_path, monkeypatch)

    assert _tsv(d / "baqlava_vgb.tsv.gz")[0] == ["readset_id", "VGB_1", "VGB_100062", "VGB_1593"]
    assert fetched.count(f"{H4}out_3_reactions_unstratified.tsv.gz") == 2  # once per sample
    assert _tsv(d / "humann4_pathabundance.tsv.gz")[1] == ["RS.p", "6.0", "4.0", "70.0", "20.0"]
    assert _tsv(d / "humann4_pathabundance_relab.tsv.gz")[0][1:] == [
        "PWY-1: a", "PWY-2: b", "UNINTEGRATED", "UNMAPPED"]
    rxn = _tsv(d / "humann4_reactions_relab.tsv.gz")
    assert rxn[0] == ["readset_id", "RXN-1", "UNGROUPED", "UNMAPPED"]
    assert [float(v) for v in rxn[1][1:]] == [0.2, 0.6, 0.2]
    assert _tsv(d / "humann4_reactions.tsv.gz")[1] == ["RS.p", "10.0", "30.0", "10.0"]

    s = {r["readset_id"]: r for r in csv.DictReader(deliver.read_text(d / "samples.tsv.gz"), delimiter="\t")}
    raw = "https://cmgd-raw.cancerdatasci.org/cmgd_humann4a2/2.4.2/RS.p/"
    assert s["RS.p"]["raw_url"] == raw and s["RS.p"]["completed_at"] == "2026-10-17T20:44:05Z"
    assert s["RS.p"]["genefamilies_url"] == f"{raw}{H4}out_2_genefamilies_unstratified.tsv.gz"
    assert s["RS.p"]["run_url"] == "https://cmgd.cancerdatasci.org/runs/r-RS.p"
    assert s["RS.q"]["git_revision"] == "2.4.3" and s["RS.q"]["qc_low_feature_tables"] == ""

    readme = (d / "README.md").read_text()
    assert "no UNCLASSIFIED row" in readme and "registration's current" in readme
    assert "UNMAPPED is 20.0–20.0% of the row total" in readme
    assert "humann v4.0.0.alpha.2" in readme and "baqlava v1.2.0-devel" in readme  # pre-release
    assert "Patched UniRef file name" in readme  # specs.CAVEATS

    # MANIFEST.tsv covers every file but itself, the .sha256 and the tarball, with real dims.
    man = {r["file"]: r for r in csv.DictReader((d / "MANIFEST.tsv").open(), delimiter="\t")}
    assert set(man) == {p.name for p in d.iterdir()} - {
        "MANIFEST.tsv", "MANIFEST.sha256", "p-2026-10-17.tar.gz"}
    for f, r in man.items():
        assert int(r["bytes"]) == (d / f).stat().st_size
        assert r["sha256"] == _sha((d / f).read_bytes())
        if f.endswith(".tsv.gz"):
            rows = _tsv(d / f)
            assert (int(r["rows"]), int(r["cols"])) == (len(rows) - 1, len(rows[0]))
        else:
            assert r["rows"] == r["cols"] == ""
    assert f"{_sha((d / 'MANIFEST.tsv').read_bytes())}  MANIFEST.tsv" in (
        d / "MANIFEST.sha256").read_text()

    html = (d / "index.html").read_text()
    missing = [f for f in [*man, "MANIFEST.tsv", "MANIFEST.sha256", "p-2026-10-17.tar.gz"]
               if f'href="{f}"' not in html and f != "index.html"]
    assert not missing
    assert "sha256:f00 (tag resolved on 2026-10-17)" in html and "not recorded" in html
    assert "runs/r-RS.p" not in html  # summary, not a per-sample table
    assert "ZhangX_2015 1 / 1 / 1; baqlava_test 1 / 1 / 1" in html
    assert "median 57.0 million" in html and "2.4.2 (1 sample) / 2.4.3 (1 sample)" in html


def test_methods_names_every_manifest_version(tmp_path, monkeypatch):
    """Drift guard: every version string in the manifests appears in METHODS.md, so
    a template slot can't silently point at the wrong key."""
    d, _ = _build4(tmp_path, monkeypatch)
    text = (d / "METHODS.md").read_text()
    for key, (version, commit) in VERSIONS4.items():
        m = json.loads(_manifest4(key, version, commit))
        wanted = {m["provenance"]["pipeline_version"], m["provenance"]["git_commit"][:7],
                  m["provenance"]["nextflow_version"], *m["parameters"].values(),
                  *(v for k, v in m["software_versions"].items() if k not in deliver.IGNORED_TOOLS)}
        assert not [v for v in wanted if v not in text]
    prose = text.split("## 1. Methods text")[1].split("## 2.")[0]
    for v in ("fasterq-dump 3.2.1", "Trimmomatic 0.39", "Bowtie 2 2.5.4", "KneadData v0.12.3",
              "MetaPhlAn 4.1.1 using the mpa_vOct22_CHOCOPhlAnSGB_202403",
              "HUMAnN v4.0.0.alpha.2", "BAQLaVa v1.2.0-devel", "Nextflow 25.10.8",
              "v2.4.2, commit 06f4f47 (1 sample)"):
        assert v in prose
    assert "not recorded" not in prose
    assert "releases/tag/v1.2.0-devel" in text and "doi:10.1093/nar/gkz862" in text
    assert "Kraken" not in text.split("## 5. How to cite")[1]  # run, but not delivered or cited


def test_olm_columns_for_uncurated_samples_by_biosample_or_run(tmp_path, monkeypatch):
    import duckdb

    wide = tmp_path / "wide.parquet"
    duckdb.connect().execute(f"""COPY (SELECT * FROM (VALUES
        ('SAMN1', 'healthy', 0.9, 'llm', 'adult'),
        ('SRRb', 'disease', 0.5, 'curated', 'infant'),
        ('SAMN9', 'never', 1.0, 'x', 'adult'))
        t(sample_key, health_condition, health_condition__confidence,
          health_condition__route, age_category)) TO '{wide}' (FORMAT parquet)""")

    def fetch(prefix, sub):
        key = prefix.rsplit("/", 1)[1]
        return _manifest(key, "c1") if sub == "manifest.json" else None

    monkeypatch.setattr(source, "locate", lambda w, v, k: f"r2:x/{k}")
    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(deliver, "metadata", lambda samples: ({"RS.g": {"disease": {"cmd"}}}, ["abc"]))
    items = [{**_item("RS.a", "completed", "S1"), "biosample_id": "SAMN1"},  # by BioSample
             _item("RS.b", "completed", "S1"),                                # by run (SRRb)
             _item("RS.g", "completed", "S1")]                                # cMD-curated: no olm_*
    d = deliver.build(REG, items, ["S1"], "t", tmp_path, today=TODAY,
                      olm=deliver.olm_source(tmp_path / "cache" / "w.parquet", str(wide)))

    samples = {s["readset_id"]: s for s in
               csv.DictReader(deliver.read_text(d / "samples.tsv.gz"), delimiter="\t")}
    assert list(samples["RS.a"])[-4:] == ["olm_health_condition", "olm_health_condition__confidence",
                                          "olm_health_condition__route", "olm_age_category"]
    assert (samples["RS.a"]["olm_health_condition"], samples["RS.a"]["olm_age_category"]) == (
        "healthy", "adult")
    assert samples["RS.b"]["olm_health_condition__route"] == "curated"
    assert samples["RS.g"]["olm_health_condition"] == "" and samples["RS.g"]["disease"] == "cmd"
    assert (tmp_path / "cache" / "w.parquet").exists()
    assert deliver.OLM_RELEASE in (d / "README.md").read_text()
    assert deliver.OLM_CITATION in (d / "METHODS.md").read_text()


def test_record_delivery_replaces_same_day_row(tmp_path):
    from cdsci.lake import Settings

    from nextflow_telemetry.etl import lake

    con = lake.connect(Settings(lake_backend="local", storage_base_uri=f"file://{tmp_path}/lake"))
    d = tmp_path / "x" / "2026-10-19"
    d.mkdir(parents=True)
    with gzip.open(d / "samples.tsv.gz", "wt") as f:
        f.write("readset_id\nRS.a\nRS.b\n")
    (d / "MANIFEST.sha256").write_text("abc  samples.tsv.gz\n")
    deliver.record(con, d, "x", REG, ["B", "A"], recipient="Long")
    row = deliver.record(con, d, "x", REG, ["B", "A"], recipient="Long", issue="gh#1")
    assert row["url"].endswith("/deliverables/x/2026-10-19/index.html")
    got = con.execute("SELECT n_samples, collections, expires::VARCHAR, issue "
                      "FROM lake.cmgd.deliveries").fetchall()
    assert got == [(2, ["A", "B"], "2027-01-17", "gh#1")]
