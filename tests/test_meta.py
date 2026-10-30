"""nf-meta: cMD unpivot, readset ids, and the views over a tiny in-memory schema."""
import csv

import duckdb

from nextflow_telemetry.meta import store, zoe, zoe_link


def test_unpivot_cmd_folds_ontology_and_unit_columns():
    cols = ["study_name", "sample_id", "age", "age_unit", "disease", "disease_ontology_term_id",
            "bmi", "curator", "ncbi_accession"]
    rows = [{"study_name": "X_2020", "sample_id": "s1", "age": "18", "age_unit": "Month",
             "disease": "Healthy", "disease_ontology_term_id": "NCIT:C115935", "bmi": "NA",
             "curator": "c", "ncbi_accession": "SRR1;SRR2"}]
    out = {r["field"]: r for r in store.unpivot_cmd(rows, cols, "X_2020")}
    assert set(out) == {"age", "disease"}  # bmi NA dropped; unit/ontology folded
    assert out["age"]["value_unit"] == "Month"
    assert out["disease"]["value_ontology"] == "NCIT:C115935"
    assert out["age"]["ncbi_accession"] == "SRR1;SRR2" and out["age"]["curator"] == "c"


def test_readset_id_golden_vectors():  # ADR-0007
    assert store.readset_id(["SRR000001"]) == "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"
    runs = ["ERR478958", "ERR478959", "ERR478960", "ERR478961",
            "ERR480454", "ERR480455", "ERR480456", "ERR480457"]
    assert store.readset_id(runs[::-1] + runs[:1]) == "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"


def _base_schema(con):
    con.execute("CREATE SCHEMA m")
    con.execute("""CREATE TABLE m.source_releases AS SELECT * FROM (VALUES
        ('olm', 'R1', true), ('cmd', 'abc', true)) t(source, release, pinned)""")
    # One Olm sample (two runs) with age (from cMD) and sex (independent, disagrees).
    con.execute("""CREATE TABLE m.olm_samples_wide AS SELECT 'R1' AS release, 'PRJ1' AS study_accession,
        'SAM1' AS sample_key, 'SAM1' AS biosample_accession, NULL::VARCHAR AS release_retired,
        10958.0 AS age_at_collection_days, 0.9 AS age_at_collection_days__confidence,
        'R2' AS age_at_collection_days__route,
        'male' AS sex, 0.8 AS sex__confidence, 'R1' AS sex__route,
        NULL::DOUBLE AS bmi, NULL::DOUBLE AS bmi__confidence, NULL::VARCHAR AS bmi__route""")
    con.execute("""CREATE TABLE m.olm_determinations AS SELECT * FROM (VALUES
        ('R1', 'SAM1', 'age_at_collection_days', 'R2', 'external.cmd', 'import', NULL),
        ('R1', 'SAM1', 'sex', 'R1', 'biosample', 'parser', NULL))
        t(release, sample_key, field_name, route, evidence_source, determined_by, release_retired)""")
    con.execute("""CREATE TABLE m.olm_runs AS SELECT * FROM (VALUES
        ('R1', 'SRR1', 'SAM1', 'PRJ1'), ('R1', 'SRR2', 'SAM1', 'PRJ1'))
        t(release, run_accession, sample_key, study_accession)""")
    con.execute("""CREATE TABLE m.cmd_sample_values AS SELECT * FROM (VALUES
        ('cmd', 'abc', 'X_2020', 's1', 'SRR1;SRR2', 'c', 'age', '30.4', NULL, 'Year'),
        ('cmd', 'abc', 'X_2020', 's1', 'SRR1;SRR2', 'c', 'sex', 'Female', NULL, NULL),
        ('cmd', 'abc', 'X_2020', 's1', 'SRR1;SRR2', 'c', 'country', 'Japan', NULL, NULL))
        t(source, release, study_name, sample_id, ncbi_accession, curator, field, value,
          value_ontology, value_unit)""")
    con.execute(f"CREATE TABLE m.field_map AS SELECT * FROM read_csv('{store.HERE / 'field_map.csv'}', all_varchar=true)")
    con.execute(f"CREATE TABLE m.country_iso2 AS SELECT * FROM read_csv('{store.HERE / 'country_iso2.csv'}', all_varchar=true)")
    con.execute("CREATE TABLE m.sample_xref AS SELECT 'RS.x' AS readset_id, unnest(['SRR1', 'SRR2']) AS run_accession")
    con.execute("CREATE TABLE m.olm_sample_values AS "
                + store.olm_unpivot_sql(["age_at_collection_days", "bmi", "sex"], "m"))
    for table, columns in (("zoe_sample_values", zoe.COLUMNS), ("zoe_sample_link", zoe_link.COLUMNS)):
        con.execute(f"CREATE TABLE m.{table} ({', '.join(c + ' VARCHAR' for c in columns)})")


