"""scripts/olm_candidates.py ranks Olm studies and filters out the ones we must not file."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb

_path = Path(__file__).resolve().parent.parent / "scripts" / "olm_candidates.py"
_spec = importlib.util.spec_from_file_location("olm_candidates", _path)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _pkg(tmp_path: Path) -> Path:
    con = duckdb.connect()
    # study -> (median Gbp per sample, retired?, flagged samples out of 10)
    studies = {"CHEAP": (2.0, False, 0), "DEAR": (5.0, False, 1), "OLD": (2.0, True, 0),
               "FLAGGED": (2.0, False, 2), "SHALLOW": (0.1, False, 0), "DONE": (2.0, False, 0)}
    con.execute("create table s (study_accession varchar, study_title varchar, short_title varchar,"
                " release_retired varchar, cov_age_at_collection_days double, cov_sex double,"
                " cov_health_condition double)")
    con.execute("create table r (study_accession varchar, sample_key varchar, base_count bigint,"
                " library_strategy varchar, instrument_platform varchar)")
    con.execute("create table q (study_accession varchar, sample_key varchar, qc_low_depth boolean,"
                " qc_low_complexity boolean, qc_non_metagenome_strict boolean)")
    for acc, (gbp, retired, flagged) in studies.items():
        con.execute("insert into s values (?, ?, null, ?, 1, 1, 1)", [acc, acc, "R1" if retired else None])
        for i in range(10):
            con.execute("insert into r values (?, ?, ?, 'WGS', 'ILLUMINA')", [acc, f"{acc}{i}", int(gbp * 1e9)])
            con.execute("insert into q values (?, ?, ?, false, false)", [acc, f"{acc}{i}", i < flagged])
    for t, f in (("s", "gut_studies"), ("r", "gut_runs"), ("q", "gut_sandpiper_sample_summary")):
        con.execute(f"copy {t} to '{tmp_path / f}.parquet' (format parquet)")
    return tmp_path


def test_ranks_cheapest_first_and_drops_retired_registered_flagged_shallow(tmp_path):
    rows = mod.candidates(_pkg(tmp_path), registered=["DONE"], only=[], limit=10)
    assert [r["study_accession"] for r in rows] == ["CHEAP", "DEAR"]
    assert rows[0]["samples"] == 10 and rows[0]["gbp"] == 20.0 and rows[0]["coverage"] == 1.0
    assert "--label add-study" in mod.issue_command(rows[0])


def test_only_restricts_to_named_studies(tmp_path):
    rows = mod.candidates(_pkg(tmp_path), registered=[], only=["DEAR"], limit=10)
    assert [r["study_accession"] for r in rows] == ["DEAR"]
