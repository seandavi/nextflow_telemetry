"""R2 -> Postgres event ingester (ADR-0013 §3, #375).

Lists ``telemetry/events/`` after ``ingest_cursor`` and applies each NDJSON line once, in
key order then line order (SinkDO's write order), through ``transitions`` only, with
``now = received_at``. One transaction per object, one savepoint per line: a line and the
cursor that passes it commit together, so ``record_task``'s count stays exact. A line
that fails for a reason of its own goes to ``ingest_rejects`` and the cursor moves on.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import obstore
from obstore.store import ObjectStore
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..control_db import ingest_cursor_tbl, ingest_rejects_tbl
from ..log import logger
from . import transitions

EVENTS_STREAM = "telemetry/events"  # ingest_cursor.stream, and the key prefix below
EVENTS_PREFIX = EVENTS_STREAM + "/"
_TRANSIENT_SQLSTATE_CLASSES = ("08", "40", "53", "57")  # connection, rollback (deadlock/serialization), resources, operator
REJECT_ERROR_MAX = 2000


@dataclass
class IngestResult:
    objects: int = 0                          # objects whose transaction committed
    lines: int = 0                            # lines applied
    rejected: int = 0                         # lines written to ingest_rejects
    skipped: bool = False                     # another ingester holds or moved the cursor
    caught_up: bool = False                   # caught_up_at was set by this poll
    last_received_at: datetime | None = None  # received_at of the last applied line


def is_transient(exc: BaseException) -> bool:
    """Retry the object on the next poll; anything else is a property of the line."""
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None) or ""
        return exc.connection_invalidated or sqlstate.startswith(_TRANSIENT_SQLSTATE_CLASSES)
    return isinstance(exc, (OSError, TimeoutError))


async def read_lines(store: ObjectStore, key: str) -> list[str]:
    raw = gzip.decompress(bytes(await (await obstore.get_async(store, key)).bytes_async()))
    # Not splitlines(): JSON.stringify leaves U+2028 unescaped, which would cut a line in two.
    lines = raw.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


async def keys_after(store: ObjectStore, after_key: str) -> list[str]:
    keys: list[str] = []
    async for batch in obstore.list(store, prefix=EVENTS_PREFIX, offset=after_key or None):
        keys.extend(o["path"] for o in batch)
    return sorted(k for k in keys if k > after_key and k.endswith(".ndjson.gz"))


async def apply_line(conn: AsyncConnection, line: str) -> datetime:
    """Apply one envelope and return its ``received_at``. Raises on a malformed envelope."""
    envelope = json.loads(line)
    if not isinstance(envelope, dict):
        raise ValueError("envelope is not an object")
    received_at = datetime.fromisoformat(envelope["received_at"])
    if received_at.tzinfo is None:
        raise ValueError("received_at has no timezone")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    source = envelope.get("source")
    if source == "weblog":
        await transitions.apply_weblog_event(conn, payload, now=received_at)
    elif source == "run_event":
        run_name = envelope.get("run_name")
        if not isinstance(run_name, str) or not run_name:
            raise ValueError("run_event without run_name")
        if envelope.get("nextflow_log_uploaded") is True:
            await transitions.mark_log_uploaded(conn, run_name, now=received_at)
        await transitions.apply_run_event(conn, run_name, payload, now=received_at)
    else:
        raise ValueError(f"unknown source {source!r}")
    return received_at


def _no_nul(s: str) -> str:
    return s.replace("\x00", "")


async def _work_items(store: ObjectStore, object_key: str, line_offset: int, until: tuple[str, int] | None):
    """Yield ``(key, first line to apply, lines)`` lazily, one object at a time."""
    if until is not None and until[0] < object_key:
        return
    if object_key:
        lines = await read_lines(store, object_key)
        end = until[1] if until is not None and until[0] == object_key else len(lines)
        if line_offset < end:
            yield object_key, line_offset, lines[:end]
    for k in await keys_after(store, object_key):
        if until is not None and k > until[0]:
            return
        lines = await read_lines(store, k)
        yield k, 0, (lines[: until[1]] if until is not None and k == until[0] else lines)


async def ingest_once(
    engine: AsyncEngine, store: ObjectStore, *, now: datetime, until: tuple[str, int] | None = None
) -> IngestResult:
    """One poll. ``now`` is its start (UTC-aware); ``until`` is an inclusive ``(key, line_count)`` bound."""
    result = IngestResult()
    async with engine.begin() as conn:
        await conn.execute(
            pg_insert(ingest_cursor_tbl)
            .values(stream=EVENTS_STREAM, object_key="", line_offset=0)
            .on_conflict_do_nothing(index_elements=["stream"])
        )
        cursor = (await conn.execute(
            select(ingest_cursor_tbl.c.object_key, ingest_cursor_tbl.c.line_offset)
            .where(ingest_cursor_tbl.c.stream == EVENTS_STREAM)
        )).one()
    expected = (cursor[0], cursor[1])

    async for key, start, lines in _work_items(store, expected[0], expected[1], until):
        async with engine.begin() as conn:
            locked = (await conn.execute(
                select(ingest_cursor_tbl.c.object_key, ingest_cursor_tbl.c.line_offset)
                .where(ingest_cursor_tbl.c.stream == EVENTS_STREAM)
                .with_for_update(skip_locked=True)
            )).first()
            if locked is None or (locked[0], locked[1]) != expected:
                result.skipped = True
                return result
            for i in range(start, len(lines)):
                try:
                    async with conn.begin_nested():
                        received = await apply_line(conn, lines[i])
                    result.lines += 1
                    result.last_received_at = received
                except Exception as exc:
                    if is_transient(exc):
                        raise
                    error = _no_nul(f"{type(exc).__name__}: {exc}"[:REJECT_ERROR_MAX])
                    reject = pg_insert(ingest_rejects_tbl).values(
                        stream=EVENTS_STREAM, object_key=key, line_no=i, error=error,
                        line=_no_nul(lines[i]), rejected_at=now)
                    await conn.execute(reject.on_conflict_do_update(
                        index_elements=["stream", "object_key", "line_no"],
                        set_={"error": reject.excluded.error, "line": reject.excluded.line,
                              "rejected_at": reject.excluded.rejected_at}))
                    logger.warning("event_ingest.rejected", extra={"object_key": key, "line_no": i, "error": error})
                    result.rejected += 1
            await conn.execute(
                update(ingest_cursor_tbl)
                .where(ingest_cursor_tbl.c.stream == EVENTS_STREAM)
                .values(object_key=key, line_offset=len(lines), updated_at=now)
            )
        expected = (key, len(lines))
        result.objects += 1

    if until is None:
        async with engine.begin() as conn:
            await conn.execute(
                update(ingest_cursor_tbl).where(ingest_cursor_tbl.c.stream == EVENTS_STREAM).values(caught_up_at=now)
            )
        result.caught_up = True
    return result


async def run_event_ingest(engine: AsyncEngine, store: ObjectStore, interval: timedelta) -> None:
    """Background loop. Cancelling the task stops it."""
    while True:
        try:
            result = await ingest_once(engine, store, now=datetime.now(timezone.utc))
            if result.objects or result.rejected or result.skipped:
                lag = (
                    None if result.last_received_at is None
                    else (datetime.now(timezone.utc) - result.last_received_at).total_seconds()
                )
                logger.info("event_ingest.poll", extra={**asdict(result), "lag_seconds": lag})
        except Exception:
            logger.exception("event_ingest.failed")
        await asyncio.sleep(interval.total_seconds())