def test_views_unpivot_harmonize_and_compare():
    con = duckdb.connect()
    _base_schema(con)
    for name, sql in store.views_sql("m").items():
        con.execute(f"CREATE VIEW m.{name} AS {sql}")

    olm = con.execute("SELECT source_field, value_raw, route, evidence_source, run_accessions "
                      "FROM m.olm_sample_values ORDER BY 1").fetchall()
    assert olm == [("age_at_collection_days", "10958.0", "R2", "external.cmd", ["SRR1", "SRR2"]),
                   ("sex", "male", "R1", "biosample", ["SRR1", "SRR2"])]
    vals = dict(con.execute("SELECT source || ':' || field, value FROM m.sample_values").fetchall())
    assert vals == {"olm:age_years": "30.0", "olm:sex": "male", "cmd:age_years": "30.4",
                    "cmd:sex": "female", "cmd:country": "JP"}
    harm = dict(con.execute("SELECT field, source FROM m.sample_harmonized").fetchall())
    assert harm == {"age_years": "cmd", "sex": "cmd", "country": "cmd"}

    cmp = {(f, s): (b, a, d, c, o) for f, s, b, a, d, c, o in con.execute(
        "SELECT field, olm_rows, n_both, n_agree, n_disagree, n_cmd_only, n_olm_only "
        "FROM m.compare_cmd_olm").fetchall()}
    assert len(cmp) == 12  # 6 fields x {all, independent}
    assert cmp[("age_years", "all")] == (1, 1, 0, 0, 0)  # within 1 year
    assert cmp[("age_years", "independent")] == (0, 0, 0, 1, 0)  # external.cmd dropped
    assert cmp[("sex", "independent")] == (1, 0, 1, 0, 0)
    assert cmp[("bmi", "all")] == (0, 0, 0, 0, 0)

    with open(store.HERE / "field_map.csv") as f:
        assert {r["field"] for r in csv.DictReader(f)} == {
            "age_years", "sex", "country", "bmi", "antibiotics_current_use", "health_condition"}


def test_views_harmonize_zoe_precedence_sex_and_bmi_flag():
    con = duckdb.connect()
    _base_schema(con)
    con.execute("INSERT INTO m.source_releases VALUES ('zoe', 'Z1', true)")
    # (cohort, sample, link_status, runs, age, sex, bmi)
    samples = [("P3_US21", "P3_a", "resolved", "SRR1", "50", "female", "107.9"),
               ("P1", "P1_b", "resolved", "ERR9", "43", "M", "22.0"),
               ("P2", "predict2_1", "unresolved_no_key", None, None, "F", None),
               ("P3_US21", "P3_c", "ambiguous", "ERR7;ERR8", None, "male", None),
               ("P3_US21", "P3_lo", "resolved", "ERR21", None, None, "12"),
               ("P3_US21", "P3_hi", "resolved", "ERR22", None, None, "70"),
               ("P3_US21", "P3_lo2", "resolved", "ERR23", None, None, "11.9"),
               ("P3_US21", "P3_hi2", "resolved", "ERR24", None, None, "70.1")]
    for cohort, sample, status, runs, age, sex, bmi in samples:
        for field, v in (("age", age), ("sex", sex), ("bmi", bmi)):
            if v is not None:
                con.execute("INSERT INTO m.zoe_sample_values (release, cohort, bioproject, source_sample, "
                            "field, value_raw, value_unit, source) VALUES ('Z1', ?, 'PRJ', ?, ?, ?, "
                            "?, 'zoe')", [cohort, sample, field, v, {"age": "year", "bmi": "kg/m2"}.get(field)])
        con.execute("INSERT INTO m.zoe_sample_link (release, cohort, source_sample, link_status, "
                    "biosample, run_accessions) VALUES ('Z1', ?, ?, ?, 'SAMX', ?)",
                    [cohort, sample, status, runs])
    con.execute("INSERT INTO m.sample_xref VALUES ('RS.z', 'ERR9'), ('RS.amb', 'ERR7')")
    con.execute("""INSERT INTO m.olm_sample_values (source, release, study, sample, biosample, run_accessions,
        source_field, value_raw, confidence, route, evidence_source) VALUES
        ('olm', 'R1', 'PRJ1', 'SAM2', 'SAM2', ['ERR9'], 'sex', 'female', 0.9, 'R1', 'biosample')""")
    for name, sql in store.views_sql("m").items():
        con.execute(f"CREATE VIEW m.{name} AS {sql}")

    def harm(readset):
        return {f: (v, src, rel, flag) for f, v, src, rel, flag in con.execute(
            "SELECT field, value, source, release, value_flag FROM m.sample_harmonized "
            "WHERE readset_id = ?", [readset]).fetchall()}

    x = harm("RS.x")
    assert x["age_years"][1] == x["sex"][1] == "cmd"
    assert x["bmi"] == ("107.9", "zoe", "Z1", "implausible_bmi")
    assert harm("RS.z") == {"age_years": ("43.0", "zoe", "Z1", None), "sex": ("male", "zoe", "Z1", None),
                            "bmi": ("22.0", "zoe", "Z1", None)}
    assert harm("RS.amb") == {}
    assert con.execute("SELECT value, run_accessions FROM m.sample_values WHERE sample = 'predict2_1'"
                       ).fetchall() == [("female", None)]
    flags = dict(con.execute("SELECT sample, value_flag FROM m.sample_values WHERE field = 'bmi' "
                             "AND sample LIKE 'P3_%'").fetchall())
    assert flags == {"P3_a": "implausible_bmi", "P3_lo": None, "P3_hi": None,
                     "P3_lo2": "implausible_bmi", "P3_hi2": "implausible_bmi"}
