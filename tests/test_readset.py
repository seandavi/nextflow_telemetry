"""readset.py (#373): ADR-0007's golden vectors, the same ones nf_client and the Worker are pinned to."""
from nextflow_telemetry.readset import normalize_runs, readset_id, readset_id_for_runs

ZELLER_RUNS = ["ERR478958", "ERR478959", "ERR478960", "ERR478961", "ERR480454", "ERR480455", "ERR480456", "ERR480457"]


def test_single_run_golden_vector():
    assert readset_id_for_runs("SRR000001") == "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"


def test_zeller_golden_vector():
    assert readset_id_for_runs(";".join(ZELLER_RUNS)) == "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"
    assert readset_id("insdc.sra:" + r for r in reversed(ZELLER_RUNS)) == "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"


def test_mixed_archives_golden_vector():
    assert readset_id_for_runs("SRR1;ERR2;DRR3") == "RS.uFRI93utvCk3llTO2fQPqARuz-e6Lj_-"


def test_non_run_entry_has_no_readset():
    assert readset_id_for_runs("SRR1;Not_applicable") is None
    assert readset_id_for_runs("") is None


def test_normalize_runs_sorts_and_dedupes():
    assert normalize_runs("SRR2, SRR1;SRR1") == "SRR1;SRR2"
    assert normalize_runs(" ;, ") == ""
