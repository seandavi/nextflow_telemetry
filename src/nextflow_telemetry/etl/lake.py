"""nf-etl's tables in the shared cdsci DuckLake, written through ``cdsci.lake``.

nf-etl is a cdsci-lake producer (cdsci-lake ADR-0011): ``lake_connect`` opens the
lake and its ``ops`` ledger, the tables live in schema ``cmgd``
(``lake.cmgd.<table>``), and every batch is an ``ops.run`` whose writes are one
attributed snapshot (``author = nextflow_telemetry:cmgd``).

The backend comes from cdsci-lake's settings (``CU_OPENALEX_*`` env vars, see
docs/etl-runbook.md): ``local`` (DuckDB-file catalog + local parquet) for dev and
tests, ``postgres`` (catalog DB ``lake``, data ``r2://cdsci-lake/``, credentials
from GCP Secret Manager) in production.

Writes are delete+insert per batch, not the platform's ``upsert``: a sample's
outputs for a registration are immutable once published, so a batch is a pure
append, and the delete only runs for keys being re-ingested. A keyed MERGE would
join every batch against the whole table (markers included) for no change.
"""
from __future__ import annotations

import json
import tempfile

import duckdb
from cdsci.lake import Settings, get_settings, lake_connect, ops  # type: ignore[import-untyped]

WRITER = "nextflow_telemetry"
SCHEMA = "cmgd"
SOURCE = ops.Source(
    name="cmgd", lake_schema=SCHEMA,
    description="curatedMetagenomicData pipeline outputs (MetaPhlAn, Bracken, resistome, "
                "QC, markers, HUMAnN pathways) per v2 registration; gene families indexed",
    cadence="15-min tick", distribution="cmgd-raw", license="CC0-1.0",
)

# Common columns. sample_key is the id the registration published under (its
# output folder name); sample_id is the md5 content address (None for RS-keyed
# registrations, ADR-0007) and readset_id the RS. id once known.
_ID = {"sample_key": "VARCHAR", "sample_id": "VARCHAR", "readset_id": "VARCHAR",
       "study_name": "VARCHAR", "run_ids": "VARCHAR",
       "workflow_id": "VARCHAR", "version": "VARCHAR"}
_BRANCH = {"data_type": "VARCHAR"}
_HUMANN = {**_ID, **_BRANCH, "humann_bundle": "VARCHAR"}

SCHEMAS: dict[str, dict[str, str]] = {
    # Separate per-method profiles — one value interpretation per table.
    # metaphlan_profile names the MetaPhlAn pass (pipeline ADR-0018): the main
    # pass, or a HUMAnN bundle's own pass (then humann_bundle is set).
    "taxonomic_profile_metaphlan": {**_ID, **_BRANCH, "metaphlan_profile": "VARCHAR",
                          "humann_bundle": "VARCHAR",
                          "clade_name": "VARCHAR", "rank": "VARCHAR",
                          "ncbi_taxid": "INTEGER", "sgb_id": "VARCHAR",
                          "relative_abundance": "DOUBLE",  # metaphlan percent (native)
                          "coverage": "DOUBLE", "estimated_reads": "BIGINT"},
    "taxonomic_profile_bracken": {**_ID, **_BRANCH, "clade_name": "VARCHAR", "rank": "VARCHAR",
                          "ncbi_taxid": "INTEGER",
                          "fraction_total_reads": "DOUBLE",  # bracken read-count fraction (native)
                          "estimated_reads": "BIGINT"},
    "resistome": {**_ID, **_BRANCH, "gene": "VARCHAR", "template_coverage": "DOUBLE",
                  "template_identity": "DOUBLE", "depth": "DOUBLE", "score": "DOUBLE"},
    # One row per ingested sample; also the done-set (see ingested_keys).
    "qc_metrics": {**_ID, "reads_raw": "BIGINT", "reads_decontaminated": "BIGINT",
                   "bases_raw": "BIGINT", "bases_decontaminated": "BIGINT",
                   "reads_surviving_fraction": "DOUBLE", "bases_surviving_fraction": "DOUBLE",
                   "metaphlan_index": "VARCHAR", "metaphlan_profile": "VARCHAR",
                   "humann_bundle": "VARCHAR",
                   "pipeline_version": "VARCHAR", "git_commit": "VARCHAR"},
    "marker_abundance": {**_ID, **_BRANCH, "marker_name": "VARCHAR", "value": "DOUBLE"},
    "marker_presence": {**_ID, **_BRANCH, "marker_name": "VARCHAR"},
    # HUMAnN, unnormalized, stratum None = community total.
    "humann_pathabundance": {**_HUMANN, "pathway": "VARCHAR", "stratum": "VARCHAR",
                             "abundance": "DOUBLE"},
    "humann_pathcoverage": {**_HUMANN, "pathway": "VARCHAR", "stratum": "VARCHAR",
                            "coverage": "DOUBLE"},
    # Gene families stay in cmgd-raw as per-sample downloads (#228); this is
    # their index (#229): key is the object key in the bucket, size (bytes) and
    # sha256 (hex) the object's, rows its parsed row count (what the file would
    # have been in the lake).
    "humann_genefamilies_files": {"sample_key": "VARCHAR", "readset_id": "VARCHAR",
                                  "workflow_id": "VARCHAR", "version": "VARCHAR",
                                  "humann_bundle": "VARCHAR", "branch": "VARCHAR",
                                  "key": "VARCHAR", "size": "BIGINT", "sha256": "VARCHAR",
                                  "rows": "BIGINT"},
}

