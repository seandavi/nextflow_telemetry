"""Bucketed, sorted Parquet for one registration's release tables.

The mechanics of the multi-file release layout, built into a local directory.
Not wired into ``publish`` yet: no pages, per-study downloads or pointers, and
``cdsci.publish`` still writes one file per table.

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

  <out>/manifest.json, views.sql, cmgd.duckdb
  <out>/tables/<table>/data/part-00000.parquet, part-00001.parquet, ...
"""
from __future__ import annotations

import hashlib
import json
import resource
import shutil
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import duckdb
from cdsci.publish import build_db, sql_literal  # type: ignore[import-untyped]

from . import lake, publish
from .v2 import CompletedJob

BUCKET_SAMPLES = 500        # samples sorted together: ~15M marker rows, sorts in memory
TARGET_BYTES = 700_000_000  # per file; the asked-for range is 500 MB - 1 GB
KEY = "sample_accession"
HUB, STUDIES = "samples", "sample_studies"
# On the hub, not on every fact row: free on disk (RLE) but ~500 B/row in a sort.
_HUB_ONLY = ("sample_id", "readset_id", "study_name", "run_ids", "workflow_id", "version")


def fact_columns(table: str) -> list[str]:
    return [KEY] + [c for c in lake.SCHEMAS[table] if c not in _HUB_ONLY]


def sort_by(table: str) -> list[str]:
    """Accession first (the bucket order), then the table's primary key."""
    return [KEY, "sample_key"] + [c for c in publish._TABLES[table][2] if c != "sample_key"]


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
         jobs: Sequence[CompletedJob], bucket_samples: int) -> None:
    """``bucket_hub``: the samples table plus each sample's bucket."""
    _load(con, "bucket_jobs", ("sample_key", "run_accessions", "completed_at"),
          [{"sample_key": j.sample_key, "run_accessions": j.run_accessions,
            "completed_at": j.completed_at} for j in jobs])
    # ponytail: every collection is published as a study; filter on collection type
    # once v2 job items carry it (ad hoc collections and bioproject aliases are in here).
    _load(con, "bucket_members", ("study_name", "sample_key"),
          [{"study_name": c, "sample_key": j.sample_key} for j in jobs for c in j.collections])
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


def _range(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[str | None, str | None]:
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


def views_sql(tables: list[dict], base_url: str) -> str:
    """One view per table over all of its files."""
    stmts = []
    for t in tables:
        urls = ", ".join(sql_literal(f"{base_url}/{f['path']}") for f in t["files"])
        stmts.append(f"CREATE OR REPLACE VIEW main.{t['table']} AS SELECT * FROM read_parquet([{urls}])")
    return ";\n".join(stmts) + ";\n"


def build(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str, out: Path | str, *,
          jobs: Sequence[CompletedJob] = (), snapshot: int | None = None,
          base_url: str | None = None, bucket_samples: int = BUCKET_SAMPLES,
          target_bytes: int = TARGET_BYTES, memory_limit: str | None = "32GB",
          threads: int | None = 16) -> dict:
    """Build the bucketed tables of one registration into ``out`` (new or empty);
    returns the manifest.

    ``con`` has the shared lake attached as ``lake``. ``jobs`` are the registration's
    completed v2 jobs: run accessions, completion times and collections (the study
    memberships). ``base_url`` is where ``out`` will be served from (default: the
    local directory, so ``cmgd.duckdb`` works in place). ``memory_limit`` and
    ``threads`` are set on ``con``: DuckDB's own default is 80% of RAM."""
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
    _hub(con, sid, workflow_id, version, jobs, bucket_samples)

    tables, seconds = [], {}

    def add(table: str, order: list[str], files: list[dict], t0: float) -> None:
        tables.append({"table": table, "sort_by": order, "row_count": sum(f["row_count"] for f in files),
                       "bytes": sum(f["bytes"] for f in files), "files": files})
        seconds[table] = round(time.monotonic() - t0, 1)
        print(f"{table:32} {tables[-1]['row_count']:>15,} rows {len(files):>4} files "
              f"{tables[-1]['bytes'] / 1e6:>10,.1f} MB {seconds[table]:>8.1f} s", flush=True)

    for table, select in (
            (HUB, f"SELECT * EXCLUDE (bucket) FROM bucket_hub ORDER BY {KEY}, sample_key"),
            (STUDIES, f"SELECT m.study_name, h.{KEY}, h.sample_key FROM (SELECT DISTINCT * FROM "
                      f"bucket_members) m JOIN bucket_hub h USING (sample_key) ORDER BY ALL")):
        t0 = time.monotonic()
        path = out / "tables" / table / "data" / "part-00000.parquet"
        n = _copy(con, select, path)
        add(table, [KEY, "sample_key"] if table == HUB else ["study_name", KEY, "sample_key"],
            [_file(out, path, n, *_range(con, path))], t0)
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
        "dataset": publish.dataset_id(workflow_id, version), "base_url": base_url,
        "lake_snapshot": sid, "bucket_samples": bucket_samples, "target_bytes": target_bytes,
        "samples": tables[0]["row_count"], "tables": tables,
        "build": {"seconds": round(time.monotonic() - start, 1), "table_seconds": seconds,
                  "memory_limit": memory_limit, "threads": threads,
                  # Linux reports ru_maxrss in KB
                  "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
