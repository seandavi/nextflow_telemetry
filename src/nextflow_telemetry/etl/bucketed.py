"""Bucketed, sorted Parquet for one registration's release tables.

The multi-file release layout. ``build`` writes the tables into a directory;
``release`` wraps that into a preview release in the local store under its own
dataset id (``<workflow_id>-<version>-preview``), so the stable dataset, its
``latest.json`` and the store's root ``index.json`` are untouched.
``cdsci.publish`` writes one file per table, so the bundle files (views,
manifest, Croissant) are written here until it takes a file list.

No step sorts a whole table, so memory is bounded by a bucket, not the corpus:

1. ``samples`` (the hub): one row per sample with its ``sample_accession`` (the
   BioSample, else the lowest run accession, else the sample key). Samples are
   ordered by it and cut into buckets of ``bucket_samples``. Accessions are
   handed out per submission, so a study's samples sit next to each other
   without a study column on the facts.
2. Each fact table streams out of the lake once, partitioned by bucket.
3. Each bucket is sorted on its own (sample_accession, sample_key, feature).
4. Consecutive sorted buckets are concatenated into files of about
   ``target_bytes``. Buckets are disjoint, ordered accession ranges, so the
   files are too: a reader prunes on ``sample_accession`` min/max.

Fact tables carry ``sample_accession``, ``sample_key`` and their own columns;
the other ids are in ``samples``. Study membership is many-to-many and lives
only in ``sample_studies``.

Every table with a sample has a ``<table>_by_study`` view (the table joined to
``sample_studies``); a sample in two studies is in both, so those views are for
filtering on ``study_name``, not for corpus-wide totals.

  <out>/manifest.json, views.sql, cmgd.duckdb
  <out>/tables/<table>/data/part-00000.parquet, part-00001.parquet, ...
  release only: croissant.json, provenance.json, README.md, index.html,
                studies/index.json, studies/<study>/metaphlan_species.tsv.gz, qc.tsv
"""
from __future__ import annotations

import hashlib
import json
import re
import resource
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path

import duckdb
from cdsci.publish import build_db, croissant, sql_literal  # type: ignore[import-untyped]

from . import export, lake, publish, site
from .specs import DATABASES
from .v2 import CompletedJob, Registration

BUCKET_SAMPLES = 500        # samples sorted together: ~15M marker rows, sorts in memory
TARGET_BYTES = 700_000_000  # per file; the asked-for range is 500 MB - 1 GB
KEY = "sample_accession"
HUB, STUDIES = "samples", "sample_studies"
BY_STUDY = "_by_study"
PREVIEW = "preview"
MANIFEST_VERSION = "2-preview"  # tables carry `files`, not one `path`
KEY_DOC = ("The sample's INSDC accession: its BioSample, else its lowest run accession, else "
           "the sample key. Tables are sorted and split into files by it, so a study's "
           "samples are adjacent.")
_TABLE_DOCS = {
    HUB: "One row per sample: accession, readset id, runs, BioSample, BioProject and the "
         "pipeline version that produced it. Every other table joins to it on sample_key.",
    STUDIES: "Study membership, many-to-many: one row per study and sample. A sample in two "
             "studies has two rows.",
}
# On the hub, not on every fact row: free on disk (RLE) but ~500 B/row in a sort.
_HUB_ONLY = ("sample_id", "readset_id", "study_name", "run_ids", "workflow_id", "version")


def fact_columns(table: str) -> list[str]:
    return [KEY] + [c for c in lake.SCHEMAS[table] if c not in _HUB_ONLY]


def sort_by(table: str) -> list[str]:
    """Accession first (the bucket order), then the table's primary key."""
    return [KEY, "sample_key"] + [c for c in publish._TABLES[table][2] if c != "sample_key"]


def _doc(column: str) -> str:
    if column == KEY:
        return KEY_DOC
    description, units = publish._COLUMNS[column]
    return f"{description} ({units})" if units else description


