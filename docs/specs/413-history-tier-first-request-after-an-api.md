# Spec #413: history tier, load the event archive at startup

Issue: [#413](https://github.com/seandavi/nextflow_telemetry/issues/413). There is no
GitHub parent issue. It was found in the M7 staging rehearsal
([#377](https://github.com/seandavi/nextflow_telemetry/issues/377)) under tracking issue
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), and it blocks a clean
M8 cutover ([#378](https://github.com/seandavi/nextflow_telemetry/issues/378)), because M8
restarts the API.

## Goal

The history tier (`services/event_archive.py`, `EventArchive`) keeps an in-memory DuckDB
copy of the NDJSON event archive at `EVENTS_ARCHIVE_URL`. Today the first copy is made
lazily: the first call to `EventArchive.query` runs `_refresh_if_stale()` while holding
`self._lock`. That call lists and reads every `dt=*/…ndjson.gz` on R2, which took 93.7 s
on staging. So the first history request after a restart, and every request queued
behind it on the lock, hangs or times out.

After this change, the API lifespan starts that first load in a background thread as soon
as the process starts. It doesn't block startup or the control routes. The task is
cancelled on shutdown, and shutdown interrupts a load that is still running. Requests that
arrive mid-load wait on the existing lock, as they do today. Requests after the load
answer warm (0.03–0.1 s on staging).

## Non-goals

- **No 503 with `Retry-After` while loading.** The issue marks it optional. A 503 would
  fail the contract suite and the dashboard just as a timeout would, only sooner, and the
  frontend has no retry handling for it. The `event_archive.warmed` log line (below) is
  what the M8 runbook waits on before running the contract suite and opening the
  dashboard.
- No readiness field on `/health`, and no change to `/health`. The Docker healthcheck
  must stay green during the load, because the control routes are serving.
- No change to the refresh policy after the first load. Incremental refreshes stay on the
  request path every `EVENTS_ARCHIVE_REFRESH_SECONDS` (they read only new partitions, so
  they are fast).
- No change to the SQL, the schema of the DuckDB copy, `ProcessMetricsService`, the
  routers, or response models.
- No Alembic migration and no Postgres change. The history tier is DuckDB only.
- No new env var or config setting. The load always runs. Tests are kept off R2 by a
  test-only default for `EVENTS_ARCHIVE_URL` (see Design §3), not by a flag.
- No change to `nf-client`, `etl/`, the frontend, `health_report.py`, the add-study bot,
  `cf/`, or the `contract/` suite.

This doesn't depart from ADR-0013, 0007, 0010 or 0012. The history tier is outside the
control plane, and the control routes must not depend on it. That's why the load runs in
the background rather than before the lifespan's `yield`: a 94 s startup would also hold
up claims, heartbeats and `/health`.

## Design

### 1. `src/nextflow_telemetry/services/event_archive.py`

Add `import asyncio` to the imports. Add three public methods to `EventArchive`. Nothing
else in the class changes.

```python
    def refresh(self) -> None:
        """Bring the copy up to date now, the same way the next query would."""
        with self._lock:
            self._refresh_if_stale()

    async def warm(self) -> None:
        """Load the archive in a worker thread so the first request finds it loaded.

        Run from the app lifespan. A failed load is logged, not raised; the next
        query retries it on the request path, as it would without warming.
        """
        started = time.monotonic()
        try:
            await asyncio.to_thread(self.refresh)
        except Exception:
            logger.exception("event_archive.warm_failed")
            return
        logger.info(
            "event_archive.warmed",
            extra={"files": len(self._files), "seconds": round(time.monotonic() - started, 1)},
        )

    def interrupt(self) -> None:
        """Abort the DuckDB statement running on the connection, if any (shutdown)."""
        self._con.interrupt()
```

Constraints the implementer must keep:

- **`asyncio.to_thread`, not `starlette.concurrency.run_in_threadpool`.** anyio's
  `to_thread.run_sync` defers cancellation until the thread returns, so the lifespan's
  `gather` would wait out the whole load at shutdown. `asyncio.to_thread` cancels the
  awaiting coroutine immediately. `retired_archive.py` already uses it.
- **`interrupt()` doesn't take `self._lock`.** It has to work while another thread holds
  the lock inside `_refresh_if_stale`. `DuckDBPyConnection.interrupt()` is meant to be
  called from another thread. I checked it on the installed duckdb 1.5.6:
  - On a running query, it raises `duckdb.InterruptException` ("INTERRUPT Error:
    Interrupted!") in the executing thread within about 1 ms.
  - On an idle connection, it's a no-op: the next `execute` succeeds.
  - The connection stays usable after an interrupt.
- `_refresh_if_stale` is unchanged. A failed first load still re-raises, and
  `_refreshed_at` stays `None`, so `warm()` logs `event_archive.warm_failed` and the next
  query retries the full load. A successful `warm()` sets `_refreshed_at`, so a query
  within `EVENTS_ARCHIVE_REFRESH_SECONDS` doesn't refresh again.
- Interrupting during `_load_runs` (HTTP calls to v2) doesn't abort them. Those calls
  run to completion; see Risks.

### 2. `src/nextflow_telemetry/main.py`: `lifespan`

`event_archive` is a module global that is defined after `lifespan`. That's fine, because
`lifespan` only runs at app startup. Change the function to:

```python
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The history tier's first load reads the whole R2 event archive (~90 s, #413).
    tasks: list[asyncio.Task[None]] = [asyncio.create_task(event_archive.warm())]
    if settings.DEADLINE_SWEEP_ENABLED:
        ...  # unchanged
    if settings.RETIRED_ARCHIVE_ENABLED:
        ...  # unchanged
    if settings.EVENT_INGEST_ENABLED:
        ...  # unchanged
    yield
    for task in tasks:
        task.cancel()
    event_archive.interrupt()
    await asyncio.gather(*tasks, return_exceptions=True)
```

`event_archive.interrupt()` comes after the cancels and before the `gather`. The
`EVENT_INGEST_ENABLED` config check still raises before `yield`. If it raises, the warm
task is already created and the process is exiting, which is acceptable.

### 3. `tests/conftest.py`

After the existing `BLOB_STORE_URL` line, add a module-level default so that no test app
warms from R2:

```python
os.environ.setdefault("EVENTS_ARCHIVE_URL", tempfile.mkdtemp(prefix="events-"))
```

Without it, every `integration_client` / `app_module` start would begin a background
glob against `r2://nf-telemetry/telemetry/events` with no credentials. I measured that at
4.7 s: DuckDB downloads httpfs, then gets `NoSuchBucket`. Update the comment above the
two lines to cover both, for example:
`# config reads these at import; local directories keep tests off R2.`
`control_client` in `tests/test_control_api.py` already sets its own local
`EVENTS_ARCHIVE_URL` and keeps working unchanged.

### 4. `deploy/onclappc02/.env.example`

In the "Historical metrics tier" comment block, add one line. Comment only, no new keys:

```
# The API loads the archive in the background at startup (~90 s from R2); history
# requests wait until the log line "event_archive.warmed" appears.
```

### Affected callers

None. `nf-client`, `etl/`, the frontend, `health_report.py`, the bot and `contract/` are
unchanged. HTTP behavior is the same, except that history requests stop hanging once the
startup load has finished.

### Tests

Add both tests to `tests/test_historical_metrics.py`, which already holds the history-tier
tests, and reuse its `_write` and `_task` helpers.

1. **`test_lifespan_warms_event_archive`** (sync test). It shows that starting the app
   loads the archive with no history request.
   - Write one file with `_write(tmp_path / "events", NOW.date().isoformat(),
     "120000-a.ndjson.gz", [_task("run-a", "s1", "kneaddata", "COMPLETED")])`.
   - Use `monkeypatch.setenv` for `EVENTS_ARCHIVE_URL=str(tmp_path / "events")`,
     `BLOB_STORE_URL=str(tmp_path / "blobs")`, `TELEMETRY_SKIP_DB_INIT=1` and
     `V2_API_URL=http://v2.invalid/api`. Use `monkeypatch.delenv(..., raising=False)` for
     `DEADLINE_SWEEP_ENABLED`, `RETIRED_ARCHIVE_ENABLED` and `EVENT_INGEST_ENABLED`, so
     no Postgres is needed. The engine is created but never connected.
   - Drop every `nextflow_telemetry*` module from `sys.modules`, then
     `module = importlib.import_module("nextflow_telemetry.main")`. This is the
     `control_client` pattern.
   - Before starting the client, call `monkeypatch.setattr(sys.modules[
     "nextflow_telemetry.services.v2_api"], "run_workflows", lambda _url, runs: {r:
     ("cmgd_nextflow", "2.2.1") for r in runs})`. Patch the freshly imported module, not
     the one this test file imported.
   - Inside `with TestClient(module.app):`, poll for up to 5 s, every 0.05 s, until
     `module.event_archive._refreshed_at is not None`. Assert that it is set, then assert
     `len(module.event_archive._files) == 1`. Then assert `module.event_archive.query(
     "select count(*) as n from task_executions")[0]["n"] == 1`. That query is inside the
     refresh window, so it doesn't reload.
2. **`test_warm_logs_and_swallows_a_failed_load`** (async). It shows that a failed first
   load doesn't crash the lifespan task, and that the next query retries.
   - Use the existing `archive` fixture. Call `monkeypatch.setattr(archive, "refresh",
     boom)`, where `boom` raises `RuntimeError("r2 down")`.
   - Inside `with caplog.at_level(logging.ERROR, logger="nextflow_telemetry"):`, which is
     the `test_access_log.py` pattern, `await archive.warm()` returns `None` without
     raising. Afterwards, `"event_archive.warm_failed"` is in
     `[r.getMessage() for r in caplog.records]`.
   - `archive._refreshed_at is None`.

Leave `test_deadlines.py`'s lifespan tests alone. They pass through `integration_client`,
which now warms an empty local directory.

## Acceptance criteria

- [ ] `EventArchive.refresh`, `EventArchive.warm` and `EventArchive.interrupt` exist with
      the signatures in Design §1.
- [ ] `lifespan` always schedules `event_archive.warm()` as a task before `yield`. On
      shutdown it cancels all tasks, calls `event_archive.interrupt()`, and then gathers.
- [ ] `warm()` uses `asyncio.to_thread`. It logs `event_archive.warmed` with `files` and
      `seconds` on success. On failure it logs `event_archive.warm_failed` (via
      `logger.exception`) and doesn't raise.
- [ ] `tests/conftest.py` defaults `EVENTS_ARCHIVE_URL` to a temp directory, so no test
      app touches R2.
- [ ] `test_lifespan_warms_event_archive` and `test_warm_logs_and_swallows_a_failed_load`
      pass.
- [ ] `uv run pytest` passes, with no new failures compared with `main`.
- [ ] `uv run mypy src/nextflow_telemetry` passes.
- [ ] No change outside `services/event_archive.py`, `main.py`, `tests/conftest.py`,
      `tests/test_historical_metrics.py` and `deploy/onclappc02/.env.example`.

## Test plan

```
uv run pytest tests/test_historical_metrics.py tests/test_process_metrics_service.py -q
uv run pytest tests/test_deadlines.py tests/test_control_api.py -q   # lifespan + control_client still fine
uv run pytest -q
uv run mypy src/nextflow_telemetry
```

Manual check, optional, not part of the implement stage: start the API locally with
`EVENTS_ARCHIVE_URL` pointing at a directory of archive files. `event_archive.warmed`
should appear in the log with no request made, and `GET
/api/metrics/processes/summary` should answer immediately afterwards. On staging (M7),
after a restart, wait for `event_archive.warmed`, then run the contract suite. Its read
tests should pass without `httpx.ReadTimeout`.

## Risks

- **Requests during the load still wait.** A history request in the first ~94 s blocks on
  the lock, exactly as today. The runbook has to wait for `event_archive.warmed`. If that
  proves too fragile, the 503 `Retry-After` path in the issue is the follow-up.
- **Memory and R2 egress at every start.** Each restart, including dev `just run --reload`
  with R2 credentials, reads the whole archive even if no one opens the dashboard. That's
  the same work the first dashboard request already triggers, just earlier.
- **Dev without R2 credentials.** `just run` with the default r2 URL and no keys logs one
  `event_archive.warm_failed` traceback about 5 s after startup. History routes fail in
  that setup today anyway.
- **Shutdown during `_load_runs`.** `interrupt()` stops DuckDB statements only. The v2
  HTTP calls in `_load_runs` (`v2_api.run_workflows`) run to completion before the
  thread exits. The interpreter waits for that thread on exit, so shutdown can lag by the
  duration of those calls. Docker's stop timeout bounds it, and a SIGKILL loses nothing
  because the copy is in memory and read-only.
- **The tests read private attributes** (`_refreshed_at`, `_files`). That's acceptable
  for a test of this module's own load state. There is no public "loaded" flag, because
  nothing else needs one.
