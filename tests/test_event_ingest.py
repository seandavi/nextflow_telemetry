"""services/event_ingest.py (#375): the R2 -> Postgres event ingester, on a local store and the testcontainers Postgres."""
from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from obstore.store import LocalStore
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nextflow_telemetry.config import settings
from nextflow_telemetry.control_db import (
    ingest_cursor_tbl,
    ingest_rejects_tbl,
    jobs_tbl,
    run_task_counts_tbl,
    runs_tbl,
)
from nextflow_telemetry.services import event_ingest, transitions
from nextflow_telemetry.services.event_ingest import EVENTS_STREAM, ingest_once, is_transient, read_lines
from test_control_transitions import claim, run_row, seed

T0 = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
POLL = T0 + timedelta(hours=1)
DAY = "telemetry/events/dt=2026-10-31"


@pytest.fixture
async def engine(db_asyncpg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_asyncpg_url)
    yield engine
    await engine.dispose()


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def weblog(seconds: int, **body: Any) -> str:
    return json.dumps({"source": "weblog", "received_at": at(seconds).isoformat(), "payload": body})


def run_event(seconds: int, run_name: str, payload: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"source": "run_event", "received_at": at(seconds).isoformat(), "run_name": run_name,
                       "payload": payload, **extra})


def put(root: Path, name: str, lines: list[str]) -> str:
    key = f"{DAY}/{name}.ndjson.gz"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress("".join(line + "\n" for line in lines).encode()))
    return key


async def cursor(engine: AsyncEngine) -> tuple[str, int, datetime | None]:
    async with engine.connect() as conn:
        row = (await conn.execute(select(ingest_cursor_tbl).where(ingest_cursor_tbl.c.stream == EVENTS_STREAM))).one()
    return row.object_key, row.line_offset, row.caught_up_at


async def rejects(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(ingest_rejects_tbl).order_by(
            ingest_rejects_tbl.c.object_key, ingest_rejects_tbl.c.line_no))).all())


async def claimed_run(engine: AsyncEngine, name: str, *, max_retries: int = 1) -> tuple[str, str]:
    """A claimed run with one job; returns (run_name, the job's sample key)."""
    await seed(engine, name, max_retries=max_retries)
    batch = await claim(engine, name)
    return batch["run_name"], batch["jobs"][0]["sample_id"]


async def test_applies_events_in_order_and_advances_cursor(engine: AsyncEngine, tmp_path: Path) -> None:
    run, sample = await claimed_run(engine, "ordered")
    mark = {"process": "x:MARK_COMPLETE", "status": "COMPLETED", "tag": sample, "hash": "aa/bbbbbb"}
    k1 = put(tmp_path, "100000-00000001", [
        weblog(1, runName=run, runId="nf-id", event="started"),
        run_event(2, run, {"type": "heartbeat"}),
    ])
    k2 = put(tmp_path, "100000-00000002", [
        weblog(3, runName=run, event="process_completed", trace=mark),
        run_event(4, run, {"type": "wrapper_exited", "exit_code": 0}),
    ])
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    assert (result.objects, result.lines, result.rejected, result.skipped, result.caught_up) == (2, 4, 0, False, True)
    assert result.last_received_at == at(4)
    row = await run_row(engine, run)
    assert (row["status"], row["run_id"], row["started_at"], row["completed_at"]) == ("completed", "nf-id", at(1), at(4))
    assert row["last_heartbeat_at"] == at(2) and row["wrapper_exit_code"] == 0
    async with engine.connect() as conn:
        job = (await conn.execute(select(jobs_tbl.c.status, jobs_tbl.c.completed_at).where(jobs_tbl.c.run_name == run))).one()
    assert (job.status, job.completed_at) == ("completed", at(3))
    assert await cursor(engine) == (k2, 2, POLL)
    assert k1 < k2


async def test_empty_store_sets_caught_up(engine: AsyncEngine, tmp_path: Path) -> None:
    result = await ingest_once(engine, LocalStore(tmp_path, mkdir=True), now=POLL)
    assert (result.objects, result.caught_up) == (0, True)
    assert await cursor(engine) == ("", 0, POLL)


