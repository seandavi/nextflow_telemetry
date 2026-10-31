"""Retired workflow versions' jobs archived to R2 (v2's ``archiveRetiredJobs``, #374).

Each chunk of jobs, with its dead-letter rows, is written as gzipped NDJSON and only
then deleted. R2 writes go through DuckDB ``COPY ... (FORMAT BLOB)``, with the same R2
secret pattern as ``event_archive.py``.
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import orjson
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..control_db import dead_letter_tbl, jobs_tbl, workflows_tbl
from ..log import logger
from .deadlines import JOB_INFLIGHT

ARCHIVE_CHUNK = 5_000      # rows per object, as v2
ARCHIVE_HOUR_UTC = 6       # v2's cron "0 6 * * *"


@dataclass
class ArchiveResult:
    workflows_archived: int = 0
    jobs_archived: int = 0


def seconds_until_archive(now: datetime) -> float:
    """Seconds from `now` to the next ARCHIVE_HOUR_UTC:00 UTC strictly after it."""
    target = now.astimezone(timezone.utc).replace(
        hour=ARCHIVE_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class RetiredJobArchive:
    def __init__(
        self,
        engine: AsyncEngine,
        url: str,
        r2_account_id: str = "",
        r2_access_key_id: str = "",
        r2_secret_access_key: str = "",
    ) -> None:
        self.engine = engine
        self.url = url
        self._r2 = (r2_account_id, r2_access_key_id, r2_secret_access_key)
        # An isolated secret_directory, for the reason event_archive.py gives.
        self._secret_dir = tempfile.mkdtemp(prefix="nf-telemetry-duckdb-")

    async def archive(self, workflow_pk: int | None = None) -> ArchiveResult:
        targets = select(workflows_tbl.c.id).where(workflows_tbl.c.status == "retired").order_by(
            workflows_tbl.c.id)
        if workflow_pk is not None:
            targets = targets.where(workflows_tbl.c.id == workflow_pk)
        async with self.engine.connect() as conn:
            pks = [r[0] for r in await conn.execute(targets)]

        result = ArchiveResult()
        for pk in pks:
            n = 0
            while chunk := await self._archive_chunk(pk, n):
                n += chunk
            if n:
                result.workflows_archived += 1
                result.jobs_archived += n
        logger.info("retired_archive.done", extra=asdict(result))
        return result

    async def _archive_chunk(self, pk: int, n: int) -> int:
        """Archive and delete one chunk of `pk`'s jobs; 0 when there is nothing to do."""
        async with self.engine.begin() as conn:
            # The lock makes a concurrent status PATCH wait for the chunk.
            still_retired = (await conn.execute(
                select(workflows_tbl.c.id)
                .where(workflows_tbl.c.id == pk, workflows_tbl.c.status == "retired")
                .with_for_update()
            )).first()
            if still_retired is None:
                return 0
            # Retiring does not cancel in-flight runs; tomorrow's run gets them.
            if (await conn.execute(select(exists().where(
                    jobs_tbl.c.workflow_pk == pk, jobs_tbl.c.status.in_(JOB_INFLIGHT))))).scalar_one():
                return 0
            rows = (await conn.execute(
                select(jobs_tbl).where(jobs_tbl.c.workflow_pk == pk)
                .order_by(jobs_tbl.c.id).limit(ARCHIVE_CHUNK).with_for_update()
            )).all()
            if not rows:
                return 0
            ids = [r.id for r in rows]
            dlq = (await conn.execute(
                select(dead_letter_tbl).where(dead_letter_tbl.c.job_id.in_(ids)))).all()
            # Keys are quoted_name (a str subclass orjson rejects): rebuild as plain str.
            body = b"".join(
                orjson.dumps({str(k): v for k, v in r._asdict().items()}) + b"\n"
                for r in [*rows, *dlq])
            now = datetime.now(timezone.utc)
            key = (f"{self.url.rstrip('/')}/{pk}/{now:%Y-%m-%dT%H%M%S}"
                   f"{now.microsecond // 1000:03d}Z-{n}.ndjson.gz")
            # R2 first: if the put raises, the transaction rolls back and nothing is deleted.
            await asyncio.to_thread(self._put, key, body)
            await conn.execute(delete(dead_letter_tbl).where(dead_letter_tbl.c.job_id.in_(ids)))
            await conn.execute(delete(jobs_tbl).where(jobs_tbl.c.id.in_(ids)))
            return len(rows)

    async def run_daily(self) -> None:
        while True:
            await asyncio.sleep(seconds_until_archive(datetime.now(timezone.utc)))
            try:
                await self.archive()
            except Exception:
                logger.exception("retired_archive.failed")

    def _put(self, key: str, body: bytes) -> None:
        con = duckdb.connect(config={"secret_directory": self._secret_dir})
        try:
            if self._r2[1]:
                con.execute(
                    "CREATE OR REPLACE SECRET r2 (TYPE r2, ACCOUNT_ID $a, KEY_ID $k, SECRET $s)",
                    dict(zip("aks", self._r2)),
                )
            if not key.startswith("r2://"):
                Path(key).parent.mkdir(parents=True, exist_ok=True)
            # key holds only digits, the timestamp and the configured prefix: safe to inline.
            con.execute(f"COPY (SELECT $1::BLOB) TO '{key}' (FORMAT BLOB, COMPRESSION gzip)", [body])
        finally:
            con.close()
