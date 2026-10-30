"""Multi-source sample metadata in the shared lake, schema ``cmgd_meta`` (ADR-0012).

Each source lands as loaded, keyed by ``(release, <natural key>)`` so releases sit
side by side; ``source_releases.pinned`` names the one the views read. Writes go
through ``cdsci.lake.upsert`` (MERGE, update only on a real change) inside an
``ops.run``, so a reload of the same release is a no-op and every real change is
one attributed snapshot (monode DUCKLAKE.md).

  load_olm(con, zip)    Olm data package -> olm_* tables (sha256-verified, no evidence quotes),
                        plus olm_sample_values, the wide table's long form
  load_cmd(con, ref)    curatedMetagenomicDataCuration inst/curated/*/*_sample.tsv -> cmd_sample_values
  load_xref(con)        v2 samples + pinned olm/cmd run groupings -> sample_xref (readset_id <-> runs)
  create_views(con)     field_map + country_iso2 seeds; views sample_values (cmd, olm, zoe),
                        sample_harmonized (cmd > zoe > olm), compare_cmd_olm

The package is untrusted input: it is only unzipped (named members) and read as
data, never executed.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
from cdsci.lake import get_settings, lake_connect, ops, upsert  # type: ignore[import-untyped]

from ..etl import lake as etl_lake
from ..etl.v2 import API_URL

SCHEMA = "cmgd_meta"
S = f"lake.{SCHEMA}"
HERE = Path(__file__).parent

OLM_ATTRIBUTION = ("Microbiome Repo, OlmLab ({release}, data package {version}{doi}); "
                   "values derived. CC-BY-4.0")
SOURCES = (
    ops.Source(name="cmgd_meta_olm", lake_schema=SCHEMA,
               description="Microbiome Repo (OlmLab) gut catalog tables per release; "
                           "evidence quotes withheld",
               cadence="manual", distribution="github-release", license="CC-BY-4.0"),
    ops.Source(name="cmgd_meta_cmd", lake_schema=SCHEMA,
               description="curatedMetagenomicData curated sample TSVs, unpivoted, per commit",
               cadence="manual", distribution="github-repo", license="CC0-1.0"),
    ops.Source(name="cmgd_meta_zoe", lake_schema=SCHEMA,
               description="ZOE PREDICT open metadata (age, sex, BMI) per catalog snapshot",
               cadence="manual", distribution="zenodo", license="CC-BY-4.0"),
    ops.Source(name="cmgd_meta", lake_schema=SCHEMA,
               description="cmgd sample metadata: readset xref, field map, harmonized views",
               cadence="manual", distribution="cmgd-public", license="CC0-1.0"),
)

# Olm file -> (table, natural key within a release). Columns are loaded as-is
# except any *quote* column (verbatim paper text, terms unclear; D4).
OLM_TABLES = {
    "gut_studies.parquet": ("olm_studies", ["study_accession"]),
    "gut_runs.parquet": ("olm_runs", ["run_accession"]),
    "gut_sample_metadata_wide.parquet": ("olm_samples_wide", ["sample_key"]),
    "gut_sample_determinations.parquet": ("olm_determinations",
                                          ["sample_key", "field_name", "route", "determined_by"]),
    "gut_sandpiper_sample_summary.parquet": ("olm_sandpiper_summary", ["sample_key"]),
}
CMD_REPO = "waldronlab/curatedMetagenomicDataCuration"
_CMD_ID_COLS = ("study_name", "sample_id", "ncbi_accession", "curator")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def connect(settings=None) -> duckdb.DuckDBPyConnection:
    con = lake_connect(settings or get_settings())
    ops.register_sources(con, writer=etl_lake.WRITER, sources=SOURCES)
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")
    con.execute(f"""CREATE TABLE IF NOT EXISTS {S}.source_releases (
        source VARCHAR, release VARCHAR, package_version VARCHAR, doi VARCHAR,
        license VARCHAR, url VARCHAR, sha256 VARCHAR, loaded_at TIMESTAMPTZ, pinned BOOLEAN)""")
    return con


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(src: str, dest: Path) -> Path:
    """A URL is downloaded into ``dest``; a local path is used in place."""
    if not src.startswith(("http://", "https://")):
        return Path(src)
    out = dest / "download"
    with httpx.stream("GET", src, follow_redirects=True, timeout=600) as r, out.open("wb") as f:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            f.write(chunk)
    return out


def _record_release(con, source: str, release: str, *, package_version: str | None,
                    doi: str | None, license: str, url: str, sha256: str, pin: bool) -> None:
    """Upsert the source_releases row. The first release of a source is pinned;
    later ones only with ``pin`` (which unpins the others)."""
    other_pinned = con.execute(
        f"SELECT count(*) FROM {S}.source_releases WHERE source = ? AND release <> ? AND pinned",
        [source, release]).fetchall()[0][0]
    current = con.execute(f"SELECT pinned FROM {S}.source_releases WHERE source = ? AND release = ?",
                          [source, release]).fetchone()
    pinned = pin or (current[0] if current else other_pinned == 0)
    if pin and other_pinned:
        with ops.run(con, source="cmgd_meta", target=f"{S}.source_releases", version=release):
            con.execute(f"UPDATE {S}.source_releases SET pinned = false "
                        "WHERE source = ? AND release <> ? AND pinned", [source, release])
    row = {"source": source, "release": release, "package_version": package_version, "doi": doi,
           "license": license, "url": url, "sha256": sha256,
           "loaded_at": datetime.now(UTC).isoformat(), "pinned": pinned}
    with ops.run(con, source="cmgd_meta", target=f"{S}.source_releases", version=release) as r:
        r.rows = upsert(con, f"{S}.source_releases", _row_sql(row), ["source", "release"],
                        exclude_change_cols=["loaded_at"])


def _row_sql(row: dict) -> str:
    def lit(k: str, v: object) -> str:
        if v is None:
            return "NULL::VARCHAR"
        if isinstance(v, bool):
            return "true" if v else "false"
        return _q(str(v)) + ("::TIMESTAMPTZ" if k == "loaded_at" else "")
    return "SELECT " + ", ".join(f"{lit(k, v)} AS {k}" for k, v in row.items())


# --- Olm -----------------------------------------------------------------


def load_olm(con: duckdb.DuckDBPyConnection, src: str, *, doi: str | None = None,
             pin: bool = False) -> dict[str, int]:
    """Load one Olm data package (zip URL or path). Every loaded file is checked
    against the package's own VERSION.json (sha256 and row count) before any write."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        zpath = _fetch(src, tmpd)
        zsha = _sha256(zpath)
        with zipfile.ZipFile(zpath) as z:
            version = json.loads(z.read("VERSION.json"))
            for name in OLM_TABLES:
                (tmpd / name).write_bytes(z.read(name))  # named members only, flat
        release, pkg = version["release_id"], version["package_version"]
        if not (_SAFE.match(release) and _SAFE.match(pkg)):
            raise ValueError(f"unexpected release id {release!r} / package version {pkg!r}")
        for name in OLM_TABLES:
            want = version["tables"][name]
            got = _sha256(tmpd / name)
            if got != want["sha256"]:
                raise ValueError(f"{name}: sha256 {got} != VERSION.json {want['sha256']}")
            n = con.execute(f"SELECT count(*) FROM read_parquet({_q(str(tmpd / name))})").fetchall()[0][0]
            if n != want["rows"]:
                raise ValueError(f"{name}: {n} rows != VERSION.json {want['rows']}")

        counts: dict[str, int] = {}
        for name, (table, key) in OLM_TABLES.items():
            path = _q(str(tmpd / name))
            cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet({path})").fetchall()]
            drop = [c for c in cols if "quote" in c]
            exclude = f" EXCLUDE ({', '.join(drop)})" if drop else ""
            sql = (f"SELECT 'olm' AS source, {_q(release)} AS release, {_q(pkg)} AS package_version, "
                   f"*{exclude} FROM read_parquet({path})")
            with ops.run(con, source="cmgd_meta_olm", target=f"{S}.{table}", version=release) as r:
                r.rows = counts[table] = upsert(con, f"{S}.{table}", sql, ["release", *key])
    _check_determinations_join(con, release)
    # The long form is a table, not a view: planners choke on joins through the unnest.
    sql = f"SELECT * FROM ({olm_unpivot_sql(olm_value_fields(con))}) WHERE release = {_q(release)}"
    with ops.run(con, source="cmgd_meta_olm", target=f"{S}.olm_sample_values", version=release) as r:
        r.rows = counts["olm_sample_values"] = upsert(con, f"{S}.olm_sample_values", sql,
                                                      ["release", "sample", "source_field"])
    url = src if src.startswith("http") else f"file:{Path(src).name}"
    _record_release(con, "olm", release, package_version=pkg, doi=doi, license="CC-BY-4.0",
                    url=url, sha256=zsha, pin=pin)
    return counts