async def test_cursor_mid_object_applies_each_line_once(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "once")
    trace = {"process": "x:ALIGN", "status": "FAILED", "exit": 1, "hash": "aa/bbbbbb"}
    key = put(tmp_path, "100000-00000001", [weblog(i, runName=run, event="process_completed", trace=trace) for i in range(3)])
    async with engine.begin() as conn:
        await conn.execute(ingest_cursor_tbl.insert().values(stream=EVENTS_STREAM, object_key=key, line_offset=2))
    store = LocalStore(tmp_path)

    first = await ingest_once(engine, store, now=POLL)
    second = await ingest_once(engine, store, now=POLL)

    assert (first.lines, second.lines, second.objects) == (1, 0, 0)
    async with engine.connect() as conn:
        n = (await conn.execute(select(run_task_counts_tbl.c.n).where(run_task_counts_tbl.c.run_name == run))).scalar_one()
    assert n == 1
    assert await cursor(engine) == (key, 3, POLL)


async def test_late_events_do_not_reopen_a_closed_run(engine: AsyncEngine, tmp_path: Path) -> None:
    run, sample = await claimed_run(engine, "late", max_retries=0)
    mark = {"process": "x:MARK_COMPLETE", "status": "COMPLETED", "tag": sample, "hash": "aa/bbbbbb"}
    put(tmp_path, "100000-00000001", [
        weblog(1, runName=run, runId="nf", event="started"),
        weblog(2, runName=run, event="completed", metadata={"workflow": {"success": True}}),
        weblog(3, runName=run, event="process_completed", trace=mark),
        weblog(4, runName=run, runId="nf", event="started"),
        run_event(5, run, {"type": "heartbeat"}),
        run_event(6, run, {"type": "wrapper_started"}),
    ])
    await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    row = await run_row(engine, run)
    assert (row["status"], row["completed_at"], row["liveness_deadline"]) == ("completed", at(2), None)
    async with engine.connect() as conn:
        status = (await conn.execute(select(jobs_tbl.c.status).where(jobs_tbl.c.run_name == run))).scalar_one()
    assert status == "failed"


async def test_run_event_sets_liveness_deadline_and_log_stamp(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "live")
    put(tmp_path, "100000-00000001", [
        run_event(1, run, {"type": "wrapper_started"}),
        run_event(2, run, {"type": "heartbeat"}, nextflow_log_uploaded=True),
    ])
    await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    row = await run_row(engine, run)
    assert row["status"] == "submitted" and row["submitted_at"] == at(1)
    assert row["liveness_deadline"] == at(2) + timedelta(minutes=settings.LIVENESS_MINUTES)
    assert row["nextflow_log_uploaded_at"] == at(2)


async def test_bad_lines_are_rejected_and_the_rest_applied(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "bad")
    naive = json.dumps({"source": "weblog", "received_at": "2026-10-31T12:00:00", "payload": {}})
    no_time = json.dumps({"source": "weblog", "payload": {}})
    nul_run = run_event(5, "r\u0000x", {"type": "heartbeat"}, nextflow_log_uploaded=True)
    key = put(tmp_path, "100000-00000001", [
        "{not json",
        "[1]",
        json.dumps({"source": "other", "received_at": at(1).isoformat(), "payload": {}}),
        no_time,
        naive,
        run_event(5, run, {"type": "heartbeat"}),
        nul_run,
    ])
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    assert (result.lines, result.rejected, result.objects) == (1, 6, 1)
    found = await rejects(engine)
    assert [r.line_no for r in found] == [0, 1, 2, 3, 4, 6]
    assert {r.object_key for r in found} == {key} and found[0].line == "{not json"
    assert (await run_row(engine, run))["last_heartbeat_at"] == at(5)
    assert (await cursor(engine))[:2] == (key, 7)


async def test_ill_typed_values_are_coerced_not_rejected(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "coerce", max_retries=0)
    put(tmp_path, "100000-00000001", [
        run_event(1, run, {"type": "pre_nextflow", "wait_seconds": 1.5}),
        run_event(2, run, {"type": "wrapper_exited", "exit_code": "x"}),
    ])
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    assert (result.lines, result.rejected) == (2, 0)
    row = await run_row(engine, run)
    assert (row["wait_seconds"], row["status"], row["wrapper_exit_code"]) == (1, "failed", None)


class _Deadlock(Exception):
    sqlstate = "40P01"


