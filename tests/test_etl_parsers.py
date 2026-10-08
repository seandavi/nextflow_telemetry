"""Parser unit checks against 2.2.1 file shapes (no network, no DB).

Locks the three transforms that forced code over declarative config:
percent->fraction normalization, degenerate-presence collapse, and
taxid/rank/SGB extraction from metaphlan's ``|``-delimited lineage.
"""
import gzip
import json
from pathlib import Path

from nextflow_telemetry.etl import parsers as P

METAPHLAN = (
    b"#mpa_vJan25_CHOCOPhlAnSGB_202503\n"
    b"#/usr/local/bin/metaphlan ... -t rel_ab_w_read_stats\n"
    b"#100 reads processed\n"
    b"#SampleID\tMetaphlan_Analysis\n"
    b"UNCLASSIFIED\t-1\t16.5\t-\t700\n"
    b"k__Bacteria\t2\t83.5\t-\t8000\n"
    b"k__Bacteria|p__Bacteroidota|s__Segatella_copri\t2|976|165179\t34.9\t-\t500\n"
    b"k__Bacteria|p__Bacteroidota|s__Segatella_copri|t__SGB1836\t2|976|165179|\t10.0\t-\t100\n"
)

BRACKEN = (
    b"name\ttaxonomy_id\ttaxonomy_lvl\tkraken_assigned_reads\tadded_reads\tnew_est_reads\tfraction_total_reads\n"
    b"Segatella copri\t165179\tS\t10545248\t682838\t11228086\t0.34937\n"
)

RESISTOME = (
    b"#Template\tScore\tExpected\tTemplate_length\tTemplate_Identity\tTemplate_Coverage\t"
    b"Query_Identity\tQuery_Coverage\tDepth\tq_value\tp_value\n"
    b"ARO:3002999|CblA-1\t   12616\t     550\t     891\t   99.21\t  100.00\t   99.21\t  100.00\t   13.90\t11057.25\t1.0e-26\n"
)

PRESENCE = b"#mpa_vJan25\n#SampleID\tMetaphlan_Analysis\nUniRef90_A0A1\t1\nUniRef90_B0B2\t1\n"

MANIFEST = (
    b'{"read_accounting":{"raw":{"number_reads":48137590,"number_bases":7268776090},'
    b'"decontaminated":{"number_reads":47184000,"number_bases":7100000000},'
    b'"reads_surviving_fraction":0.9802,"bases_surviving_fraction":0.9527},'
    b'"parameters":{"metaphlan_index":"mpa_vJan25_CHOCOPhlAnSGB_202503"},'
    b'"provenance":{"pipeline_version":"2.2.1","git_commit":"deadbeef","input_ids":["SRR1","SRR2"]}}'
)


def test_metaphlan_native_units_and_extraction():
    rows = list(P.parse_metaphlan_profile(METAPHLAN))
    assert len(rows) == 4
    kingdom = rows[1]
    assert kingdom["rank"] == "kingdom"
    assert kingdom["relative_abundance"] == 83.5   # native percent, not normalized
    assert kingdom["coverage"] is None and kingdom["estimated_reads"] == 8000
    species = rows[2]
    assert species["rank"] == "species" and species["ncbi_taxid"] == 165179
    assert species["sgb_id"] is None
    sgb = rows[3]
    assert sgb["sgb_id"] == "t__SGB1836" and sgb["rank"] == "strain"
    assert rows[0]["ncbi_taxid"] is None  # UNCLASSIFIED / -1 -> None


def test_bracken_native_fraction_and_reads():
    (row,) = list(P.parse_bracken(BRACKEN))
    assert row["rank"] == "species" and row["ncbi_taxid"] == 165179
    assert row["fraction_total_reads"] == 0.34937  # native read-count fraction
    assert row["estimated_reads"] == 11228086
    assert "relative_abundance" not in row  # bracken doesn't share metaphlan's column


def test_resistome_padded_numerics():
    (row,) = list(P.parse_resistome(RESISTOME))
    assert row["gene"] == "ARO:3002999|CblA-1"
    assert row["template_coverage"] == 100.0 and row["depth"] == 13.90


def test_presence_is_membership_no_value():
    rows = list(P.parse_marker_presence(PRESENCE))
    assert rows == [{"marker_name": "UniRef90_A0A1"}, {"marker_name": "UniRef90_B0B2"}]


def test_qc_maps_number_reads():
    (qc,) = list(P.parse_qc(MANIFEST))
    assert qc["reads_raw"] == 48137590 and qc["reads_decontaminated"] == 47184000
    assert qc["metaphlan_index"].startswith("mpa_vJan25")
    assert qc["run_ids"] == "SRR1;SRR2" and qc["pipeline_version"] == "2.2.1"


# ---------------------------------------------------------------------------
# Real file excerpts (tests/fixtures/etl/): 2.2.1 from r2:cmgd-raw sample
# 05f281407e15a03e65eba0dd74f30fae; HUMAnN 3.9 / 4.0.0a1 from the Alpine pilot
# (pipeline #88/#98, sample G69210).
# ---------------------------------------------------------------------------
FIX = Path(__file__).parent / "fixtures" / "etl"


def _fix(rel: str) -> bytes:
    return (FIX / rel).read_bytes()


