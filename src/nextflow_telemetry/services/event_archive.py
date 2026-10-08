"""The v2 telemetry event archive as local DuckDB tables.

v2's SinkDO flushes every event to ``<source>/dt=YYYY-MM-DD/<hhmmss>-<id>.ndjson.gz``
on R2 (see ``cf/src/sink-do.ts``). Files are immutable once written, so this
module keeps an in-memory DuckDB copy and, on refresh, reads only files it has
not seen, listing only the partitions from the newest one it already holds to
today. It exposes the same two relations the v1 Postgres queries used:

- ``telemetry``: one row per Nextflow weblog event.
- ``task_executions``: one row per ``process_completed`` event with a trace,
  columns derived from the trace exactly as v1's migration ``e3f4a5b6`` did.

Both carry ``workflow_id`` / ``workflow_version`` from v2 ``GET /api/runs``. The
archived ``started`` event's manifest version is not used: a run of registered
version 2.2.1 at revision 2.2.3 reports manifest 2.2.3 (ADR-0004).
"""
from __future__ import annotations

import datetime as dt
import re
import tempfile
import threading
import time
from typing import Any

import duckdb
from starlette.concurrency import run_in_threadpool

from ..log import logger
from . import v2_api

_COLUMNS = (
    "{run_name:'VARCHAR', run_id:'VARCHAR', sample_id:'VARCHAR', event:'VARCHAR', "
    "utc_time:'TIMESTAMPTZ', source:'VARCHAR', payload:'JSON'}"
)

_SCHEMA = """
create sequence telemetry_id_seq;
create table events (
    telemetry_id bigint, run_name varchar, run_id varchar, sample_id varchar,
    event varchar, utc_time timestamp,
    has_trace boolean, task_id varchar, task_hash varchar, process varchar,
    name varchar, status varchar, attempt integer, exit_code varchar,
    error_action varchar, realtime_ms double, requested_cpus double,
    requested_memory_bytes double, requested_time_ms double, pct_cpu double,
    pct_mem double, peak_rss double, read_bytes double, write_bytes double,
    rchar double, wchar double
);
create table runs (run_name varchar primary key, workflow_id varchar, workflow_version varchar);
create view telemetry as
    select e.*, r.workflow_id, r.workflow_version from events e left join runs r using (run_name);
create view task_executions as
    select * from telemetry where event = 'process_completed' and has_trace;
"""


def _trace(field: str, cast: str = "VARCHAR") -> str:
    expr = f"""json_extract_string(payload, '$.trace."{field}"')"""
    return expr if cast == "VARCHAR" else f"try_cast(nullif({expr}, '') as {cast})"


_INSERT = f"""
insert into events
select
    nextval('telemetry_id_seq'), run_name, run_id, sample_id, event,
    utc_time::timestamp,
    json_extract(payload, '$.trace') is not null,
    coalesce({_trace('task_id')}, ''),
    {_trace('hash')},
    coalesce({_trace('process')}, ''),
    {_trace('name')},
    coalesce({_trace('status')}, ''),
    coalesce({_trace('attempt', 'INTEGER')}, 1),
    {_trace('exit')},
    {_trace('error_action')},
    {_trace('realtime', 'DOUBLE')},
    {_trace('cpus', 'DOUBLE')},
    {_trace('memory', 'DOUBLE')},
    {_trace('time', 'DOUBLE')},
    {_trace('%cpu', 'DOUBLE')},
    {_trace('%mem', 'DOUBLE')},
    {_trace('peak_rss', 'DOUBLE')},
    {_trace('read_bytes', 'DOUBLE')},
    {_trace('write_bytes', 'DOUBLE')},
    {_trace('rchar', 'DOUBLE')},
    {_trace('wchar', 'DOUBLE')}
from read_json($files, format='newline_delimited', columns={_COLUMNS})
where source = 'weblog'
order by utc_time
"""