def olm_value_fields(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Wide-table fields that carry a ``<f>__route`` / ``<f>__confidence`` triplet."""
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'lake' "
        "AND schema_name = ? AND table_name = 'olm_samples_wide'", [SCHEMA]).fetchall()}
    return sorted(c for c in cols if f"{c}__route" in cols and f"{c}__confidence" in cols)


def olm_unpivot_sql(fields: Iterable[str], schema: str = S) -> str:
    """Long form of the wide table: one row per (sample, field) with a value, its
    route and confidence, plus the determination's evidence_source/determined_by."""
    structs = ", ".join(
        f"{{'source_field': '{f}', 'value_raw': w.{f}::VARCHAR, "
        f"'confidence': w.{f}__confidence, 'route': w.{f}__route}}" for f in fields)
    return f"""
    WITH long AS (
        SELECT w.release, w.study_accession, w.sample_key, w.biosample_accession, unnest(
            list_filter([{structs}], x -> x.value_raw IS NOT NULL), recursive := true)
        FROM {schema}.olm_samples_wide w WHERE w.release_retired IS NULL),
    runs AS (SELECT release, sample_key, list(run_accession ORDER BY run_accession) AS run_accessions
             FROM {schema}.olm_runs WHERE sample_key IS NOT NULL GROUP BY ALL)
    SELECT 'olm' AS source, l.release, l.study_accession AS study, l.sample_key AS sample,
           l.biosample_accession AS biosample, r.run_accessions, l.source_field, l.value_raw,
           NULL::VARCHAR AS value_unit, NULL::VARCHAR AS value_ontology, l.confidence, l.route,
           d.evidence_source, d.determined_by, NULL::VARCHAR AS curator
    FROM long l
    LEFT JOIN {schema}.olm_determinations d ON d.release = l.release AND d.sample_key = l.sample_key
        AND d.field_name = l.source_field AND d.route = l.route AND d.release_retired IS NULL
    LEFT JOIN runs r ON r.release = l.release AND r.sample_key = l.sample_key"""


def _check_determinations_join(con: duckdb.DuckDBPyConnection, release: str) -> None:
    """Every current wide value must meet exactly one current determination (ADR-0012)."""
    fields = olm_value_fields(con)
    bad = con.execute(f"""
        WITH v AS ({olm_unpivot_sql(fields)})
        SELECT count(*) FILTER (WHERE evidence_source IS NULL AND determined_by IS NULL),
               count(*) - count(DISTINCT (sample, source_field))
        FROM v WHERE release = ?""", [release]).fetchall()[0]
    if bad != (0, 0):
        raise ValueError(f"olm {release}: wide->determinations join not 1:1 "
                         f"(unmatched {bad[0]}, duplicated {bad[1]})")


# --- cMD -----------------------------------------------------------------


def unpivot_cmd(rows: Iterable[dict[str, str]], columns: list[str], study: str) -> Iterator[dict]:
    """One curated TSV -> one row per (sample, field) with a value.

    ``<f>_ontology_term_id`` and ``<f>_unit`` columns ride along with ``<f>`` as
    ``value_ontology`` / ``value_unit`` instead of becoming fields; blank and
    ``NA`` cells are absent values."""
    cols = set(columns)

    def folded(c: str) -> bool:
        return c.endswith("_ontology_term_id") or (c.endswith("_unit") and c[:-5] in cols)

    fields = [c for c in columns if c not in _CMD_ID_COLS and not folded(c)]
    for r in rows:
        sample = (r.get("sample_id") or "").strip()
        if not sample:
            continue
        for f in fields:
            v = (r.get(f) or "").strip()
            if v in ("", "NA"):
                continue
            yield {"study_name": (r.get("study_name") or "").strip() or study, "sample_id": sample,
                   "ncbi_accession": (r.get("ncbi_accession") or "").strip() or None,
                   "curator": (r.get("curator") or "").strip() or None, "field": f, "value": v,
                   "value_ontology": (r.get(f"{f}_ontology_term_id") or "").strip() or None,
                   "value_unit": (r.get(f"{f}_unit") or "").strip() or None}


def _resolve_ref(ref: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    r = httpx.get(f"https://api.github.com/repos/{CMD_REPO}/commits/{ref}", timeout=60)
    r.raise_for_status()
    return r.json()["sha"]


def load_cmd(con: duckdb.DuckDBPyConnection, ref: str = "master", *, pin: bool = False) -> int:
    """Load every ``inst/curated/<study>/<study>_sample.tsv`` at one commit; release = the SHA."""
    sha = _resolve_ref(ref)
    url = f"https://codeload.github.com/{CMD_REPO}/tar.gz/{sha}"
    tsv = re.compile(r"^[^/]+/inst/curated/([^/]+)/\1_sample\.tsv$")
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        tgz = _fetch(url, tmpd)
        tsha = _sha256(tgz)
        out = tmpd / "cmd.ndjson"
        with tarfile.open(tgz, "r:gz") as tar, out.open("w") as f:
            for m in tar:
                hit = tsv.match(m.name)
                if not (hit and m.isfile()):
                    continue
                fh = tar.extractfile(m)
                assert fh is not None
                reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"), delimiter="\t")
                rows = list(reader)
                for row in unpivot_cmd(rows, list(reader.fieldnames or []), hit.group(1)):
                    f.write(json.dumps(row) + "\n")
        cols = ["study_name", "sample_id", "ncbi_accession", "curator", "field", "value",
                "value_ontology", "value_unit"]
        types = ", ".join(f"'{c}': 'VARCHAR'" for c in cols)
        src = f"read_json({_q(str(out))}, format = 'newline_delimited', columns = {{{types}}})"
        dups = con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {src} "
                           "GROUP BY study_name, sample_id, field HAVING count(*) > 1)").fetchall()[0][0]
        if dups:
            raise ValueError(f"cmd {sha}: {dups} duplicated (study_name, sample_id, field) keys")
        sql = f"SELECT 'cmd' AS source, {_q(sha)} AS release, {', '.join(cols)} FROM {src}"
        with ops.run(con, source="cmgd_meta_cmd", target=f"{S}.cmd_sample_values", version=sha) as r:
            r.rows = n = upsert(con, f"{S}.cmd_sample_values", sql,
                                ["release", "study_name", "sample_id", "field"])
    _record_release(con, "cmd", sha, package_version=None, doi=None, license="CC0-1.0",
                    url=url, sha256=tsha, pin=pin)
    return n


# --- readset xref --------------------------------------------------------


def readset_id(runs: Iterable[str]) -> str:
    """ADR-0007 readset id over INSDC run accessions (twin of nf_client.readset)."""
    units = sorted({"insdc.sra:" + r.strip() for r in runs})

    def t24u(o) -> str:
        data = json.dumps(o, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(hashlib.sha512(data).digest()[:24]).decode()
    return "RS." + t24u({"units": t24u(units)})


_RUN = re.compile(r"^[SED]RR\d+$")


def _runs(acc: str | None) -> list[str]:
    runs = [a.strip() for a in (acc or "").split(";")]
    return [a for a in runs if _RUN.match(a)]


def _v2_samples() -> list[dict]:
    out: list[dict] = []
    with httpx.Client(timeout=60) as client:
        while True:
            r = client.get(f"{API_URL}/api/samples", params={"limit": 1000, "offset": len(out)})
            r.raise_for_status()
            items = r.json()["items"]
            out.extend(items)
            if len(items) < 1000:
                return out


def load_xref(con: duckdb.DuckDBPyConnection) -> int:
    """Rebuild sample_xref: one row per (readset_id, run_accession).

    Readsets come from v2 (registered, ``origins`` has 'v2') and are predicted
    for every pinned Olm sample (its runs in olm_runs) and cMD sample (its
    ncbi_accession), the groupings registration would use. BioSample, SRA sample
    and BioProject per run come from the pinned olm_runs, else v2's biosample_id."""
    groups: list[tuple[str, str, list[str], str | None]] = []  # origin, readset, runs, biosample
    for s in _v2_samples():
        runs = _runs(s.get("ncbi_accession"))
        if runs and s.get("readset_id"):
            groups.append(("v2", s["readset_id"], runs, s.get("biosample_id")))
    pinned = "(SELECT release FROM {S}.source_releases WHERE source = '{src}' AND pinned)"
    for (runs,) in con.execute(
            f"SELECT list(run_accession) FROM {S}.olm_runs WHERE sample_key IS NOT NULL "
            f"AND release IN {pinned.format(S=S, src='olm')} GROUP BY sample_key").fetchall():
        runs = [r for r in runs if _RUN.match(r)]
        if runs:
            groups.append(("olm", readset_id(runs), runs, None))
    for (acc,) in con.execute(
            f"SELECT DISTINCT ncbi_accession FROM {S}.cmd_sample_values "
            f"WHERE release IN {pinned.format(S=S, src='cmd')}").fetchall():
        runs = _runs(acc)
        if runs:
            groups.append(("cmd", readset_id(runs), runs, None))
    with tempfile.NamedTemporaryFile("w", suffix=".csv") as f:
        w = csv.writer(f)
        w.writerow(["origin", "readset_id", "run_accession", "biosample"])
        for origin, rs, runs, bios in groups:
            w.writerows((origin, rs, run, bios) for run in runs)
        f.flush()
        sql = f"""
            SELECT g.readset_id, g.run_accession,
                   coalesce(any_value(o.sample_accession), any_value(g.biosample)) AS biosample,
                   any_value(o.secondary_sample_accession) AS sra_sample,
                   any_value(o.study_accession) AS bioproject,
                   list(DISTINCT g.origin ORDER BY g.origin) AS origins
            FROM read_csv({_q(f.name)}, header = true, all_varchar = true) g
            LEFT JOIN {S}.olm_runs o ON o.run_accession = g.run_accession
                 AND o.release IN {pinned.format(S=S, src='olm')}
            GROUP BY g.readset_id, g.run_accession"""
        with ops.run(con, source="cmgd_meta", target=f"{S}.sample_xref") as r:
            r.rows = n = upsert(con, f"{S}.sample_xref", sql, ["readset_id", "run_accession"],
                                prune="true")
    return n


# --- seeds and views -----------------------------------------------------

# Value transforms named in field_map.unit_transform. Values stay VARCHAR; a mapped
# field's "no value" spellings (cMD writes "Not applicable") become NULL.
_NUM = "TRY_CAST(v.value_raw AS DOUBLE)"
_MISSING = "('', 'na', 'n/a', 'not applicable', 'not available', 'unknown', 'missing')"
TRANSFORM = f"""CASE WHEN m.field IS NOT NULL AND lower(trim(v.value_raw)) IN {_MISSING} THEN NULL
    ELSE CASE m.unit_transform
    WHEN 'days_to_years' THEN round({_NUM} / 365.25, 2)::VARCHAR
    WHEN 'to_years' THEN round({_NUM} / CASE lower(coalesce(v.value_unit, 'year'))
        WHEN 'month' THEN 12 WHEN 'week' THEN 365.25 / 7 WHEN 'day' THEN 365.25 ELSE 1 END, 2)::VARCHAR
    WHEN 'round2' THEN round({_NUM}, 2)::VARCHAR
    WHEN 'lower' THEN lower(trim(v.value_raw))
    WHEN 'sex' THEN CASE lower(trim(v.value_raw)) WHEN 'f' THEN 'female' WHEN 'm' THEN 'male'
        ELSE lower(trim(v.value_raw)) END
    WHEN 'country_iso2' THEN c.iso2
    WHEN 'case_control' THEN CASE WHEN lower(trim(v.value_raw)) IN ('healthy', 'healthy_control')
        THEN 'healthy_control' ELSE 'case' END
    ELSE v.value_raw END END"""

BMI_PLAUSIBLE = (12.0, 70.0)  # docs/research/metadata-sources/README.md: "under 12 or over 70"


def views_sql(schema: str = S) -> dict[str, str]:
    s = schema
    pinned = f"JOIN {s}.source_releases p ON p.source = v.source AND p.release = v.release AND p.pinned"
    return {
        # Every source's long rows, every release; `field` is the harmonized name
        # where field_map maps it (then `value` is converted), else the source's own.
        "sample_values": f"""
            WITH v AS (
                SELECT source, release, study_name AS study, sample_id AS sample,
                       NULL::VARCHAR AS biosample,
                       list_filter(string_split(ncbi_accession, ';'), x -> x <> '') AS run_accessions,
                       field AS source_field, value AS value_raw, value_unit, value_ontology,
                       NULL::DOUBLE AS confidence, 'curated' AS route, NULL::VARCHAR AS evidence_source,
                       NULL::VARCHAR AS determined_by, curator
                FROM {s}.cmd_sample_values
                UNION ALL BY NAME SELECT * FROM {s}.olm_sample_values
                UNION ALL BY NAME
                SELECT z.source, z.release, z.bioproject AS study, z.source_sample AS sample,
                       l.biosample,
                       list_filter(string_split(l.run_accessions, ';'), x -> x <> '') AS run_accessions,
                       z.field AS source_field, z.value_raw, z.value_unit,
                       NULL::VARCHAR AS value_ontology, NULL::DOUBLE AS confidence, 'curated' AS route,
                       NULL::VARCHAR AS evidence_source, NULL::VARCHAR AS determined_by,
                       NULL::VARCHAR AS curator
                FROM {s}.zoe_sample_values z
                LEFT JOIN {s}.zoe_sample_link l ON l.release = z.release AND l.cohort = z.cohort
                     AND l.source_sample = z.source_sample AND l.link_status = 'resolved'),
            t AS (
                SELECT v.source, v.release, v.study, v.sample, v.biosample, v.run_accessions,
                       coalesce(m.field, v.source_field) AS field, {TRANSFORM} AS value,
                       v.source_field, v.value_raw, v.value_unit, v.value_ontology, v.confidence,
                       v.route, v.evidence_source, v.determined_by, v.curator
                FROM v
                LEFT JOIN {s}.field_map m ON m.source = v.source AND m.source_field = v.source_field
                LEFT JOIN {s}.country_iso2 c ON m.unit_transform = 'country_iso2'
                     AND lower(c.name) = lower(trim(v.value_raw)))
            SELECT t.*, CASE WHEN t.field = 'bmi' AND (TRY_CAST(t.value AS DOUBLE) < {BMI_PLAUSIBLE[0]}
                    OR TRY_CAST(t.value AS DOUBLE) > {BMI_PLAUSIBLE[1]}) THEN 'implausible_bmi' END AS value_flag
            FROM t""",
        # One value per (readset, mapped field) from the pinned releases:
        # cmd > zoe > olm R1/R2 (not external.cmd) > olm R3/R4 with confidence >= 0.5.
        # A value_flag does not demote a value.
        "sample_harmonized": f"""
            WITH v AS (
                SELECT v.*, CASE
                    WHEN v.source = 'cmd' THEN 1
                    WHEN v.source = 'zoe' THEN 2
                    WHEN v.route IN ('R1', 'R2') AND v.evidence_source IS DISTINCT FROM 'external.cmd' THEN 3
                    WHEN v.route IN ('R3', 'R4') AND v.confidence >= 0.5
                         AND v.evidence_source IS DISTINCT FROM 'external.cmd' THEN 4 END AS rank
                FROM {s}.sample_values v {pinned}
                WHERE v.field IN (SELECT field FROM {s}.field_map) AND v.value IS NOT NULL),
            r AS (SELECT DISTINCT x.readset_id, v.*
                  FROM v, unnest(v.run_accessions) AS u(run)
                  JOIN {s}.sample_xref x ON x.run_accession = u.run
                  WHERE v.rank IS NOT NULL)
            SELECT readset_id, field, value, source, release, route, confidence, value_flag
            FROM r QUALIFY row_number() OVER (PARTITION BY readset_id, field
                ORDER BY rank, confidence DESC NULLS LAST, sample) = 1""",
        # Per cMD study x mapped field, pinned releases, Olm's resolved value with
        # (olm_rows = 'all') and without (= 'independent') its external.cmd rows.
        # cMD and Olm samples pair when they share a run; age agrees within 1 year, BMI 0.1.
        "compare_cmd_olm": f"""
            WITH pin AS MATERIALIZED (SELECT source, release FROM {s}.source_releases WHERE pinned),
            cmd_units AS MATERIALIZED (
                SELECT DISTINCT c.study_name AS study, c.sample_id AS sample,
                       trim(unnest(string_split(c.ncbi_accession, ';'))) AS run
                FROM {s}.cmd_sample_values c JOIN pin p ON p.source = 'cmd' AND p.release = c.release),
            link AS MATERIALIZED (
                SELECT DISTINCT u.study, u.sample AS cmd_sample, o.sample_key AS olm_sample,
                       o.study_accession AS olm_study
                FROM cmd_units u JOIN {s}.olm_runs o ON o.run_accession = u.run
                JOIN pin p ON p.source = 'olm' AND p.release = o.release),
            studies AS MATERIALIZED (SELECT DISTINCT study, olm_study FROM link),
            scopes AS (SELECT * FROM (VALUES ('all'), ('independent')) t(olm_rows)),
            mapped AS MATERIALIZED (
                SELECT v.source, v.study, v.sample, v.field, v.value, v.evidence_source
                FROM {s}.sample_values v JOIN pin p ON p.source = v.source AND p.release = v.release
                WHERE v.field IN (SELECT field FROM {s}.field_map) AND v.value IS NOT NULL),
            c AS MATERIALIZED (SELECT m.study, m.sample AS cmd_sample, m.field, m.value, sc.olm_rows
                  FROM mapped m, scopes sc WHERE m.source = 'cmd'),
            o AS MATERIALIZED (SELECT m.study AS olm_study, m.sample AS olm_sample, m.field, m.value, sc.olm_rows
                  FROM mapped m, scopes sc WHERE m.source = 'olm'
                  AND (sc.olm_rows = 'all' OR m.evidence_source IS DISTINCT FROM 'external.cmd')),
            -- cmd values joined to their linked Olm samples first: left to itself the
            -- planner joined c to o on (field, olm_rows) alone, a near cross product.
            cl AS MATERIALIZED (SELECT c.*, l.olm_sample FROM c
                                JOIN link l ON l.study = c.study AND l.cmd_sample = c.cmd_sample),
            pairs AS MATERIALIZED (
                SELECT c.study, c.field, c.olm_rows, c.cmd_sample, o.olm_sample,
                       CASE c.field
                            WHEN 'age_years' THEN abs(TRY_CAST(c.value AS DOUBLE) - TRY_CAST(o.value AS DOUBLE)) <= 1
                            WHEN 'bmi' THEN abs(TRY_CAST(c.value AS DOUBLE) - TRY_CAST(o.value AS DOUBLE)) <= 0.1
                            ELSE c.value = o.value END AS agree
                FROM cl c
                JOIN o ON o.olm_sample = c.olm_sample AND o.field = c.field AND o.olm_rows = c.olm_rows),
            both_ AS (SELECT study, field, olm_rows, count(*) AS n_both,
                             count(*) FILTER (WHERE agree) AS n_agree,
                             count(*) FILTER (WHERE NOT agree OR agree IS NULL) AS n_disagree
                      FROM pairs GROUP BY ALL),
            cmd_only AS (SELECT study, field, olm_rows, count(*) AS n_cmd_only FROM c
                         ANTI JOIN pairs p USING (study, field, olm_rows, cmd_sample) GROUP BY ALL),
            olm_only AS (SELECT st.study, o.field, o.olm_rows, count(DISTINCT o.olm_sample) AS n_olm_only
                         FROM o JOIN studies st ON st.olm_study = o.olm_study
                         ANTI JOIN pairs p ON p.study = st.study AND p.field = o.field
                              AND p.olm_rows = o.olm_rows AND p.olm_sample = o.olm_sample
                         GROUP BY ALL),
            grid AS (SELECT DISTINCT st.study, m.field, sc.olm_rows
                     FROM studies st, {s}.field_map m, scopes sc)
            SELECT g.study, g.field, g.olm_rows,
                   coalesce(b.n_both, 0) AS n_both, coalesce(b.n_agree, 0) AS n_agree,
                   coalesce(b.n_disagree, 0) AS n_disagree,
                   coalesce(co.n_cmd_only, 0) AS n_cmd_only, coalesce(oo.n_olm_only, 0) AS n_olm_only,
                   round(b.n_agree / b.n_both, 4) AS agreement
            FROM grid g
            LEFT JOIN both_ b USING (study, field, olm_rows)
            LEFT JOIN cmd_only co USING (study, field, olm_rows)
            LEFT JOIN olm_only oo USING (study, field, olm_rows)""",
    }


def _seed(con: duckdb.DuckDBPyConnection, name: str, key: list[str]) -> int:
    path = _q(str(HERE / f"{name}.csv"))
    with ops.run(con, source="cmgd_meta", target=f"{S}.{name}") as r:
        r.rows = n = upsert(con, f"{S}.{name}",
                            f"SELECT * FROM read_csv({path}, header = true, all_varchar = true)",
                            key, prune="true")
    return n


def create_views(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Load the seed CSVs and (re)create the views. Views are DDL, so this is a
    separate step from the loads (CREATE OR REPLACE always writes a snapshot)."""
    _seed(con, "field_map", ["source", "source_field"])
    _seed(con, "country_iso2", ["name"])
    made = []
    with ops.run(con, source="cmgd_meta", target=S) as r:
        with r.attribute("views"):
            for name, sql in views_sql().items():
                con.execute(f"CREATE OR REPLACE VIEW {S}.{name} AS {sql}")
                made.append(name)
        r.rows = len(made)
    return made
