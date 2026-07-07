"""DuckLake catalog: connection, schema, and writes.

Runs DuckDB in-process (on onclappc02). The catalog is a DuckDB file
(``ETL_LAKE_CATALOG``); data (parquet) lands at ``ETL_LAKE_DATA_PATH`` — a local
dir for dev/tests, or ``s3://cmgd-data/lake/`` (Cloudflare R2) in prod. When the
data path is ``s3://``, R2 credentials are read from the rclone ``[r2]`` config
(never printed) and installed as a DuckDB secret.

ponytail: DuckDB-file catalog (single-writer ETL). Upgrade path — a Postgres
catalog — is what enables concurrent internal readers; swap the ATTACH target
when that's needed. Partitioning (workflow/version/method/data_type) is a
follow-up; correctness of ingest doesn't depend on it.
"""
from __future__ import annotations

import configparser
import os
import pathlib

import duckdb

CATALOG = os.environ.get("ETL_LAKE_CATALOG", "/data/cmgd/lake/cmgd_lake.ducklake")
DATA_PATH = os.environ.get("ETL_LAKE_DATA_PATH", "/data/cmgd/lake/data")

_ID = {"sample_id": "VARCHAR", "study_name": "VARCHAR", "run_ids": "VARCHAR",
       "workflow": "VARCHAR", "version": "VARCHAR"}
_BRANCH = {"data_type": "VARCHAR"}

SCHEMAS: dict[str, dict[str, str]] = {
    "taxonomic_profile": {**_ID, **_BRANCH, "method": "VARCHAR", "clade_name": "VARCHAR",
                          "rank": "VARCHAR", "ncbi_taxid": "INTEGER", "sgb_id": "VARCHAR",
                          "relative_abundance": "DOUBLE"},
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
    if DATA_PATH.startswith("s3://"):
        sql = _r2_secret_sql()
        if not sql:
            raise RuntimeError("ETL_LAKE_DATA_PATH is s3:// but no rclone [r2] credentials found")
        con.execute(sql)
    else:
        pathlib.Path(DATA_PATH).mkdir(parents=True, exist_ok=True)
        pathlib.Path(CATALOG).parent.mkdir(parents=True, exist_ok=True)
    ro = ", READ_ONLY" if read_only else ""
    con.execute(f"ATTACH 'ducklake:{CATALOG}' AS lake (DATA_PATH '{DATA_PATH}'{ro})")
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    for table, cols in SCHEMAS.items():
        coldefs = ", ".join(f"{c} {t}" for c, t in cols.items())
        con.execute(f"CREATE TABLE IF NOT EXISTS lake.{table} ({coldefs})")


def replace_sample(con: duckdb.DuckDBPyConnection, table: str, sample_id: str,
                   rows: list[dict]) -> int:
    """Idempotent write: delete this sample's existing rows, then insert. Returns
    rows written. Caller wraps a whole sample's tables in one transaction."""
    cols = list(SCHEMAS[table])
    con.execute(f"DELETE FROM lake.{table} WHERE sample_id = ?", [sample_id])
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in cols)
    con.executemany(
        f"INSERT INTO lake.{table} VALUES ({placeholders})",
        [[r.get(c) for c in cols] for r in rows],
    )
    return len(rows)
