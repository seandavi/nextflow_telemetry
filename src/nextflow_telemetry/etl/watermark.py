"""Telemetry-side state: which completed samples still need ingesting, and the
``etl_ingested`` watermark. Reads pending as ``completed jobs`` anti-joined
against ``etl_ingested`` — restart- and re-run-safe, and the source of the
backlog count that drives the tick trigger.
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


async def pending(conn: asyncpg.Connection, workflow: str, version: str,
                  limit: int | None = None) -> list[str]:
    q = """
        SELECT j.sample_id
        FROM jobs j
        WHERE j.status = 'completed' AND j.workflow_id = $1 AND j.workflow_version = $2
          AND NOT EXISTS (
              SELECT 1 FROM etl_ingested e
              WHERE e.sample_id = j.sample_id AND e.workflow_id = j.workflow_id
                AND e.workflow_version = j.workflow_version)
        ORDER BY j.completed_at
    """
    args: list = [workflow, version]
    if limit is not None:
        q += " LIMIT $3"
        args.append(limit)
    return [r["sample_id"] for r in await conn.fetch(q, *args)]


async def backlog_count(conn: asyncpg.Connection, workflow: str, version: str) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM jobs j
        WHERE j.status = 'completed' AND j.workflow_id = $1 AND j.workflow_version = $2
          AND NOT EXISTS (SELECT 1 FROM etl_ingested e
              WHERE e.sample_id = j.sample_id AND e.workflow_id = j.workflow_id
                AND e.workflow_version = j.workflow_version)
        """,
        workflow, version,
    )


async def study_map(conn: asyncpg.Connection, sample_ids: list[str]) -> dict[str, str]:
    """sample_id → a study label. Prefers a study-type collection; falls back to
    any collection's label/id. (Sample↔study is many-to-many; one is picked.)"""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (cs.sample_id) cs.sample_id, coalesce(c.label, c.collection_id) AS study
        FROM collection_samples cs JOIN collections c ON c.collection_id = cs.collection_id
        WHERE cs.sample_id = ANY($1::text[])
        ORDER BY cs.sample_id, (c.type = 'study') DESC NULLS LAST
        """,
        sample_ids,
    )
    return {r["sample_id"]: r["study"] for r in rows}


async def mark_ingested(conn: asyncpg.Connection, sample_id: str, workflow: str,
                        version: str, row_counts: dict) -> None:
    await conn.execute(
        """
        INSERT INTO etl_ingested (sample_id, workflow_id, workflow_version, row_counts)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sample_id, workflow_id, workflow_version)
        DO UPDATE SET ingested_at = now(), row_counts = EXCLUDED.row_counts
        """,
        sample_id, workflow, version, json.dumps(row_counts),
    )