# Sort within files by (study, sample, feature) so a study/sample subset is a
# range read (docs/research/results-storage-and-access.md). DuckLake sorts on
# insert; a batch is one insert per table, so files are batch-sized.
_FEATURE = {
    "taxonomic_profile_metaphlan": "clade_name", "taxonomic_profile_bracken": "clade_name",
    "resistome": "gene", "marker_abundance": "marker_name", "marker_presence": "marker_name",
    "humann_pathabundance": "pathway", "humann_pathcoverage": "pathway",
}


def connect(settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """Write-mode lake + ops ledger, with nf-etl's source registered and tables ensured."""
    con = lake_connect(settings or get_settings())
    ops.register_sources(con, writer=WRITER, sources=(SOURCE,))
    ensure_schema(con)
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{SCHEMA}")
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = ?",
        [SCHEMA]).fetchall()}
    for table, cols in SCHEMAS.items():
        if table in existing:
            continue
        coldefs = ", ".join(f"{c} {t}" for c, t in cols.items())
        con.execute(f"CREATE TABLE lake.{SCHEMA}.{table} ({coldefs})")
        sort = ", ".join(c for c in ("study_name", "sample_key", _FEATURE.get(table)) if c in cols)
        con.execute(f"ALTER TABLE lake.{SCHEMA}.{table} SET SORTED BY ({sort})")


def ingested_keys(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str) -> set[str]:
    """Sample keys already in the lake for a registration. qc_metrics has exactly
    one row per ingested sample and commits in the same snapshot as the rest of
    its batch, so the lake is its own done-set (an ops watermark is one cursor,
    not a set of keys — cdsci-lake ADR-0011 §3)."""
    return {r[0] for r in con.execute(
        f"SELECT sample_key FROM lake.{SCHEMA}.qc_metrics WHERE workflow_id = ? AND version = ?",
        [workflow_id, version]).fetchall()}


