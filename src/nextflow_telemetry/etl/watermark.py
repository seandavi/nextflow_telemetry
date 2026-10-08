"""The ``etl_ingested`` watermark in the catalog Postgres (``SQLALCHEMY_URI``).

Pending = a registration's completed v2 jobs (``v2.completed_jobs``) minus the
keys recorded here — restart- and re-run-safe, and the backlog count that drives
the tick trigger. The ``sample_id`` column holds the job's sample key (md5 for
2.2.x, a readset id for RS-keyed registrations); the table is unchanged from
its migration, so no schema move was needed.
"""
from __future__ import annotations

import json
import os
import re

import asyncpg  # type: ignore[import-untyped]


def _uri() -> str:
    return re.sub(r"\+asyncpg", "", os.environ["SQLALCHEMY_URI"])


async def connect() -> asyncpg.Connection:
    return await asyncpg.connect(_uri())


async def ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etl_ingested (
            sample_id        text NOT NULL,
            workflow_id      text NOT NULL,
            workflow_version text NOT NULL,
            ingested_at      timestamptz NOT NULL DEFAULT now(),
            row_counts       jsonb,
            PRIMARY KEY (sample_id, workflow_id, workflow_version)
        )
        """
    )


async def ingested_keys(conn: asyncpg.Connection, workflow_id: str, version: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT sample_id FROM etl_ingested WHERE workflow_id = $1 AND workflow_version = $2",
        workflow_id, version)
    return {r["sample_id"] for r in rows}


async def mark_ingested(conn: asyncpg.Connection, sample_key: str, workflow_id: str,
                        version: str, row_counts: dict) -> None:
    await conn.execute(
        """
        INSERT INTO etl_ingested (sample_id, workflow_id, workflow_version, row_counts)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sample_id, workflow_id, workflow_version)
        DO UPDATE SET ingested_at = now(), row_counts = EXCLUDED.row_counts
        """,
        sample_key, workflow_id, version, json.dumps(row_counts),
    )