def manifest_230(bundle: str | None, humann_profile: str | None = None) -> bytes:
    """A 2.3.0 manifest: the real 2.2.1 one with the parameters block
    build_manifest.py writes since pipeline ADR-0018 (no real 2.3.0 manifest
    exists yet)."""
    m = json.loads(_fix("2.2.1/manifest.json"))
    m["provenance"]["pipeline_version"] = "2.3.0"
    m["parameters"] = {"metaphlan_profile": "mpa4.2.2_vJan25", "store_dir": "/store",
                       "skip_humann": bundle is None, "skip_rarefied": False}
    if bundle:
        m["parameters"] |= {"humann_bundle": bundle, "humann_metaphlan_profile": humann_profile}
    return json.dumps(m).encode()


def test_real_221_metaphlan_bracken_resistome_markers():
    mpa = list(P.parse_metaphlan_profile(_fix("2.2.1/marker_rel_ab_w_read_stats.tsv")))
    assert [r["rank"] for r in mpa] == [None, "kingdom", "phylum", "phylum", "strain", "strain"]
    assert mpa[4]["sgb_id"] == "t__SGB9862" and mpa[4]["estimated_reads"] == 325532
    (vc, *_rest) = list(P.parse_bracken(_fix("2.2.1/bracken.species.txt")))
    assert vc["clade_name"] == "Vibrio cholerae" and vc["ncbi_taxid"] == 666
    assert vc["fraction_total_reads"] == 0.84793 and len(_rest) == 2
    res = list(P.parse_resistome(_fix("2.2.1/card_kma.res")))
    assert len(res) == 2 and res[0]["gene"].startswith("gb|AY536519.1|") and res[1]["depth"] == 0.78
    ma = list(P.parse_marker_abundance(_fix("2.2.1/marker_abundance.tsv")))
    assert ma[0] == {"marker_name": "UniClust90_GCFHOAII01520|1__9|SGB1478", "value": 1.7980826379540922e-06}


def test_gzip_is_transparent():
    raw = _fix("2.2.1/marker_rel_ab_w_read_stats.tsv")
    assert list(P.parse_metaphlan_profile(gzip.compress(raw))) == list(P.parse_metaphlan_profile(raw))


def test_qc_221_keeps_metaphlan_index_no_profile():
    (qc,) = list(P.parse_qc(_fix("2.2.1/manifest.json")))
    assert qc["metaphlan_index"] == "mpa_vJan25_CHOCOPhlAnSGB_202503"
    assert qc["metaphlan_profile"] is None and qc["humann_bundle"] is None
    assert qc["run_ids"] == "ERR866581" and qc["pipeline_version"] == "2.2.1"


def test_qc_230_reads_profile_and_bundle():
    (qc,) = list(P.parse_qc(manifest_230("humann3.9", "mpa4.1.1_vJun23")))
    assert qc["metaphlan_profile"] == "mpa4.2.2_vJan25" and qc["humann_bundle"] == "humann3.9"
    assert qc["metaphlan_index"] is None
    (mpa_only,) = list(P.parse_qc(manifest_230(None)))
    assert mpa_only["humann_bundle"] is None


def test_humann39_genefamilies_strata():
    rows = list(P.parse_humann_genefamilies(_fix("humann3.9/out_genefamilies.tsv")))
    assert rows[0] == {"gene_family": "UNMAPPED", "stratum": None, "abundance": 5886207.0}
    assert rows[2] == {"gene_family": "UniRef90_A0A0F7Q4K5",
                       "stratum": "g__Bifidobacterium.s__Bifidobacterium_longum",
                       "abundance": 62202.2071576517}
    assert len(rows) == 5  # header line skipped


def test_humann39_pathways_abundance_and_coverage():
    ab = list(P.parse_humann_pathabundance(_fix("humann3.9/out_pathabundance.tsv")))
    assert [r["pathway"] for r in ab[:2]] == ["UNMAPPED", "UNINTEGRATED"]
    assert ab[3] == {"pathway": "PWY-7238: sucrose biosynthesis II",
                     "stratum": "g__Bifidobacterium.s__Bifidobacterium_longum",
                     "abundance": 2052.1677068505}
    cov = list(P.parse_humann_pathcoverage(_fix("humann3.9/out_pathcoverage.tsv")))
    assert len(cov) == 5 and set(cov[0]) == {"pathway", "stratum", "coverage"}


def test_humann4a1_numbered_tables():
    gf = list(P.parse_humann_genefamilies(_fix("humann4.0.0a1/out_2_genefamilies.tsv")))
    assert gf[0]["gene_family"] == "READS_UNMAPPED" and gf[2]["stratum"] == "unclassified"
    pa = list(P.parse_humann_pathabundance(_fix("humann4.0.0a1/out_4_pathabundance.tsv")))
    assert pa[3]["pathway"] == "PWY0-1586: peptidoglycan maturation (meso-diaminopimelate containing)"
    assert pa[3]["stratum"] == "s__Bifidobacterium_longum.t__SGB17248"


def test_humann_bundle_metaphlan_profiles():
    for bundle, sgb in (("humann3.9", "t__SGB17248"), ("humann4.0.0a1", "t__SGB17248")):
        rows = list(P.parse_metaphlan_profile(_fix(f"{bundle}/metaphlan_rel_ab_w_read_stats.tsv")))
        assert rows[0]["clade_name"] == "k__Bacteria" and rows[0]["relative_abundance"] == 100.0
        assert rows[-1]["sgb_id"] == sgb
