"""Parser unit checks against 2.2.1 file shapes (no network, no DB).

Locks the three transforms that forced code over declarative config:
percent->fraction normalization, degenerate-presence collapse, and
taxid/rank/SGB extraction from metaphlan's ``|``-delimited lineage.
"""
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
