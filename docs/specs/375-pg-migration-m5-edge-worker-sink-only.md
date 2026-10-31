# Spec #375: PG migration M5, edge Worker sink-only mode, R2 event ingester, nf-client `event_url`

Issue: [#375](https://github.com/seandavi/nextflow_telemetry/issues/375). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), step M5. Blocked by M1
(#371, `fa0b1c2d`) and M3 (#373), both merged. Uses M4's sweep (#374, `0d5e1a7c`) and
M6's export cursor (#376). Feeds M7 (#377) and M8 (#378). ADR-0013 decisions 2, 3 and 7
are the design this spec implements. It also fixes #405 code-r0-3 (event values written
to typed columns without coercion).

## Goal

1. **Sink-only Worker.** `MODE=sink-only` in `cf/`. In that mode `POST /telemetry`,
   `POST /runs/{run}/event` and `POST /task-logs` answer exactly as they do today (same
   status codes and bodies) and write through SinkDO (and R2 for attachments), but never
   create a ControlDO or RunDO stub. The daily cron only flushes SinkDO. The default
   stays full mode; M8 flips the flag.
2. **Event ingester on onclappc02.** An asyncio task in the API process, off by default.
   Every `EVENT_INGEST_SECONDS` it lists `telemetry/events/` after `ingest_cursor`. It
   applies each NDJSON line once, in key order and then line order, through
   `services/transitions.py` with `now = received_at`. It commits one transaction per
   object, with one savepoint per line. A line that still fails after coercion goes to
   `ingest_rejects`, and the cursor moves past it.
3. **Poison-pill hardening in `transitions`.** Values from events are coerced (`_int`,
   `_str`, `_dict`) before they reach typed columns. That covers the origin's own event
   routes too (#405 code-r0-3).
4. **Sweep waits for the ingester.** While the ingester isn't caught up, the deadline
   sweep holds liveness and backstop expiry (claim expiry continues). This is the
   follow-up M4's spec named under Risks.
5. **`nf-client` `event_url`.** It's separate from `server_url`. The SLURM template
   passes it to `run-wrapper --telemetry-url`. `-with-weblog` keeps using the existing
   `weblog_url`, which already points at the edge.
6. **Replay check.** `python -m nextflow_telemetry.v2_replay BEFORE AFTER EVENTS` imports
   export BEFORE into a scratch database, ingests events up to AFTER's cursor, and
   compares run and job states with AFTER. This is the tool for the issue's done-gate.

## Non-goals

- No deploy of the Worker or the service, no flip of `MODE`, no `just deploy-clients`
  rollout, no production export and no production migration in the implement stage.
  Those are operator steps (see "Done-when and the production replay").
- No change to the non-event Worker routes (dispatch, samples, workflows, daemons,
  admin, reads, `/metrics/processes/running`). They stay in full behavior in both modes
  until M10 deletes them.
- No change to `RunDO` or `ControlDO`. Alarms that RunDOs already have armed still fire
  into ControlDO's frozen copy after the flip. See Risks.
- `FLUSH_MS` stays at 60 000 ms and `FLUSH_ROWS` at 500. That already matches the
  latency target ("60 s now"), so nothing changes.
- No Cloudflare Queue (ADR-0013 §3), and no change to `EventArchive` (it reads the
  same objects with explicit columns, so the new envelope field is ignored; checked
  with DuckDB `read_json(..., columns=...)`).
- No client-side buffering in `nf-client` for origin outages (#86). See Decision 9.
- No change to the pipeline's `afterScript` task-log upload. It posts to the
  pipeline's `params.api_url`, which is already the Worker, and the Worker's
  `/task-logs` route never touches ControlDO.
- No change to `etl/`, the frontend, `health_report.py`, the add-study bot or the
  `contract/` suite.

## Departures and decisions

1. **The origin's event routes stay after cutover.** The origin's `POST /telemetry` and
   `POST /api/runs/{run}/event` (M3) keep applying events synchronously. The contract
   suite, `just pg-local` and local development use them. Production reporters post to
   the edge only: `weblog_url` and `event_url` both point at the Worker. An event posted
   to the origin is applied but not archived to R2, as the route's description already
   says. Nothing posts the same event to both.
2. **Order is key order, then line order, with no sort by `received_at`.** There is one
   SinkDO. It assigns `received_at` when the event is buffered, flushes the buffer in
   insertion order, and names objects `dt=<date>/<HHMMSS>-<seq hex>`, so key order then
   line order *is* `received_at` order (#376). Sorting would add nothing.
3. **The envelope gains `nextflow_log_uploaded: true`** when a `nextflow_log`
   attachment was stored. It's added in **both** modes, so M7's staging ingester (which
   follows production while the Worker is still in full mode) sets
   `runs.nextflow_log_uploaded_at`. `classification` reads that column
   (`ended-no-log`). The key is absent otherwise.
4. **Sink-only mode stores run-event attachments without the orphan check.** The Worker
   can't ask whether a run exists without ControlDO. It stores
   `nextflow-logs/<run>/...` for any run name. For an unknown run the ingester's
   `mark_log_uploaded` updates no row. `run_name` is an unguessable UUIDv7, so the
   exposure is the same as `/task-logs`, which never had the check.
5. **The cron in sink-only mode only flushes SinkDO.** The ControlDO snapshot and
   `archiveRetiredJobs` are skipped. After cutover the Python service owns the
   retired-job archive (`RETIRED_ARCHIVE_URL`), and both write under `archive/jobs/`.
6. **Rejected lines go to a table, not only a log.** `ingest_rejects` keeps the key,
   line number, error and line text, so a rejected `MARK_COMPLETE` can be found and
   replayed. Log rotation in the container would otherwise lose it.
7. **Coercion covers only `_int`, `_str` and `_dict`.** The issue lists `_float` as an
   example, but no column that events write is a float. `wait_seconds`,
   `wrapper_exit_code` and `run_failed_tasks.attempt` are `integer`.
8. **The deadline sweep waits for the ingester (new).** ADR-0013 §4 and M4 cover only
   an outage of the service. With heartbeats arriving by pull, a stalled ingester (R2
   unreachable, a bug) while the service is up would fail every live run 10 minutes
   later. The fix:
   - The cursor row gets `caught_up_at`, the start time of the last poll that applied
     everything it listed.
   - While that row exists and `caught_up_at` is null or older than `LIVENESS / 2`, the
     sweep expires claims only.
   - With no cursor row (contract suite, `pg-local`), behavior is unchanged.
   - An M6 import writes the row with `caught_up_at` null, so after cutover, liveness
     expiry starts only once the ingester has caught up.
9. **#86: no `nf-client` buffering for origin calls.**
   - Claims need the origin anyway, so while it's down nothing new is dispatched.
   - `/dispatch/submitted` follows a claim within seconds, so the window is the
     outage's first seconds. `wrapper_started` (edge → ingester) also moves
     `claimed → submitted`.
   - The sweep's grace period covers the claim TTL across an outage.
   - Revisit only if M7 or production shows a lost `submitted`.
10. **The ingester reads its own `EVENT_INGEST_URL`, not `BLOB_STORE_URL`.** It uses
    the same URL syntax (`r2://<bucket>` or a local directory). Staging (M7) can then
    follow production's event prefix without its own blob writes landing in the
    production bucket, and tests point it at a temporary directory.
11. **`event_url` falls back to `server_url` when empty, and the template uses
    `{{ event_url | default(server_url) }}`.** Version drift (#249) between the checked-out
    template and the installed `nf-client` then can't break rendering, which uses
    `StrictUndefined`. Until M8 moves `server_url` to the origin, both URLs are the
    Worker, so the rollout changes no behavior.
12. **The done-gate's production replay is an operator step.** It needs two production
    exports about a day apart, and those are R2 writes. The implement stage proves
    the same comparison on fixtures. The PR says "Refs #375", not "Closes".

## Design

### Files

| File | Change |
|---|---|
| `cf/src/types.ts` | `Env.MODE?: string` |
| `cf/src/index.ts` | `sinkOnly(env)`; the three event routes and `scheduled` branch on it; the run-event envelope sets `nextflow_log_uploaded` |
| `cf/src/sink-do.ts` | `TelemetryEnvelope.nextflow_log_uploaded?: true` |
| `cf/wrangler.jsonc` | `"MODE": "full"` var with a comment |
| `cf/test/sink-only.test.ts` | **new** |
| `cf/README.md` | a "Sink-only mode (M5/M8)" section |
| `src/nextflow_telemetry/migrations/versions/20261031_6b3e9d21_event_ingest.py` | **new** migration |
| `src/nextflow_telemetry/control_db.py` | `ingest_cursor.caught_up_at`; `ingest_rejects_tbl` |
| `src/nextflow_telemetry/services/transitions.py` | `_int`, `_str`, `_dict` and their use |
| `src/nextflow_telemetry/services/event_ingest.py` | **new** |
| `src/nextflow_telemetry/services/deadlines.py` | ingest-stale guard; `SweepResult.ingest_stale` |
| `src/nextflow_telemetry/config.py` | `EVENT_INGEST_ENABLED`, `EVENT_INGEST_SECONDS`, `EVENT_INGEST_URL` |
| `src/nextflow_telemetry/main.py` | start the ingester task in `lifespan` |
| `src/nextflow_telemetry/v2_import.py` | import `EVENTS_STREAM` from `services.event_ingest` (one spelling) |
| `src/nextflow_telemetry/v2_replay.py` | **new** (v2-only; deleted with ControlDO in M10) |
| `justfile` | `v2-replay` recipe |
| `deploy/onclappc02/.env.example` | the three `EVENT_INGEST_*` lines, commented, defaults shown |
| `packages/nf_client/src/nf_client/config.py` | `ClientConfig.event_url` |
| `packages/nf_client/src/nf_client/submission.py` | `event_url` in the template context |
| `packages/nf_client/src/nf_client/run_wrapper.py`, `cli.py` | `--telemetry-url` help text names `event_url` |
| `templates/submit_slurm.sh.j2`, `templates/submit_example.sh.j2` | `--telemetry-url {{ event_url \| default(server_url) }}` |
| `config/client-{alpine,anvil,bridges2}.yaml` | `event_url: "https://nf-telemetry.seandavi.workers.dev/api"` |
| `packages/nf_client/client-example.yaml` | document `event_url` |
| tests | see "Test plan" |

No new Python or npm dependency. Everything uses obstore 0.12 (`obstore.list(store,
prefix=, offset=)` and `obstore.get_async`, both checked against the installed
package), the stdlib `gzip`/`json`, SQLAlchemy Core and asyncpg.

### Worker (`cf/`)

`types.ts`:

```ts
/** "sink-only" after cutover (ADR-0013, M8): the event routes only archive. Anything else is full mode. */
MODE?: string;
```

`index.ts`:

```ts
/** Fails safe to full mode: only the exact string "sink-only" turns ControlDO and RunDO off for events. */
export function sinkOnly(env: Pick<Env, "MODE">): boolean {
  return env.MODE === "sink-only";
}
```

- **`POST /telemetry`:** parsing and `stripZoneIds` are unchanged. The block from
  `const ctl = control(c.env)` through the `completed` handling runs only when
  `!sinkOnly(c.env)`. `control(...)` is never called in sink-only mode. The
  `sink.write` and the `200` echo of the body are unchanged.
- **`POST /runs/:run_name/event`:**
  - `const live = !sinkOnly(c.env)`.
  - The orphan check (`getRunStatus` → 404) runs only when `live`. The 422/413 checks
    run in both modes.
  - The R2 puts run in both modes. `markLogUploaded` runs only when `live`.
  - `applyRunEvent` and the heartbeat/`wrapper_exited` RunDO block run only when `live`.
  - The `sink.write` envelope adds `...(logUploaded ? { nextflow_log_uploaded: true as const } : {})`.
  - The 201 body is unchanged.
- **`POST /task-logs`:** unchanged (it never touches ControlDO).
- **`scheduled`:** `if (sinkOnly(env)) { await sink(env).flush(); return; }`, then the
  existing body.

`sink-do.ts`: add to `TelemetryEnvelope`:

```ts
/** Set on run events whose nextflow_log attachment was stored; the ingester stamps nextflow_log_uploaded_at. */
nextflow_log_uploaded?: true;
```

`wrangler.jsonc` `vars`: `"MODE": "full"`, with a comment. Set it to `sink-only` at
cutover (M8): the event routes then only archive to R2, and the Postgres ingester
applies them.

### Migration `6b3e9d21` (revises `0d5e1a7c`)

```python
revision = "6b3e9d21"
down_revision = "0d5e1a7c"

def upgrade() -> None:
    op.add_column("ingest_cursor", sa.Column("caught_up_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "ingest_rejects",
        sa.Column("stream", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),          # 0-based line in the object
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("stream", "object_key", "line_no"),
        sa.CheckConstraint("line_no >= 0", name="ck_ingest_rejects_line_no"),
    )

def downgrade() -> None:
    op.drop_table("ingest_rejects")
    op.drop_column("ingest_cursor", "caught_up_at")
```

`control_db.py` mirrors it:
- `ingest_cursor_tbl` gains
  `Column("caught_up_at", DateTime(timezone=True), nullable=True)`, with the comment
  "start of the last poll that applied everything it listed; the sweep reads it".
- New `ingest_rejects_tbl` with the same columns, primary key and check. No extra
  index: the primary key covers lookups by object.

### `services/transitions.py`: coercion

```python
_INT4_MIN, _INT4_MAX = -(2**31), 2**31 - 1

def _int(v: Any) -> int | None:
    """Event values bound for integer columns. Bools and non-numbers become None; a finite
    float is truncated toward zero; anything outside int4 becomes None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        v = int(v)
    return v if isinstance(v, int) and _INT4_MIN <= v <= _INT4_MAX else None

def _str(v: Any) -> str | None:
    """Event values bound for text columns: strings, and numbers (not bools) as text. NUL removed (Postgres text rejects it)."""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None

def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}
```

Where they're applied. Nothing else in the module changes:

- **`apply_weblog_event`:**
  - `run_name = _str(body.get("runName") or body.get("run_name")) or ""`.
  - `event = _str(body.get("event")) or ""`.
  - `tag = _str(trace.get("tag")) if trace else None`, then
    `sample_id = tag.split(":")[0] if tag else None`.
  - `mark_running(..., _str(body.get("runId") or body.get("run_id")), ...)`.
  - In the `completed` branch, `wf = _dict(_dict(body.get("metadata")).get("workflow"))`.
    The failure reason is the first `_str(wf[k])` that isn't None among
    `errorMessage` and `errorReport`, else `"nextflow reported success=false"`, cut
    to `[:1000]`.
- **`record_task`:**
  - `status = _str(trace.get("status")) or "unknown"`.
  - `process = _str(trace.get("process")) or "unknown"`.
  - `exit_code = _str(trace.get("exit"))`, `task_hash = _str(trace.get("hash"))`,
    `attempt = _int(trace.get("attempt"))`, `error_action = _str(trace.get("error_action"))`.
- **`apply_run_event`:**
  - The first line is `run_name = _str(run_name) or ""`.
  - `pre_nextflow` writes `_int(event.get("wait_seconds"))` only when it isn't None.
  - `slurm_state` writes `_str(event.get("state"))` and `_str(event.get("reason"))`.
  - `wrapper_exited` uses `exit_code = _int(event.get("exit_code"))` for both the
    column and the `ok`/reason logic.

### `services/event_ingest.py` (new)

Module docstring: the R2 → Postgres event ingester (ADR-0013 §3, #375). It says lines
are applied once each, in key order then line order (SinkDO's write order), through
`transitions` only.

```python
EVENTS_STREAM = "telemetry/events"          # ingest_cursor.stream, and the key prefix below
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

def is_transient(exc: BaseException) -> bool
async def read_lines(store: ObjectStore, key: str) -> list[str]
async def keys_after(store: ObjectStore, after_key: str) -> list[str]
async def apply_line(conn: AsyncConnection, line: str) -> datetime
async def ingest_once(engine: AsyncEngine, store: ObjectStore, *, now: datetime,
                      until: tuple[str, int] | None = None) -> IngestResult
async def run_event_ingest(engine: AsyncEngine, store: ObjectStore, interval: timedelta) -> None
```

**`is_transient(exc)`** is True when `exc` is a `sqlalchemy.exc.DBAPIError` and
either `exc.connection_invalidated` is set, or
`getattr(exc.orig, "sqlstate", None) or ""` starts with one of
`_TRANSIENT_SQLSTATE_CLASSES`. SQLAlchemy's asyncpg adapter puts asyncpg's
`sqlstate` on `orig` (checked in SQLAlchemy 2.0.46). It's also True for `OSError` and
`TimeoutError`. Anything else is a property of the line.

**`read_lines(store, key)`:**
1. `gzip.decompress(bytes(await (await obstore.get_async(store, key)).bytes_async()))`,
   decoded as UTF-8 with `errors="replace"`.
2. Split on `"\n"` **only**, and drop one trailing empty string. Don't use
   `splitlines()`: `JSON.stringify` leaves U+2028 unescaped, so `splitlines()` would
   cut a line in two and shift every offset after it.

**`keys_after(store, after_key)`:**
1. `async for batch in obstore.list(store, prefix=EVENTS_PREFIX, offset=after_key or None)`
   collects every `o["path"]`.
2. Returns `sorted(k for k in keys if k > after_key and k.endswith(".ndjson.gz"))`.
   The filter and the sort are cheap, and they keep the result correct whatever the
   store returns.

**`apply_line(conn, line)`** returns `received_at`. It raises on a malformed envelope:
- `envelope = json.loads(line)`. It must be a dict, else `ValueError`.
- `received_at = datetime.fromisoformat(envelope["received_at"])`. A missing key,
  a non-string or a time with no timezone raises.
- `payload = envelope.get("payload")`. It must be a dict, else `ValueError`.
- `source == "weblog"`:
  `await transitions.apply_weblog_event(conn, payload, now=received_at)`.
- `source == "run_event"`:
  - `run_name = envelope.get("run_name")` must be a non-empty `str`.
  - If `envelope.get("nextflow_log_uploaded") is True`, call
    `await transitions.mark_log_uploaded(conn, run_name, now=received_at)`.
  - Then `await transitions.apply_run_event(conn, run_name, payload, now=received_at)`.
- Any other `source` raises `ValueError(f"unknown source {source!r}")`.

**`ingest_once(engine, store, *, now, until=None)`**: `now` is the poll's start time
(UTC-aware), and `until` is an inclusive bound `(object_key, line_count)` used by the
replay.

1. **Read the cursor.** In `engine.begin()`, run
   `insert into ingest_cursor (stream, object_key, line_offset) values (EVENTS_STREAM, '', 0) on conflict (stream) do nothing`.
   A missing row means "start at the beginning of the prefix". Then read
   `(object_key, line_offset)` into `expected`.
2. **Plan, lazily, one object at a time.**
   - First the cursor object itself, if `object_key != ""`. Read its lines. The end is
     `until[1]` when `until[0] == object_key`, else `len(lines)`. If
     `line_offset < end`, it's work item `(object_key, line_offset, lines[:end])`.
   - Then each `k` in `await keys_after(store, object_key)`. Stop when
     `until is not None and k > until[0]`. Read the lines, and cut them to `until[1]`
     when `k == until[0]`. The work item is `(k, 0, lines)`.
   - If `until` is set and `until[0] < object_key`, there's no work.
3. **Apply one work item `(k, start, lines)` per transaction.** In `engine.begin()`:
   1. Read `(object_key, line_offset)` from the cursor row
      `with_for_update(skip_locked=True)`. If no row comes back, or the values differ
      from `expected`, set `result.skipped = True` and return. Another ingester is
      busy, or moved the cursor.
   2. For `i in range(start, len(lines))`:
      - Inside `async with conn.begin_nested()`, run
        `received = await apply_line(conn, lines[i])`, then `result.lines += 1` and
        `result.last_received_at = received`.
      - `except Exception as exc`: if `is_transient(exc)`, re-raise, so the whole
        object rolls back and is retried on the next poll. Otherwise:
        - insert into `ingest_rejects` `(stream=EVENTS_STREAM, object_key=k, line_no=i,`
          `error=f"{type(exc).__name__}: {exc}"[:REJECT_ERROR_MAX] with "\x00" removed,`
          `line=lines[i] with "\x00" removed, rejected_at=now)`, with
          `on_conflict_do_update(index_elements=["stream", "object_key", "line_no"], set_={"error", "line", "rejected_at"})`;
        - log a warning `event_ingest.rejected` with `object_key`, `line_no` and
          `error`;
        - `result.rejected += 1`.
   3. Update the cursor row: `object_key=k, line_offset=len(lines), updated_at=now`.
   4. After the commit: `expected = (k, len(lines))` and `result.objects += 1`.
4. **Mark caught up.** When `until is None` and the loop finished without skipping,
   run `update ingest_cursor set caught_up_at = :now where stream = EVENTS_STREAM` in
   its own transaction, and set `result.caught_up = True`. Nothing else ever writes
   `caught_up_at`.

Every line is applied exactly once. A line's effects and the cursor that passes it
commit in the same transaction, and a crash rolls both back. That's what `record_task`
needs, because it counts.

**`run_event_ingest(engine, store, interval)`** loops forever:
- `now = datetime.now(timezone.utc)`, then `result = await ingest_once(engine, store, now=now)`.
- If `result.objects or result.rejected or result.skipped`, log
  `logger.info("event_ingest.poll", extra={**asdict(result), "lag_seconds": ...})`.
  `lag_seconds` is `(datetime.now(timezone.utc) - result.last_received_at).total_seconds()`,
  or None when nothing was applied.
- `except Exception: logger.exception("event_ingest.failed")`.
- `await asyncio.sleep(interval.total_seconds())`.

Cancelling the task stops it, the same pattern as `run_deadline_sweeps`.

### `services/deadlines.py`: the ingest-stale guard

- `SweepResult` gains
  `ingest_stale: bool = False  # liveness and backstop expiry held: the event ingester is behind`.
- In `sweep_deadlines`, after the grace block and before the `expired` select:

```python
cursor = (await conn.execute(
    select(ingest_cursor_tbl.c.caught_up_at).where(ingest_cursor_tbl.c.stream == EVENTS_STREAM)
)).first()
result.ingest_stale = cursor is not None and (
    cursor[0] is None or now - cursor[0] > policy.liveness / 2)
if result.ingest_stale:
    logger.warning("deadline.sweep.ingest_stale", extra={"caught_up_at": cursor[0]})
```

- When `ingest_stale`, the `expired` select's `where` is `runs_tbl.c.claim_expires_at <= now`
  only. Otherwise it keeps today's three-way `or_`.
- `EVENTS_STREAM` is imported from `.event_ingest`, and `ingest_cursor_tbl` from
  `..control_db`.
- `run_deadline_sweeps` also logs when `result.ingest_stale` is set.

### Config (`config.py`)

```python
# R2 → Postgres event ingester (ADR-0013 §3, #375). Off until cutover (M8).
# EVENT_INGEST_URL is the bucket root holding telemetry/events/ (r2://<bucket> or a
# local directory); required when enabled.
EVENT_INGEST_ENABLED: bool    # _as_bool(os.environ.get("EVENT_INGEST_ENABLED", "0"))
EVENT_INGEST_SECONDS: float   # float(os.environ.get("EVENT_INGEST_SECONDS", "30"))
EVENT_INGEST_URL: str         # os.environ.get("EVENT_INGEST_URL", "")
```

### `main.py`

In `lifespan`, after the existing tasks:

```python
if settings.EVENT_INGEST_ENABLED:
    if not settings.EVENT_INGEST_URL or settings.EVENT_INGEST_SECONDS <= 0:
        raise RuntimeError("EVENT_INGEST_ENABLED needs EVENT_INGEST_URL (r2://<bucket> or a directory) "
                           "and EVENT_INGEST_SECONDS > 0")
    events_store = blob_store(settings.EVENT_INGEST_URL, r2_account_id=settings.R2_ACCOUNT_ID,
                              r2_access_key_id=settings.R2_ACCESS_KEY_ID,
                              r2_secret_access_key=settings.R2_SECRET_ACCESS_KEY)
    tasks.append(asyncio.create_task(event_ingest.run_event_ingest(
        engine, events_store, timedelta(seconds=settings.EVENT_INGEST_SECONDS))))
```

`/health` and the routes are unchanged.

### `v2_replay.py` (new; v2-only, deleted in M10)

```python
PRODUCTION_DATABASE = "nf_telemetry"
DEADLINE_REASONS = (CLAIM_EXPIRED_REASON, BACKSTOP_REASON, LIVENESS_REASON)  # from services.deadlines
RUN_FIELDS = ("status", "run_id", "executor_job_id", "wait_seconds", "wrapper_exit_code",
              "last_known_slurm_state", "slurm_reason")
RUN_STAMPS = ("submitted_at", "started_at", "completed_at", "nextflow_log_uploaded_at")  # set vs unset only
JOB_FIELDS = ("status", "run_name", "retry_count", "failure_reason")
EXAMPLES = 20

def compare(before: dict[str, list[dict]], after: dict[str, list[dict]],
            replay_runs: dict[str, dict], replay_jobs: dict[int, dict]) -> dict[str, Any]
async def replay(engine: AsyncEngine, before: Snapshot, after: Snapshot,
                 events: ObjectStore) -> dict[str, Any]
def main(argv: list[str] | None = None) -> None
```

Timestamps are compared only as set or unset. v2 stamps them when ControlDO is called,
and the ingester stamps them at SinkDO's `received_at` a few milliseconds later.

**`compare(before, after, replay_runs, replay_jobs)`.** `before` and `after` are
`to_pg_rows(...)` outputs. The checks:

- **Runs:** every run in `before["runs"]`, matched by `run_name`.
  - No AFTER row is a mismatch.
  - The AFTER status is terminal with `slurm_reason in DEADLINE_REASONS`, and the
    replay status isn't terminal: difference **`deadline`**. The replay doesn't run
    the sweep, and RunDO alarms aren't events.
  - Otherwise every `RUN_FIELDS` value is equal, and every `RUN_STAMPS` column is set
    in both or unset in both. Anything else is a mismatch.
- **Jobs:** every job in `before["jobs"]` with a non-null `run_name`, matched by `id`.
  - No AFTER row: difference **`archived`** (retired-version archive).
  - Its BEFORE run is in the `deadline` difference: **`deadline`**.
  - The AFTER `run_name` is non-null and not in BEFORE's runs: the job was retried and
    re-claimed by a run minted in the window, and claims aren't events. If the replay
    job is `pending`, has `run_name` None and the same `retry_count`, that's
    difference **`reclaimed`**. Otherwise it's a mismatch.
  - Otherwise every `JOB_FIELDS` value is equal. Anything else is a mismatch.
- **Report:**
  - `{"runs_compared", "jobs_compared", "differences": {"deadline", "reclaimed", "archived"}, "mismatches": <int>, "examples": [...]}`.
  - At most `EXAMPLES` examples, each
    `{"table", "key", "field", "replay", "v2"}`. A missing row uses
    `field: "<row>"`.

**`replay(engine, before, after, events)`:**
1. `after.manifest["events_cursor"]` must not be None, else `ValueError`.
2. `await v2_import.import_snapshot(engine, before, replace=True)`.
3. `result = await event_ingest.ingest_once(engine, events, now=datetime.now(timezone.utc), until=(c["object_key"], c["line_offset"]))`.
   `result.skipped` raises `RuntimeError`.
4. Read every `runs` row, and the `jobs` rows whose ids are in BEFORE, as dicts.
5. Return `compare(...)` plus `"lines_applied": result.lines`,
   `"lines_rejected": result.rejected` and
   `"clean": mismatches == 0 and result.rejected == 0`.

**`main`:** `python -m nextflow_telemetry.v2_replay BEFORE_DIR AFTER_DIR EVENTS_DIR`.
- `BEFORE_DIR` and `AFTER_DIR` are M6 export copies (`read_snapshot`).
- `EVENTS_DIR` is a local directory holding `telemetry/events/dt=.../*.ndjson.gz`,
  opened with `blob_store(str(EVENTS_DIR), r2_account_id="", r2_access_key_id="", r2_secret_access_key="")`.
- `SQLALCHEMY_URI` is read and normalized the same way as `v2_import.main`.
- It refuses with exit 2 when `make_url(uri).database == PRODUCTION_DATABASE`, or when
  the URI isn't PostgreSQL. `ValueError`/`OSError`/`RuntimeError` exit 2, as do
  `SQLAlchemyError` (with v2_import's message).
- It prints the report as JSON and exits 0 when clean, 1 otherwise.

`justfile` (v2 section):

```just
# M5 (#375): replay the events between two exports into $SQLALCHEMY_URI (a scratch database) and compare with the second.
v2-replay before after events:
	uv run python -m nextflow_telemetry.v2_replay {{before}} {{after}} {{events}}
```

### `nf-client`

- **`ClientConfig`:** add
  `event_url: str = ""` with the comment "Base URL (like `server_url`, ending in
  `/api`) for the run wrapper's events: the edge Worker (ADR-0013 §7). Empty means
  `server_url`."
- **`build_submission_context`:** add `"event_url": cfg.event_url or cfg.server_url`.
- **Templates:** both change `--telemetry-url {{ server_url }}` to
  `--telemetry-url {{ event_url | default(server_url) }}`. `-with-weblog {{ weblog_url }}`
  is unchanged.
- **Help text:** `run_wrapper.py`'s `--telemetry-url` help and `cli.py`'s
  `run-wrapper` option help say `ClientConfig.event_url` instead of `server_url`.
- **Config files:**
  - `config/client-{alpine,anvil,bridges2}.yaml` add
    `event_url: "https://nf-telemetry.seandavi.workers.dev/api"` under `server_url`.
  - `client-example.yaml` documents the field.
- **No `nf-client` version or API change.** `sanitized_config_yaml` now includes
  `event_url`, so `GET /daemons` shows which daemons have it. That's the drift check
  for #249.

### Endpoints, config, callers

- **Endpoints:**
  - There are no new endpoints.
  - On the Worker, the three event routes behave the same in full mode. The run-event
    envelope gains one key.
  - On the origin, `/telemetry` and `/api/runs/{run}/event` now answer 2xx for
    ill-typed values that used to raise 500 (#405).
- **Env:**
  - Worker: `MODE` (default `"full"`).
  - Service: `EVENT_INGEST_ENABLED`, `EVENT_INGEST_SECONDS`, `EVENT_INGEST_URL`, all off
    or empty by default.
  - With the defaults, a deployed service behaves as before, apart from the coercion
    and the sweep guard. The guard is inert with no cursor row.
- **Callers:**
  - `nf-client` and the SLURM template: as above.
  - `etl/`, the frontend, `health_report.py`, the add-study bot and `contract/`: none.
  - `EventArchive`: none (the extra key is ignored).

## Acceptance criteria

- [ ] `cf`: in sink-only mode, with `CONTROL` and `RUN` removed from the env:
  - `POST /telemetry` returns 200 with the body echoed;
  - `POST /api/runs/{run}/event` returns 201 (heartbeat, and `wrapper_exited` with a
    `nextflow_log` for an unknown run);
  - `POST /api/task-logs` returns 201.
  The flushed SinkDO object holds the envelopes, the attachment is in R2, and the
  `wrapper_exited` envelope has `nextflow_log_uploaded: true`.
- [ ] `cf`: in full mode, a `wrapper_exited` with a `nextflow_log` for a known run
  writes an envelope with `nextflow_log_uploaded: true`. Without an attachment the key
  is absent.
- [ ] `cf`: `scheduled` in sink-only mode, with `CONTROL` removed, flushes SinkDO and
  doesn't throw.
- [ ] `cf`: `sinkOnly` is true only for the exact string `"sink-only"`.
- [ ] `cf`: `npm run typecheck` is clean and `npm test` passes.
- [ ] `alembic upgrade head`, then `downgrade 0d5e1a7c`, then `upgrade head` work.
  `alembic heads` shows one head. Columns match `control_db` (`ingest_cursor`,
  `ingest_rejects`).
- [ ] The ingester applies weblog and run events from a local store in key and line
  order with `now = received_at`. The cursor ends at `(last key, line count)`, and
  `caught_up_at = now`.
- [ ] A cursor at `(k, 2)` applies only lines 2 onward of `k`. A second poll applies
  nothing, and `run_task_counts` is unchanged (each line once).
- [ ] A `completed` followed by a late `MARK_COMPLETE` leaves the run terminal and
  the job not `completed`. A later `started`, `heartbeat` or `wrapper_started`
  doesn't flip a terminal run.
- [ ] A run event sets `liveness_deadline = received_at + LIVENESS_MINUTES`.
- [ ] `nextflow_log_uploaded: true` sets `runs.nextflow_log_uploaded_at = received_at`.
- [ ] Each of these is recorded in `ingest_rejects` with its `line_no`: a non-JSON line,
  a non-object payload, an unknown source, a missing or naive `received_at`, and a
  line that fails in Postgres (a run event whose `run_name` contains NUL and carries
  `nextflow_log_uploaded`). Every other line in the same object is applied, and the
  cursor passes the object.
- [ ] A float `wait_seconds` and a string `exit_code` are applied, not rejected:
  `wait_seconds` is truncated, and the run fails with `wrapper_exit_code` null.
- [ ] A transient error (`DBAPIError` with `sqlstate` `40P01`) propagates out of
  `ingest_once`. The cursor doesn't move and no reject row is written.
- [ ] `until=(k, n)` stops at line `n` of `k`, leaves the cursor at `(k, n)`, and
  doesn't set `caught_up_at`.
- [ ] While another connection holds the cursor row lock, `ingest_once` returns
  `skipped` and applies nothing.
- [ ] A payload containing U+2028 is one line.
- [ ] `transitions`: `_int`, `_str` and `_dict` behave as specified (bool, float,
  out-of-range int, NUL, non-dict metadata). Origin `POST /api/runs/{run}/event`
  returns 201 for `{"type":"pre_nextflow","wait_seconds":1.5}` and for
  `{"type":"wrapper_exited","exit_code":"x"}`.
- [ ] Sweep:
  - with a cursor row whose `caught_up_at` is null or older than liveness/2, a past
    liveness or backstop deadline isn't expired, a past claim is, and
    `ingest_stale` is True;
  - with a fresh `caught_up_at`, or no row, today's behavior holds.
- [ ] `v2_replay.compare` classifies a match, a `deadline` run and its jobs, a
  `reclaimed` job, an `archived` job and a real mismatch. An end-to-end `replay`
  over fixture exports plus fixture events reports `clean`. The CLI exits 2 on a
  database named `nf_telemetry`.
- [ ] `nf-client`:
  - `event_url` defaults to `""`;
  - the context's `event_url` falls back to `server_url`;
  - the rendered SLURM script has `--telemetry-url <event_url>` and still has
    `-with-weblog <weblog_url>`;
  - a context without `event_url` renders `--telemetry-url <server_url>`.
- [ ] `uv run pytest` and `uv run mypy src/nextflow_telemetry` pass, and so do the
  `nf-client` tests.
- [ ] No test touches the production R2 prefix or `pg_main`. They use temporary
  directories and testcontainers.

## Test plan

### `cf/test/sink-only.test.ts` (new; miniflare, as `export.test.ts`)

- **`beforeAll`:**
  - `POST /api/admin/reset`.
  - Register a workflow and one sample, reconcile, claim and submit (full mode via
    `SELF`). That gives a known run `R`.
- **`withEnv(overrides, path, init)`:** like `lifecycle.test.ts`.
  `worker.fetch(new Request(...), { ...env, ...overrides } as unknown as Env, ctx)`.
  The sink-only overrides are `{ MODE: "sink-only", CONTROL: undefined, RUN: undefined }`,
  so any stub creation throws and the request answers 500.
- **Sink-only event routes:**
  - Send a weblog `started`, a `heartbeat` run event, a `wrapper_exited` with a
    `nextflow_log` file for `r-unknown`, and a `/api/task-logs` upload. Check the
    statuses and bodies.
  - Then `runInDurableObject(sinkStub, (i: SinkDO) => i.flush())`, gunzip the
    object, and check the envelopes (`source`, `run_name`, `received_at`,
    `nextflow_log_uploaded`).
  - Check `env.STORE.get("nextflow-logs/r-unknown/nextflow.log")`.
- **Full mode:** a `wrapper_exited` with a `nextflow_log` for `R` via `SELF` produces
  an envelope with `nextflow_log_uploaded: true`. A heartbeat's envelope has no key.
- **Scheduled:** `worker.scheduled({} as ScheduledController, { ...env, MODE: "sink-only", CONTROL: undefined } as unknown as Env, ctx)`
  resolves, and the buffered event lands in R2.
- **`sinkOnly`:** `"sink-only"` → true; `"full"`, `undefined`, `"SINK-ONLY"` → false.

### Python

- **`tests/test_event_ingest.py` (new):**
  - Helpers write gzipped NDJSON envelopes into a `LocalStore(tmp_path)` under
    `telemetry/events/dt=2026-10-31/HHMMSS-<seq>.ndjson.gz`.
  - Rows are seeded with `insert` the way `test_deadlines.py` seeds them.
  - One test per ingester acceptance item above.
  - The transient test monkeypatches `transitions.apply_weblog_event` to raise
    `DBAPIError("x", None, _Orig())`, where `_Orig(Exception)` has
    `sqlstate = "40P01"`.
  - The lock test holds `select 1 from ingest_cursor for update` open on a second
    connection.
- **`tests/test_control_transitions.py`:** the coercion functions, and
  `apply_run_event`/`apply_weblog_event`/`record_task` with ill-typed values (no
  exception, coerced columns).
- **`tests/test_control_api.py`:** the two #405 origin requests return 201.
- **`tests/test_deadlines.py`:** the ingest-stale guard (three cases: no row, stale,
  fresh).
- **`tests/test_v2_replay.py` (new):**
  - `compare` cases built from `v2_tables()`/`write_export` (imported from
    `test_v2_import`).
  - End to end: BEFORE = `v2_tables()` with the cursor at an object holding a
    `MARK_COMPLETE` for `r-running` and a `wrapper_exited 0`. AFTER = the same tables
    with the v2 result, and its cursor at that object's end. The replay must report
    `clean`.
  - The CLI's production-name refusal.
- **`tests/test_control_plane_migration.py`:**
  - `test_columns_match_table_definitions` adds `ingest_rejects`.
  - A new `test_event_ingest_migration_round_trip` checks
    head → `0d5e1a7c` (`ingest_rejects` and `caught_up_at` gone) → head.

### `nf-client`

- `packages/nf_client/tests/test_config.py`: the `event_url` default.
- `packages/nf_client/tests/test_submission.py`:
  - the context fallback;
  - the rendered `submit_slurm.sh.j2` with `event_url` set;
  - a hand-built context without `event_url` renders `server_url`.

### Commands

```bash
uv run alembic heads
uv run pytest tests/test_event_ingest.py tests/test_control_transitions.py tests/test_control_api.py \
  tests/test_deadlines.py tests/test_v2_replay.py tests/test_v2_import.py tests/test_control_plane_migration.py
uv run pytest
uv run mypy src/nextflow_telemetry
uv run --project packages/nf_client pytest packages/nf_client/tests
cd cf && npm ci && npm run typecheck && npm test
```

Optional, needs Docker: `just pg-local` and then
`just contract http://127.0.0.1:8000 contract-token 20`. This shows the origin's event
routes are unchanged (Decision 1).

## Done-when and the production replay

The issue is done when "a replay of one day of production R2 events into a scratch
database gives the same job and run states v2 holds for those runs, apart from
documented differences". The documented differences are `deadline`, `reclaimed` and
`archived` above, and timestamps compared only as set or unset.

Two more can show up as mismatches. Both come from state that isn't carried by events,
each in a window of seconds, so expect at most a handful per replay and explain each one
by hand (#409 code-r0-1):

- **Claimed at BEFORE.** `executor_job_id`, and `claimed` → `submitted`, come from the
  origin call `/dispatch/submitted`, not from an event. A run claimed at the BEFORE export
  keeps a null `executor_job_id` in the replay, and stays `claimed` if its SLURM job was
  still queued at AFTER.
- **Applied just before AFTER's flush.** The Worker applies an event to ControlDO first
  and writes it to SinkDO second. An event between the two, at the moment of AFTER's
  export, is in AFTER's state but its line lands after AFTER's cursor, so the replay
  never applies it.

A mismatch outside these two cases is a real difference.

After merge, the operator:

1. Deploys the Worker (`cd cf && npx wrangler deploy`). It stays in full mode; this
   adds only the envelope key.
2. Runs `just v2-export /data/davsean/tmp/v2-exports` to get E1. About 24 h later,
   runs it again to get E2.
3. Copies the event partitions that span E1's and E2's cursors, read-only:
   `rclone copy r2:nf-telemetry/telemetry/events/dt=<D> <dir>/events/telemetry/events/dt=<D>`
   for each date D.
4. Creates a scratch database on `pg_main`, runs `alembic upgrade head`, then
   `just v2-replay <E1> <E2> <dir>/events`.
5. Posts the report in #375 and closes it. M7's 24 h shadow can reuse the same run.

## Risks

- **Rollback after the flip (M8's concern).**
  - In sink-only mode, ControlDO stops seeing events.
  - RunDO alarms armed before the flip still fire into ControlDO. Within 10 minutes
    v2 marks every in-flight run "presumed dead" in its frozen state.
  - Rolling back to full mode after that means replaying the window into ControlDO,
    or accepting duplicate dispatch of the retried jobs.
  - The M7 runbook should keep the time between the flip and the go/no-go short, or
    pause RunDO alarms (a ControlDO-side change, not made here).
- **A stale daemon still pointed at the Worker after M8.** It claims from paused v2
  registrations and gets 204, so nothing runs twice, but it does no work. `GET /daemons`
  (now showing `event_url`) and the health report show it (#249).
- **The cursor overlap from M6.** An event applied to ControlDO before the export and
  flushed after it is applied again by the ingester. Transitions are idempotent;
  `run_task_counts` and `run_failed_tasks` may count it twice. The replay doesn't
  compare those tables.
- **Old object names.** Objects written before #376's sequence suffix can sort out of
  write order within one second. The ingester starts at an export cursor, which is
  always newer. A replay of a day before that deploy may show spurious mismatches.
- **Ingest lag.** Ingest lag is FLUSH_MS 60 s plus EVENT_INGEST_SECONDS 30 s plus the
  time to apply. Measure it in M7 from `event_ingest.poll` `lag_seconds`. The target is
  under 2 minutes. If it's missed, lower `EVENT_INGEST_SECONDS`; the R2 list cost is
  negligible.
- **A permanently unreadable object** (a corrupt gzip) raises on every poll and stops
  the cursor. The sweep guard then holds liveness expiry, so the failure mode is "no
  deaths detected", not "false deaths". `event_ingest.failed` repeats in the log. R2
  puts are atomic, so this is unlikely. The fix is manual: move the cursor past the
  object.
- **The R2 keys.** The service's `R2_*` keys must be able to list and read
  `nf-telemetry/telemetry/events/`. The historical tier already reads that prefix with
  the same keys.