def _load(con: duckdb.DuckDBPyConnection, name: str, cols: Sequence[str], rows: list[dict]) -> None:
    """A VARCHAR temp table from Python rows, through NDJSON (see ``lake.stage``)."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({', '.join(f'{c} VARCHAR' for c in cols)})")
    if not rows:
        return
    types = ", ".join(f"'{c}': 'VARCHAR'" for c in cols)
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
        f.flush()
        con.execute(f"INSERT INTO {name} SELECT {', '.join(cols)} FROM read_json('{f.name}', "
                    f"format = 'newline_delimited', columns = {{{types}}})")


def _hub(con: duckdb.DuckDBPyConnection, sid: int, workflow_id: str, version: str,
         jobs: Sequence[CompletedJob], bucket_samples: int,
         exclude_studies: Collection[str] = ()) -> None:
    """``bucket_hub``: the samples table plus each sample's bucket."""
    _load(con, "bucket_jobs", ("sample_key", "run_accessions", "completed_at"),
          [{"sample_key": j.sample_key, "run_accessions": j.run_accessions,
            "completed_at": j.completed_at} for j in jobs])
    # ponytail: every collection is published as a study; filter on collection type
    # once v2 job items carry it (ad hoc collections and bioproject aliases are in here).
    _load(con, "bucket_members", ("study_name", "sample_key"),
          [{"study_name": c, "sample_key": j.sample_key} for j in jobs for c in j.collections
           if c not in exclude_studies])
    xref = (f"SELECT readset_id, min(nullif(biosample, '')) AS acc, "
            "string_agg(DISTINCT biosample, ';' ORDER BY biosample) AS biosample, "
            "string_agg(DISTINCT bioproject, ';' ORDER BY bioproject) AS bioproject "
            f"FROM lake.{publish.META_SCHEMA}.sample_xref GROUP BY readset_id"
            if publish._has_meta(con, "sample_xref") else
            "SELECT NULL::VARCHAR AS readset_id, NULL::VARCHAR AS acc, NULL::VARCHAR AS biosample, "
            "NULL::VARCHAR AS bioproject WHERE false")
    # ponytail: accessions sort as strings, so the files' min/max statistics are the
    # physical order. Prefix-then-number is slightly tighter (span 1.06 vs 1.34 on the
    # 2026-10-20 release) but needs a derived sort column on every fact row.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE bucket_hub AS
        WITH x AS ({xref}),
        q AS (SELECT sample_key, readset_id, run_ids, pipeline_version, git_commit
              FROM lake.{publish.LAKE_SCHEMA}.qc_metrics AT (VERSION => {sid})
              WHERE workflow_id = $w AND version = $v),
        h AS (SELECT coalesce(x.acc,
                              list_min(string_split(coalesce(j.run_accessions, q.run_ids), ';')),
                              q.sample_key) AS {KEY},
                     q.sample_key, q.readset_id,
                     coalesce(j.run_accessions, q.run_ids) AS run_accessions,
                     x.biosample, x.bioproject, q.pipeline_version, q.git_commit, j.completed_at
              FROM q LEFT JOIN bucket_jobs j USING (sample_key) LEFT JOIN x USING (readset_id))
        SELECT *, ((row_number() OVER (ORDER BY {KEY}, sample_key) - 1) // {int(bucket_samples)})::INTEGER
                  AS bucket FROM h""", {"w": workflow_id, "v": version})


def _file(out: Path, path: Path, rows: int, lo: str | None, hi: str | None) -> dict:
    with path.open("rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    return {"path": path.relative_to(out).as_posix(), "row_count": rows,
            "bytes": path.stat().st_size, "sha256": sha256, f"min_{KEY}": lo, f"max_{KEY}": hi}


def _copy(con: duckdb.DuckDBPyConnection, select: str, path: Path, params: dict | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return con.execute(f"COPY ({select}) TO {sql_literal(str(path))} "
                       "(FORMAT parquet, COMPRESSION zstd)", params or {}).fetchall()[0][0]


def _columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[dict]:
    return [{"name": c[0], "type": c[1], "doc": _doc(c[0])} for c in con.sql(
        f"DESCRIBE SELECT * FROM read_parquet({sql_literal(str(path))})").fetchall()]


def _range(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[str | None, str | None]:
    if KEY not in {c["name"] for c in _columns(con, path)}:
        return None, None
    lo, hi = con.execute(f"SELECT min({KEY}), max({KEY}) FROM read_parquet({sql_literal(str(path))})").fetchall()[0]
    return lo, hi


def _fact(con: duckdb.DuckDBPyConnection, table: str, sid: int, workflow_id: str, version: str,
          out: Path, stage: Path, target_bytes: int) -> list[dict]:
    """One fact table: partition by bucket, sort each bucket, concatenate to ~target_bytes."""
    cols = fact_columns(table)
    parts = stage / table / "parts"
    parts.parent.mkdir(parents=True)
    # One streaming pass over the lake. No ORDER BY: the hash join against the hub
    # (one row per sample) is the only state. Row order is irrelevant here, and
    # keeping it makes a partitioned COPY buffer much more.
    con.execute("SET preserve_insertion_order = false")
    con.execute(
        f"COPY (SELECT h.bucket, h.{KEY}, {', '.join(f'f.{c}' for c in cols[1:])} "
        f"FROM (SELECT * FROM lake.{publish.LAKE_SCHEMA}.{table} AT (VERSION => {sid})) AS f "
        "JOIN bucket_hub h USING (sample_key) WHERE f.workflow_id = $w AND f.version = $v) "
        f"TO {sql_literal(str(parts))} (FORMAT parquet, PARTITION_BY (bucket))",
        {"w": workflow_id, "v": version})
    # The concatenation below relies on it; cdsci.lake connections turn it off.
    con.execute("SET preserve_insertion_order = true")
    order = ", ".join(sort_by(table))
    buckets = []  # (sorted file, rows), in accession order
    dirs = sorted(parts.glob("bucket=*"), key=lambda d: int(d.name.partition("=")[2])) if parts.exists() else []
    for d in dirs:
        path = stage / table / "sorted" / f"{d.name.partition('=')[2]}.parquet"
        n = _copy(con, f"SELECT {', '.join(cols)} FROM read_parquet({sql_literal(str(d / '*.parquet'))}) "
                       f"ORDER BY {order}", path)
        shutil.rmtree(d)
        buckets.append((path, n))
    groups: list[list[tuple[Path, int]]] = []
    size = 0
    for b in buckets:  # greedy: close a file once it has reached the target
        if not groups or size >= target_bytes:
            groups.append([])
            size = 0
        groups[-1].append(b)
        size += b[0].stat().st_size
    files = []
    for i, group in enumerate(groups):
        path = out / "tables" / table / "data" / f"part-{i:05d}.parquet"
        if len(group) == 1:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(group[0][0]), str(path))
        else:
            # The list is in accession order, so this is a concatenation, not a sort.
            names = ", ".join(sql_literal(str(p)) for p, _ in group)
            _copy(con, f"SELECT * FROM read_parquet([{names}])", path)
            for p, _ in group:
                p.unlink()
        files.append(_file(out, path, sum(n for _, n in group), *_range(con, path)))
    if not files:  # an empty table still has a file, so its view binds
        path = out / "tables" / table / "data" / "part-00000.parquet"
        types = ", ".join(f"NULL::{'VARCHAR' if c == KEY else lake.SCHEMAS[table][c]} AS {c}" for c in cols)
        _copy(con, f"SELECT {types} WHERE false", path)
        files.append(_file(out, path, 0, None, None))
    shutil.rmtree(stage / table)
    return files


def _metadata(con: duckdb.DuckDBPyConnection) -> list[tuple[str, list[str], str]]:
    """(table, sort order, SELECT) for each metadata table the lake has. They are
    ``publish.META_TABLES`` read against the hub: where a release's table leads
    with study_name, these lead with the accession."""
    meta, out = f"lake.{publish.META_SCHEMA}", []
    for table, (view, select) in publish.META_TABLES.items():
        if not publish._has_meta(con, view):
            continue
        if table == "metadata_comparison":  # per study, not per sample
            out.append((table, ["study_name", "field", "olm_rows"], select.format(
                meta=meta, samples="(SELECT DISTINCT study_name FROM bucket_members)")))
        elif "{samples}" in select:
            inner = select.format(meta=meta, samples=f"(SELECT {KEY} AS study_name, sample_key, "
                                                     "readset_id FROM bucket_hub)")
            order = [KEY, "sample_key", "field"]
            out.append((table, order, f"SELECT * RENAME (study_name AS {KEY}) FROM ({inner}) "
                                      f"ORDER BY {', '.join(order)}"))
        else:
            out.append((table, ["source", "release"], select.format(meta=meta)))
    return out


def views_sql(tables: list[dict], base_url: str) -> str:
    """One view per table over all of its files, and ``<table>_by_study`` for every
    table that carries a sample."""
    stmts = []
    for t in tables:
        urls = ", ".join(sql_literal(f"{base_url}/{f['path']}") for f in t["files"])
        stmts.append(f"CREATE OR REPLACE VIEW main.{t['table']} AS SELECT * FROM read_parquet([{urls}])")
        stmts.append(f"COMMENT ON VIEW main.{t['table']} IS {sql_literal(t['description'])}")
    for name in by_study_views(tables):
        table = name.removesuffix(BY_STUDY)
        stmts.append(f"CREATE OR REPLACE VIEW main.{name} AS SELECT m.study_name, t.* "
                     f"FROM main.{table} t JOIN main.{STUDIES} m USING ({KEY}, sample_key)")
        stmts.append(f"COMMENT ON VIEW main.{name} IS " + sql_literal(
            f"{table} with study_name from {STUDIES}. A sample in two studies appears in both: "
            "filter on study_name, and use the base table for corpus-wide totals."))
    return ";\n".join(stmts) + ";\n"


def by_study_views(tables: list[dict]) -> list[str]:
    return [t["table"] + BY_STUDY for t in tables if t["table"] != STUDIES
            and {KEY, "sample_key"} <= {c["name"] for c in t["columns"]}]


def build(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str, out: Path | str, *,
          jobs: Sequence[CompletedJob] = (), snapshot: int | None = None,
          base_url: str | None = None, bucket_samples: int = BUCKET_SAMPLES,
          target_bytes: int = TARGET_BYTES, memory_limit: str | None = "32GB",
          threads: int | None = 16, exclude_studies: Collection[str] = ()) -> dict:
    """Build the bucketed tables of one registration into ``out`` (new or empty);
    returns the manifest.

    ``con`` has the shared lake attached as ``lake``. ``jobs`` are the registration's
    completed v2 jobs: run accessions, completion times and collections (the study
    memberships, minus ``exclude_studies``). ``base_url`` is where ``out`` will be
    served from (default: the local directory, so ``cmgd.duckdb`` works in place).
    ``memory_limit`` and ``threads`` are set on ``con``: DuckDB's own default is 80%
    of RAM."""
    out = Path(out).absolute()
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} is not empty: build into a new directory")
    out.mkdir(parents=True, exist_ok=True)
    stage = out / ".stage"
    stage.mkdir()
    con.execute(f"SET temp_directory = {sql_literal(str(stage / 'tmp'))}")
    if memory_limit:
        con.execute(f"SET memory_limit = {sql_literal(memory_limit)}")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    sid = snapshot or con.sql("SELECT max(snapshot_id) FROM lake.snapshots()").fetchall()[0][0]
    base_url = (base_url or str(out)).rstrip("/")
    start = time.monotonic()
    _hub(con, sid, workflow_id, version, jobs, bucket_samples, exclude_studies)

    tables, seconds = [], {}

    def add(table: str, order: list[str], files: list[dict], t0: float) -> None:
        tables.append({
            "schema": "main", "table": table, "name": table,
            "description": _TABLE_DOCS.get(table) or publish._TABLES[table][0],
            "license": publish.LICENSES.get(table, publish.CMGD_DATA_LICENSE),
            "sort_by": order, "row_count": sum(f["row_count"] for f in files),
            "bytes": sum(f["bytes"] for f in files),
            "columns": _columns(con, out / files[0]["path"]), "files": files})
        seconds[table] = round(time.monotonic() - t0, 1)
        print(f"{table:32} {tables[-1]['row_count']:>15,} rows {len(files):>4} files "
              f"{tables[-1]['bytes'] / 1e6:>10,.1f} MB {seconds[table]:>8.1f} s", flush=True)

    singles = [
        (HUB, [KEY, "sample_key"],
         f"SELECT * EXCLUDE (bucket) FROM bucket_hub ORDER BY {KEY}, sample_key"),
        (STUDIES, ["study_name", KEY, "sample_key"],
         f"SELECT m.study_name, h.{KEY}, h.sample_key FROM (SELECT DISTINCT * FROM "
         "bucket_members) m JOIN bucket_hub h USING (sample_key) ORDER BY ALL"),
        *_metadata(con)]
    for table, order, select in singles:
        t0 = time.monotonic()
        path = out / "tables" / table / "data" / "part-00000.parquet"
        n = _copy(con, select, path)
        add(table, order, [_file(out, path, n, *_range(con, path))], t0)
    for table in publish.published_tables(workflow_id, version):
        if table != publish.SAMPLES_TABLE:
            t0 = time.monotonic()
            add(table, sort_by(table), _fact(con, table, sid, workflow_id, version, out, stage,
                                             target_bytes), t0)
    shutil.rmtree(stage)

    views = views_sql(tables, base_url)
    (out / "views.sql").write_text(views)
    if base_url.startswith(("http://", "https://")):
        build_db(out / publish.VIEWS_DB, views, base_url, out)
    else:
        duckdb.connect(str(out / publish.VIEWS_DB)).execute(views).close()
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "dataset": publish.dataset_id(workflow_id, version), "base_url": base_url,
        "lake_snapshot": sid, "bucket_samples": bucket_samples, "target_bytes": target_bytes,
        "samples": tables[0]["row_count"], "tables": tables, "views": by_study_views(tables),
        "build": {"seconds": round(time.monotonic() - start, 1), "table_seconds": seconds,
                  "memory_limit": memory_limit, "threads": threads,
                  # Linux reports ru_maxrss in KB
                  "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024},
    }
    publish._write_json(out / "manifest.json", manifest)
    return manifest


def croissant_doc(manifest: dict, title: str, description: str) -> dict:
    """Croissant 1.1 for a bucketed release. ``cdsci.publish.croissant`` describes one
    file per table, so it runs on each table's first file; a table with several files
    then gets every part as a FileObject and a FileSet its fields read from."""
    base = manifest["base_url"]
    licenses = sorted({t["license"] for t in manifest["tables"]})
    doc = croissant(
        {"release": manifest["release"], "published_at": manifest["published_at"], "base_url": base,
         "tables": [{**t, "path": t["files"][0]["path"], "bytes": t["files"][0]["bytes"],
                     "sha256": t["files"][0]["sha256"], "comment": t["description"]}
                    for t in manifest["tables"]]},
        dataset={
            "name": title, "description": description, "url": base,
            "license": [publish.LICENSE_URLS[lic] for lic in licenses],
            "creator": {"@type": "Organization", "name": "curatedMetagenomicData"},
            "citeAs": " ".join(publish.CITATION[k] for k in ("cmgd", "olm", "zoe")
                               if k == "cmgd" or publish.OLM_LICENSE in licenses),
            "keywords": publish.KEYWORDS,
        },
        field_semantics={
            f"main.{t['table']}": {"sample_key": {"references": {"field": {"@id": f"main.{HUB}/sample_key"}}}}
            for t in manifest["tables"]
            if t["table"] != HUB and "sample_key" in {c["name"] for c in t["columns"]}})
    multi = [t for t in manifest["tables"] if len(t["files"]) > 1]
    if multi:
        root = {"@type": "cr:FileObject", "@id": "release", "name": "release", "contentUrl": base,
                "encodingFormat": "https",
                # a directory has no checksum; its manifest's stands in for it
                "sha256": manifest["manifest_sha256_of"]}
        first = {t["files"][0]["path"] for t in multi}
        parts = [f for f in doc["distribution"] if f["@id"] not in first]
        for t in multi:
            fileset = f"tables/{t['table']}/data"
            parts += [{"@type": "cr:FileObject", "@id": f["path"], "name": f["path"],
                       "contentUrl": f"{base}/{f['path']}", "containedIn": {"@id": "release"},
                       "encodingFormat": "application/x-parquet",
                       "contentSize": f"{f['bytes']} B", "sha256": f["sha256"]} for f in t["files"]]
            parts.append({"@type": "cr:FileSet", "@id": fileset, "name": fileset,
                          "containedIn": {"@id": "release"},
                          "encodingFormat": "application/x-parquet",
                          "includes": f"{fileset}/*.parquet"})
            for rs in doc["recordSet"]:
                if rs["@id"] == f"main.{t['table']}":
                    for f in rs["field"]:
                        f["source"] = {"fileSet": {"@id": fileset}, "extract": f["source"]["extract"]}
        doc["distribution"] = [root, *parts]
    return doc


def _studies(db: duckdb.DuckDBPyConnection, bundle: Path, dataset: str) -> list[dict]:
    """``studies/<study>/qc.tsv`` and ``metaphlan_species.tsv.gz`` for every study in
    ``sample_studies``, plus ``studies/index.json``. A sample in two studies is in both."""
    index = []
    member = f"(SELECT {KEY}, sample_key FROM {STUDIES} WHERE study_name = $s)"
    for study, n in db.sql(f"SELECT study_name, count(*) FROM {STUDIES} GROUP BY 1 ORDER BY 1").fetchall():
        if not export.SAFE_NAME.match(study):
            raise ValueError(f"study name not safe as a path segment: {study!r}")
        d = bundle / "studies" / study
        d.mkdir(parents=True)
        # pipeline_version and git_commit are on the hub too
        qc = db.sql(f"SELECT s.*, q.* EXCLUDE ({KEY}, sample_key, pipeline_version, git_commit) "
                    f"FROM {member} m "
                    f"JOIN {HUB} s USING ({KEY}, sample_key) JOIN qc_metrics q USING ({KEY}, sample_key) "
                    f"ORDER BY {KEY}, sample_key", params={"s": study})
        qc.write_csv(str(d / export.QC_TSV), sep="\t", na_rep="", header=True)
        db.execute(f"CREATE OR REPLACE TEMP TABLE species AS SELECT sample_key, clade_name, "
                   f"relative_abundance FROM taxonomic_profile_metaphlan JOIN {member} m "
                   f"USING ({KEY}, sample_key) WHERE rank = 'species' AND data_type = 'full_data' "
                   "AND humann_bundle IS NULL", {"s": study})
        # One column per qc sample, so samples without species rows are all-zero columns.
        keys = ", ".join(sql_literal(k) for (k,) in qc.select("sample_key").fetchall())
        if keys:
            db.sql(f"PIVOT species ON sample_key IN ({keys}) USING first(relative_abundance) "
                   "GROUP BY clade_name ORDER BY clade_name").write_csv(
                str(d / export.SPECIES_TSV), sep="\t", na_rep="0", header=True, compression="gzip")
        index.append({"study_name": study, "n_samples": n,
                      "files": [export.entry(bundle, p) for p in sorted(d.iterdir())]})
    publish._write_json(bundle / "studies" / "index.json", {
        "spec_version": export.INDEX_SPEC_VERSION, "dataset": dataset,
        "file_descriptions": {k: export.STUDY_FILE_DOCS[k] for k in (export.SPECIES_TSV, export.QC_TSV)},
        "studies": index})
    return index


def _snippets(url: str, study: str) -> list[str]:
    return [f"""-- DuckDB 1.5.2+: attach the release's views (read-only, fetches only what a query needs)
INSTALL httpfs; LOAD httpfs;
ATTACH '{url}/cmgd.duckdb' AS cmgd;
SELECT study_name, count(*) AS n_samples FROM cmgd.{STUDIES} GROUP BY 1 ORDER BY 1;""",
            f"""-- One study's species profiles. study_name lives in {STUDIES}; the
-- <table>{BY_STUDY} views join it in, and DuckDB reads only that study's row groups.
SELECT sample_key, clade_name, relative_abundance
FROM cmgd.taxonomic_profile_metaphlan{BY_STUDY}
WHERE study_name = '{study}' AND rank = 'species' AND data_type = 'full_data'
  AND humann_bundle IS NULL;""",
            f"""-- A study's samples: accession, readset id, runs, BioSample, BioProject
SELECT {KEY}, sample_key, run_accessions, biosample, bioproject
FROM cmgd.{HUB}{BY_STUDY} WHERE study_name = '{study}';""",
            f"""-- Corpus-wide totals use the base tables: a sample in two studies is
-- counted twice in the {BY_STUDY} views.
SELECT count(DISTINCT sample_key) FROM cmgd.qc_metrics;"""]


_CHANGES = (
    f"Tables are split into several Parquet files of about 700 MB, sorted by `{KEY}` "
    "(the BioSample), then `sample_key`, then the feature. Query the views in `cmgd.duckdb` "
    "or read `files` per table from `manifest.json`; there is no single `part-00000.parquet` "
    "for the large tables.",
    f"Result tables no longer carry `study_name`, `sample_id`, `readset_id`, `run_ids`, "
    f"`workflow_id` or `version`. They carry `{KEY}` and `sample_key`; the other ids are in "
    f"`{HUB}`.",
    f"Study membership is many-to-many and lives in `{STUDIES}`. Each table with a sample "
    f"has a `<table>{BY_STUDY}` view that joins it in.",
    "Per-study Parquet files are gone (the views replace them). `metaphlan_species.tsv.gz` "
    "and `qc.tsv` per study remain.",
    "`manifest.json` lists `files` per table (`manifest_version` \"2-preview\"), and "
    "`croissant.json` describes the same tables.",
)


def _readme(m: dict, title: str, study: str) -> str:
    rows = "\n".join(f"| `{t['table']}` | {t['row_count']:,} | {len(t['files'])} | "
                     f"{t['bytes'] / 1e6:,.1f} MB | {t['license']} | {t['description']} |"
                     for t in m["tables"])
    snippets = "\n\n".join(f"```sql\n{s}\n```" for s in _snippets(m["base_url"], study))
    return (f"# {title}\n\n**Preview of the new release layout.** It may change or be removed; "
            "the stable dataset and its `latest.json` are unchanged.\n\n"
            f"{m['samples']:,} samples, lake snapshot {m['lake_snapshot']}, release {m['release']}.\n\n"
            "## What changed\n\n" + "\n".join(f"- {c}" for c in _CHANGES) + "\n\n"
            f"## Query it\n\n{snippets}\n\n"
            "## Tables\n\n| table | rows | files | size | license | description |\n|---|---|---|---|---|---|\n"
            f"{rows}\n\nViews: " + ", ".join(f"`{v}`" for v in m["views"]) + ".\n")


def _page(m: dict, title: str, studies: list[dict], study: str, croissant_json: str) -> str:
    stats = [(f"{m['samples']:,}", "samples"), (f"{len(studies):,}", "studies"),
             (escape(m["release"]), "release"), (f"{len(m['tables'])}", "tables")]
    files = [[f'<code>{escape(t["table"])}</code><br><span class="sha">{escape(t["description"])}</span>',
              f"{t['row_count']:,}", f"{len(t['files'])}", site._size(t["bytes"]),
              f'<span class="pill">{escape(t["license"])}</span>'] for t in m["tables"]]
    extra = [[f'<a href="{n}"><code>{n}</code></a>', "", "", "", ""]
             for n in ("manifest.json", "croissant.json", "provenance.json", publish.VIEWS_DB,
                       "views.sql", "README.md", "studies/index.json")]
    study_rows = [[escape(s["study_name"]), f"{s['n_samples']:,}",
                   " &middot; ".join(f'<a href="{escape(f["path"])}">{escape(f["name"])}</a>'
                                     for f in s["files"])] for s in studies]
    body = (
        f'<header class="top"><div class="crumbs"><a href="../../index.html">cmgd-public</a> / '
        f'{escape(m["dataset"])} / {escape(m["release"])}</div><h1>{escape(m["dataset"])} '
        f'<span class="pill">{escape(m["release"])}</span></h1>'
        f'<p class="lede">{escape(m["description"])}</p></header>'
        '<div class="note">Preview of the new release layout. It may change or be removed; the '
        "stable dataset and its <code>latest.json</code> are unchanged.</div>"
        '<div class="stats">' + "".join(f'<div class="stat"><b>{v}</b><span>{k}</span></div>'
                                        for v, k in stats) + "</div>"
        "<h2>What changed</h2><ul>" + "".join(
            "<li>" + re.sub(r"`([^`]+)`", r"<code>\\1</code>", escape(c)) + "</li>"
            for c in _CHANGES) + "</ul>"
        "<h2>Query it over HTTPS</h2>" + "".join(site._pre(s) for s in _snippets(m["base_url"], study))
        + "<h2>Tables and files</h2>"
        + site._table(["table", "rows", "files", "size", "license"], files + extra, {1, 2, 3})
        + "<h2>Studies</h2>" + site._table(["study", "samples", "downloads"], study_rows, {1})
        + '<script type="application/ld+json">' + croissant_json.replace("</", "<\\/") + "</script>")
    return site.PAGE.substitute(
        title=escape(f"{m['dataset']} {m['release']} · curatedMetagenomicData"),
        description=escape(f"curatedMetagenomicData preview release {m['dataset']} {m['release']}"),
        body=body, root="../..")


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def release(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str, root: Path | str, *,
            jobs: Sequence[CompletedJob] = (), registration: Registration | None = None,
            public_base_url: str = publish.PUBLIC_BASE_URL, today: date | None = None,
            **build_args) -> dict:
    """Build one preview release of a registration into the local store ``root``, as
    dataset ``<workflow_id>-<version>-preview``; returns its manifest. The release is
    staged next to the store and moved in whole, then its own ``releases.json`` and
    ``latest.json`` are updated. The store's root ``index.json`` and site are not:
    the preview is reached by its URL."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    did = f"{publish.dataset_id(workflow_id, version)}-{PREVIEW}"
    dataset_dir = root / did
    rel = publish._claim_release_id(dataset_dir, did, today or datetime.now(UTC).date())
    url = f"{public_base_url.rstrip('/')}/{did}/{rel}"
    title = f"curatedMetagenomicData: {workflow_id} {version} (preview layout)"
    with tempfile.TemporaryDirectory(dir=root, prefix=".staging-") as tmp:
        bundle = Path(tmp) / rel
        m = build(con, workflow_id, version, bundle, jobs=jobs, base_url=url, **build_args)
        m.update(
            dataset=did, release=rel, release_date=rel.partition(".")[0],
            published_at=datetime.now(UTC).isoformat(timespec="seconds"), git_sha=_git_sha(),
            description=(f"Profiles from the {workflow_id} {version} registration of "
                         "curatedMetagenomicsNextflow in the bucketed layout: multi-file Parquet "
                         "sorted by sample accession, study membership in its own table."))
        db = duckdb.connect()  # the release's views over the local files, for the extras
        db.execute(views_sql(m["tables"], str(bundle)))
        studies = _studies(db, bundle, did)
        ran = db.sql(f"SELECT pipeline_version, git_commit, count(*) FROM {HUB} "
                     "GROUP BY ALL ORDER BY ALL").fetchall()
        # an example study for the snippets: mid-sized, so the queries return something
        study = sorted(studies, key=lambda s: abs(s["n_samples"] - 200))[0]["study_name"] if studies else "…"
        db.close()
        sources = []
        if publish._has_meta(con, "source_releases"):
            r = con.sql("SELECT * REPLACE (loaded_at::VARCHAR AS loaded_at) FROM "
                        f"lake.{publish.META_SCHEMA}.source_releases WHERE pinned ORDER BY ALL")
            sources = [dict(zip(r.columns, row)) for row in r.fetchall()]
        publish._write_json(bundle / "provenance.json", {"cmgd": {
            "registration": {"workflow_id": workflow_id, "version": version,
                             "revision": registration.revision if registration else None,
                             "repository_url": registration.repository_url if registration else None,
                             "params": registration.params if registration else None},
            "lake_snapshot": m["lake_snapshot"],
            "pipeline_runs": [{"pipeline_version": v, "git_commit": c, "n_samples": n} for v, c, n in ran],
            "databases": DATABASES.get((workflow_id, version)),
            "metadata_sources": sources,
            "licenses": {t["table"]: t["license"] for t in m["tables"]},
            "citation": publish.CITATION}})
        publish._write_json(bundle / "manifest.json", m)
        with (bundle / "manifest.json").open("rb") as f:
            doc = croissant_doc({**m, "manifest_sha256_of": hashlib.file_digest(f, "sha256").hexdigest()},
                                title, m["description"])
        publish._write_json(bundle / "croissant.json", doc)
        (bundle / "README.md").write_text(_readme(m, title, study))
        (bundle / "index.html").write_text(
            _page(m, title, studies, study, (bundle / "croissant.json").read_text()))
        dataset_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(bundle), str(dataset_dir / rel))
    publish.promote_release(dataset_dir, did, rel, m["published_at"])
    return m


def sync_commands(root: Path | str, workflow_id: str, version: str,
                  remote: str = publish.SYNC_REMOTE) -> list[list[str]]:
    """The preview dataset's upload: release directories, then its two pointers. Not
    the store's root files, which do not list the preview."""
    did = f"{publish.dataset_id(workflow_id, version)}-{PREVIEW}"
    return publish.sync_commands(root, did, remote)[:3]
