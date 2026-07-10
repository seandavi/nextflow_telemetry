"""Parity tests: nf_client.srr must match the server's content-addressing.

Golden values computed from `nextflow_telemetry.utils.srrs_to_sample_id`. If a
change here forces these to move, the server side moved too — and that rehashes
the whole sample catalog, so it should never happen silently.
"""
from nf_client.srr import derive_sample_id, normalize_srrs, parse_srrs, srrs_to_sample_id


def test_sample_id_parity_with_server():
    # md5 of "SRR001;SRR002" — order/dup independent.
    assert srrs_to_sample_id(["SRR002", "SRR001", "SRR001"]) == "ec101225381fb2f2671abe5d836abc00"
    assert srrs_to_sample_id(["ERR12"]) == "11d7b629eda5d48ec9f9bc61a92faa64"


def test_normalize_and_parse():
    assert normalize_srrs([" SRR2 ", "SRR1", "SRR1", ""]) == "SRR1;SRR2"
    assert parse_srrs("SRR1; SRR2 ;;SRR3") == ["SRR1", "SRR2", "SRR3"]


def test_derive_skips_non_accession_rows():
    assert derive_sample_id("Not applicable") is None
    assert derive_sample_id("") is None
    assert derive_sample_id("SRR12345") == srrs_to_sample_id(["SRR12345"])
    # A row mixing a placeholder with a real accession still yields an id.
    assert derive_sample_id("n/a;SRR9") == srrs_to_sample_id(["n/a", "SRR9"])