async def test_transient_error_aborts_the_object(engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = await claimed_run(engine, "transient")
    put(tmp_path, "100000-00000001", [run_event(1, run, {"type": "heartbeat"}), weblog(2, runName=run, event="started")])

    async def deadlock(*_a: Any, **_k: Any) -> None:
        raise DBAPIError("x", None, _Deadlock())

    monkeypatch.setattr(transitions, "apply_weblog_event", deadlock)
    with pytest.raises(DBAPIError):
        await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    assert (await cursor(engine))[:2] == ("", 0)
    assert await rejects(engine) == []
    assert (await run_row(engine, run))["last_heartbeat_at"] is None


def test_is_transient() -> None:
    assert is_transient(DBAPIError("x", None, _Deadlock()))
    assert not is_transient(DBAPIError("x", None, Exception()))
    assert is_transient(TimeoutError()) and is_transient(OSError())
    assert not is_transient(ValueError())


async def test_until_stops_inside_an_object(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "until")
    k1 = put(tmp_path, "100000-00000001", [run_event(1, run, {"type": "heartbeat"})])
    k2 = put(tmp_path, "100000-00000002", [run_event(2, run, {"type": "pre_nextflow", "wait_seconds": 7}),
                                          run_event(3, run, {"type": "pre_nextflow", "wait_seconds": 9})])
    put(tmp_path, "100000-00000003", [run_event(4, run, {"type": "pre_nextflow", "wait_seconds": 11})])
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL, until=(k2, 1))

    assert (result.objects, result.lines, result.caught_up) == (2, 2, False)
    assert await cursor(engine) == (k2, 1, None)
    assert (await run_row(engine, run))["wait_seconds"] == 7
    assert k1 < k2


async def test_locked_cursor_row_skips_the_poll(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "locked")
    put(tmp_path, "100000-00000001", [run_event(1, run, {"type": "heartbeat"})])
    async with engine.begin() as conn:
        await conn.execute(ingest_cursor_tbl.insert().values(stream=EVENTS_STREAM, object_key="", line_offset=0))
    async with engine.connect() as holder:
        async with holder.begin():
            await holder.execute(text("select 1 from ingest_cursor for update"))
            result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)

    assert (result.skipped, result.lines, result.caught_up) == (True, 0, False)
    assert (await run_row(engine, run))["last_heartbeat_at"] is None


async def test_moved_cursor_skips_the_poll(engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = await claimed_run(engine, "moved")
    put(tmp_path, "100000-00000001", [run_event(1, run, {"type": "heartbeat"})])
    real = event_ingest._work_items

    async def moves_cursor(*args: Any):
        async with engine.begin() as conn:
            await conn.execute(update(ingest_cursor_tbl).values(object_key="telemetry/events/zz", line_offset=1))
        async for item in real(*args):
            yield item

    monkeypatch.setattr(event_ingest, "_work_items", moves_cursor)
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)
    assert (result.skipped, result.lines) == (True, 0)


async def test_unicode_line_separator_stays_inside_its_line(engine: AsyncEngine, tmp_path: Path) -> None:
    run, _ = await claimed_run(engine, "u2028")
    line = json.dumps({"source": "weblog", "received_at": at(1).isoformat(),
                       "payload": {"runName": run, "event": "started", "runId": "a\u2028b"}}, ensure_ascii=False)
    key = put(tmp_path, "100000-00000001", [line, run_event(2, run, {"type": "heartbeat"})])

    assert await read_lines(LocalStore(tmp_path), key) == [line, run_event(2, run, {"type": "heartbeat"})]
    result = await ingest_once(engine, LocalStore(tmp_path), now=POLL)
    assert (result.lines, result.rejected) == (2, 0)
    assert (await run_row(engine, run))["run_id"] == "a\u2028b"


async def test_keys_after_ignores_other_files(tmp_path: Path) -> None:
    k1 = put(tmp_path, "100000-00000001", [])
    k2 = put(tmp_path, "100000-00000002", [])
    (tmp_path / DAY / "README.txt").write_text("x")
    store = LocalStore(tmp_path)
    assert await event_ingest.keys_after(store, "") == [k1, k2]
    assert await event_ingest.keys_after(store, k1) == [k2]