class EventArchive:
    """Owns the DuckDB connection, the R2 secret and the refresh policy.

    ponytail: one connection behind one lock, so queries run one at a time.
    Fine while each takes milliseconds; give each query its own cursor over a
    read-only snapshot if dashboard concurrency ever makes it queue.
    """

    def __init__(
        self,
        *,
        source: str,
        v2_api_url: str,
        refresh_seconds: float,
        r2_account_id: str = "",
        r2_access_key_id: str = "",
        r2_secret_access_key: str = "",
    ) -> None:
        self.source = source.rstrip("/")
        self.v2_api_url = v2_api_url
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._files: set[str] = set()
        self._newest_dt: dt.date | None = None
        self._runs_missing_from_v2: set[str] = set()
        self._refreshed_at: float | None = None
        # An isolated secret_directory: this host's ~/.duckdb/stored_secrets
        # holds secret types older clients cannot load.
        self._con = duckdb.connect(
            config={"secret_directory": tempfile.mkdtemp(prefix="nf-telemetry-duckdb-")}
        )
        self._con.execute("SET TimeZone = 'UTC'")
        self._con.execute(_SCHEMA)
        self._r2 = (r2_account_id, r2_access_key_id, r2_secret_access_key)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_if_stale()
            # Bind only the parameters this statement names; DuckDB rejects extras.
            used = set(re.findall(r"\$(\w+)", sql))
            cur = self._con.execute(sql, {k: v for k, v in (params or {}).items() if k in used})
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, (_utc(v) for v in row))) for row in cur.fetchall()]

    async def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await run_in_threadpool(self.query, sql, params)

    def _refresh_if_stale(self) -> None:
        now = time.monotonic()
        if self._refreshed_at is not None and now - self._refreshed_at < self.refresh_seconds:
            return
        try:
            if self._refreshed_at is None and self._r2[1]:
                # Here, not in __init__: r2 secrets autoload httpfs, which may
                # download it, and app startup should not depend on that.
                self._con.execute(
                    "CREATE OR REPLACE SECRET r2 (TYPE r2, ACCOUNT_ID $a, KEY_ID $k, SECRET $s)",
                    dict(zip("aks", self._r2)),
                )
            self._load_new_files()
        except Exception:
            # Serve what we have; a first load that fails has nothing to serve.
            if self._refreshed_at is None:
                raise
            logger.exception("event_archive.refresh_failed")
        try:
            self._load_runs()
        except Exception:
            # Unattributed runs only drop out of workflow-filtered queries; retried next refresh.
            logger.exception("event_archive.run_attribution_failed")
        self._refreshed_at = now

    def _load_new_files(self) -> None:
        if self._newest_dt is None:
            patterns = [f"{self.source}/*/*.ndjson.gz"]
        else:
            today = dt.datetime.now(dt.timezone.utc).date()
            days = (today - self._newest_dt).days
            patterns = [
                f"{self.source}/dt={self._newest_dt + dt.timedelta(days=i)}/*.ndjson.gz"
                for i in range(days + 1)
            ]
        new: list[str] = []
        for pattern in patterns:
            listed = self._con.execute("select file from glob($p)", {"p": pattern}).fetchall()
            new.extend(f for (f,) in listed if f not in self._files)
        if not new:
            return
        new.sort()
        self._con.execute(_INSERT, {"files": new})
        self._files.update(new)
        for f in new:
            part = f.rsplit("/", 2)[-2]
            if part.startswith("dt="):
                d = dt.date.fromisoformat(part[3:])
                if self._newest_dt is None or d > self._newest_dt:
                    self._newest_dt = d
        logger.info("event_archive.loaded", extra={"files": len(new), "total_files": len(self._files)})

    def _load_runs(self) -> None:
        missing = {
            r for (r,) in self._con.execute(
                "select distinct run_name from events anti join runs using (run_name)"
            ).fetchall()
        } - self._runs_missing_from_v2
        if not missing:
            return
        found = v2_api.run_workflows(self.v2_api_url, missing)
        if found:
            self._con.executemany(
                "insert or replace into runs values (?, ?, ?)",
                [(r, wid, ver) for r, (wid, ver) in found.items()],
            )
        self._runs_missing_from_v2 |= missing - found.keys()


def _utc(v: Any) -> Any:
    # Timestamps are stored naive in UTC; label them so JSON carries the offset.
    if isinstance(v, dt.datetime) and v.tzinfo is None:
        return v.replace(tzinfo=dt.timezone.utc)
    return v