def stage(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> None:
    """Append rows to a temp staging table (spills to disk, unlike Python lists).

    Rows go in as one NDJSON file read by DuckDB: binding Python lists as query
    parameters converts them value by value (~1 s per 1k rows)."""
    cols = SCHEMAS[table]
    coldefs = ", ".join(f"{c} {t}" for c, t in cols.items())
    con.execute(f"CREATE TEMP TABLE IF NOT EXISTS stage_{table} ({coldefs})")
    if not rows:
        return
    types = ", ".join(f"'{c}': '{t}'" for c, t in cols.items())
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson") as f:
        f.writelines(json.dumps({c: r.get(c) for c in cols}) + "\n" for r in rows)
        f.flush()
        con.execute(f"INSERT INTO stage_{table} SELECT {', '.join(cols)} FROM read_json("
                    f"'{f.name}', format = 'newline_delimited', columns = {{{types}}})")


def write_batch(con: duckdb.DuckDBPyConnection, run: ops.Run, workflow_id: str, version: str,
                replace_keys: list[str]) -> dict[str, int]:
    """Move every staged table into the lake in one attributed snapshot.

    ``replace_keys`` (samples already in the lake) are deleted first, scoped to
    this registration, so a re-ingest replaces rather than duplicates and never
    touches another registration's rows for the same sample."""
    counts: dict[str, int] = {}
    for table in SCHEMAS:
        stage(con, table, [])
    try:
        with run.attribute(f"{workflow_id}/{version}"):
            for table, cols in SCHEMAS.items():
                if replace_keys:
                    con.execute(
                        f"DELETE FROM lake.{SCHEMA}.{table} WHERE workflow_id = ? AND version = ? "
                        "AND sample_key IN (SELECT unnest(?::VARCHAR[]))",
                        [workflow_id, version, replace_keys])
                collist = ", ".join(cols)
                n = con.execute(f"INSERT INTO lake.{SCHEMA}.{table} ({collist}) "
                                f"SELECT {collist} FROM stage_{table}").fetchall()[0][0]
                if n:
                    counts[table] = n
    finally:
        drop_stage(con)
    return counts


def drop_stage(con: duckdb.DuckDBPyConnection) -> None:
    for table in SCHEMAS:
        con.execute(f"DROP TABLE IF EXISTS stage_{table}")


_EXTRAPOLATE = (200_000, 400_000)


def _size(n: float | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1000
    return f"{n:.1f} EB"


def volumes(con: duckdb.DuckDBPyConnection) -> str:
    """Measured volumes as markdown (docs/data-volumes.md).

    bytes/row comes from the DuckLake file stats (live parquet files' bytes over
    their record counts — inlined rows have no file yet and are left out of it);
    rows/sample from the table itself, per registration. Gene families come from
    their files index: the cmgd-raw objects' bytes (gzipped TSV) and parsed rows."""
    files = dict((t, (b, r)) for t, b, r in con.execute(
        """SELECT t.table_name, sum(f.file_size_bytes), sum(f.record_count)
           FROM __ducklake_metadata_lake.ducklake_data_file f
           JOIN __ducklake_metadata_lake.ducklake_table t USING (table_id)
           JOIN __ducklake_metadata_lake.ducklake_schema s USING (schema_id)
           WHERE f.end_snapshot IS NULL AND t.end_snapshot IS NULL AND s.end_snapshot IS NULL
             AND s.schema_name = ?
           GROUP BY 1""", [SCHEMA]).fetchall())
    head = ["table", "registration", "samples", "rows/sample", "bytes/row", "bytes/sample",
            *(f"@{n // 1000}k samples" for n in _EXTRAPOLATE)]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    totals: dict[str, float] = {}
    for table in SCHEMAS:
        gf = table == "humann_genefamilies_files"
        measure = "sum(rows), sum(size)" if gf else "count(*), NULL"
        for wf, v, samples, rows, gf_bytes in con.execute(
                f"SELECT workflow_id, version, count(DISTINCT sample_key), {measure} "
                f"FROM lake.{SCHEMA}.{table} GROUP BY ALL ORDER BY ALL").fetchall():
            reg, rps = f"{wf} {v}", rows / samples
            if gf:  # not in the lake: sizes are the gzipped files in cmgd-raw
                label, per_row, per_sample = ("gene families (cmgd-raw files)",
                                              gf_bytes / rows if rows else None, gf_bytes / samples)
            else:
                fb, fr = files.get(table, (None, None))
                label, per_row = table, (fb / fr if fr else None)
                per_sample = rps * per_row if per_row is not None else None
                if per_sample is not None:
                    totals[reg] = totals.get(reg, 0.0) + per_sample
            out.append(f"| {label} | {reg} | {samples} | {rps:,.1f} | {_size(per_row)} | "
                       f"{_size(per_sample)} | "
                       + " | ".join(_size(per_sample * n if per_sample is not None else None)
                                    for n in _EXTRAPOLATE) + " |")
    for reg, t in sorted(totals.items()):
        out.append(f"| **lake total** | {reg} | | | | {_size(t)} | "
                   + " | ".join(_size(t * n) for n in _EXTRAPOLATE) + " |")
    return "\n".join(out)
