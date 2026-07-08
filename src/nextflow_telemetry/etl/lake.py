"""DuckLake catalog: connection, schema, and writes.

Runs DuckDB in-process (on onclappc02). It's a **DuckLake** — a tracked catalog
plus DuckLake-managed parquet, not a loose parquet dump. Two catalog backends:

- **Postgres** (design default for the internal working lake) — set
  ``ETL_LAKE_CATALOG_PG_DB`` (e.g. ``cmgd_lake``); the connection is derived from
  ``SQLALCHEMY_URI`` (same telemetry instance, separate database), which is what
  lets internal consumers read concurrently while the ETL writes. Provisioning the
  ``cmgd_lake`` database is a one-time privileged step (the app role lacks
  ``CREATEDB``): ``CREATE DATABASE cmgd_lake; GRANT ALL ... TO nf_telemetry;``.
- **DuckDB file** (``ETL_LAKE_CATALOG``) — single-writer fallback for dev/tests.

Data (parquet) lands at ``ETL_LAKE_DATA_PATH`` — a local dir for dev/tests, or
``s3://cmgd-data/lake/`` (Cloudflare R2) in prod. When the data path is ``s3://``,
R2 credentials are read from the rclone ``[r2]`` config (never printed) and
installed as a DuckDB secret. Partitioning (workflow/version/method/data_type) is
a follow-up; correctness of ingest doesn't depend on it.
"""
from __future__ import annotations

import configparser
import os
import pathlib
import re

import duckdb

CATALOG = os.environ.get("ETL_LAKE_CATALOG", "/data/cmgd/lake/cmgd_lake.ducklake")
CATALOG_PG_DB = os.environ.get("ETL_LAKE_CATALOG_PG_DB")  # set → Postgres catalog
DATA_PATH = os.environ.get("ETL_LAKE_DATA_PATH", "/data/cmgd/lake/data")


def _pg_catalog_dsn(db: str) -> str:
    """DuckLake Postgres-catalog DSN on the telemetry instance (same host/creds as
    SQLALCHEMY_URI, different database)."""
    uri = re.sub(r"\+\w+", "", os.environ["SQLALCHEMY_URI"])
    m = re.match(r"postgresql://([^:]*):([^@]*)@([^:/]+):(\d+)/", uri)
    if not m:
        raise RuntimeError("cannot derive Postgres catalog DSN from SQLALCHEMY_URI")
    user, pw, host, port = m.groups()
    return f"postgres:dbname={db} host={host} port={port} user={user} password={pw}"

_ID = {"sample_id": "VARCHAR", "study_name": "VARCHAR", "run_ids": "VARCHAR",
       "workflow": "VARCHAR", "version": "VARCHAR"}
_BRANCH = {"data_type": "VARCHAR"}

SCHEMAS: dict[str, dict[str, str]] = {
    # Separate per-method profiles — one value interpretation per table.
    "taxonomic_profile_metaphlan": {**_ID, **_BRANCH, "clade_name": "VARCHAR", "rank": "VARCHAR",
                          "ncbi_taxid": "INTEGER", "sgb_id": "VARCHAR",
                          "relative_abundance": "DOUBLE",  # metaphlan percent (native)
                          "coverage": "DOUBLE", "estimated_reads": "BIGINT"},
    "taxonomic_profile_bracken": {**_ID, **_BRANCH, "clade_name": "VARCHAR", "rank": "VARCHAR",
                          "ncbi_taxid": "INTEGER",
                          "fraction_total_reads": "DOUBLE",  # bracken read-count fraction (native)
                          "estimated_reads": "BIGINT"},
    "resistome": {**_ID, **_BRANCH, "gene": "VARCHAR", "template_coverage": "DOUBLE",
                  "template_identity": "DOUBLE", "depth": "DOUBLE", "score": "DOUBLE"},
    "qc_metrics": {**_ID, "reads_raw": "BIGINT", "reads_decontaminated": "BIGINT",
                   "bases_raw": "BIGINT", "bases_decontaminated": "BIGINT",
                   "reads_surviving_fraction": "DOUBLE", "bases_surviving_fraction": "DOUBLE",
                   "metaphlan_index": "VARCHAR", "pipeline_version": "VARCHAR", "git_commit": "VARCHAR"},
    "marker_abundance": {**_ID, **_BRANCH, "marker_name": "VARCHAR", "value": "DOUBLE"},
    "marker_presence": {**_ID, **_BRANCH, "marker_name": "VARCHAR"},
}


def _r2_secret_sql() -> str | None:
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.config/rclone/rclone.conf"))
    if "r2" not in cfg:
        return None
    r = cfg["r2"]
    key, secret, endpoint = r.get("access_key_id"), r.get("secret_access_key"), r.get("endpoint")
    if not (key and secret and endpoint):
        return None
    ep = endpoint.replace("https://", "").replace("http://", "")
    return (f"CREATE OR REPLACE SECRET r2 (TYPE s3, KEY_ID '{key}', SECRET '{secret}', "
            f"ENDPOINT '{ep}', URL_STYLE 'path', REGION 'auto')")


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake; INSTALL httpfs; LOAD httpfs;")
    if CATALOG_PG_DB:
        con.execute("INSTALL postgres; LOAD postgres;")
        target = _pg_catalog_dsn(CATALOG_PG_DB)
    else:
        pathlib.Path(CATALOG).parent.mkdir(parents=True, exist_ok=True)
        target = CATALOG
    if DATA_PATH.startswith("s3://"):
        sql = _r2_secret_sql()
        if not sql:
            raise RuntimeError("ETL_LAKE_DATA_PATH is s3:// but no rclone [r2] credentials found")
        con.execute(sql)
    else:
        pathlib.Path(DATA_PATH).mkdir(parents=True, exist_ok=True)
    ro = ", READ_ONLY" if read_only else ""
    con.execute(f"ATTACH 'ducklake:{target}' AS lake (DATA_PATH '{DATA_PATH}'{ro})")
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    for table, cols in SCHEMAS.items():
        coldefs = ", ".join(f"{c} {t}" for c, t in cols.items())
        con.execute(f"CREATE TABLE IF NOT EXISTS lake.{table} ({coldefs})")


def replace_sample(con: duckdb.DuckDBPyConnection, table: str, sample_id: str,
                   workflow: str, version: str, rows: list[dict]) -> int:
    """Idempotent write: delete this sample's rows *for this (workflow, version)*,
    then insert. Scoping the delete by the full key means re-ingesting one version
    never touches another version's rows for the same sample. Explicit column list
    so inserts don't depend on physical column order. Returns rows written; caller
    wraps a whole sample's tables in one transaction."""
    cols = list(SCHEMAS[table])
    con.execute(
        f"DELETE FROM lake.{table} WHERE sample_id = ? AND workflow = ? AND version = ?",
        [sample_id, workflow, version],
    )
    if not rows:
        return 0
    collist = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    con.executemany(
        f"INSERT INTO lake.{table} ({collist}) VALUES ({placeholders})",
        [[r.get(c) for c in cols] for r in rows],
    )
    return len(rows)
