# Spec #374: PG migration M4, deadline sweep with outage grace, retired-job archive

Issue: [#374](https://github.com/seandavi/nextflow_telemetry/issues/374). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), step M4. Blocked by
M1 (#371, merged as `fa0b1c2d`). Runs in parallel with M3 (#373, control endpoints) and
M6 (#376). M7 (#377) needs this step. ADR-0013 decision 4 is the design this spec
implements.

## Goal

Replace v2's per-run Durable Object alarms (`cf/src/run-do.ts`) and its daily 06:00
cron (`archiveRetiredJobs` in `cf/src/control-do.ts`) with code in the Python service:

1. **One deadline sweep.** Every `DEADLINE_SWEEP_SECONDS` (default 30), the sweep takes
   expired rows from `runs` with `FOR UPDATE SKIP LOCKED` and applies `RunDO.alarm`'s
   transitions:
   - an expired claim (`claim_expires_at`) goes back to pending and uses up no retry;
   - an expired submit backstop (`submit_deadline`) or liveness window
     (`liveness_deadline`) fails the run. Its in-flight jobs retry within budget or
     go to the dead-letter queue.
2. **Outage grace.** Each sweep records its time. If the gap since the previous sweep is
   longer than the liveness window, every open deadline is first pushed out by the gap.
3. **Retired-job archive.** Once a day at 06:00 UTC, and on demand through
   `POST /api/admin/archive-retired`, the jobs of retired workflow versions are written
   to R2 as gzipped NDJSON and then deleted. Same keys and the same rules as v2.

The deadline values are v2's (`CLAIM_TTL_MINUTES` 5, `SUBMIT_BACKSTOP_HOURS` 168,
`LIVENESS_MINUTES` 10). They come from config and accept fractions, so the contract
suite's fast clock works the way it does against v2 (`--var CLAIM_TTL_MINUTES:0.1`).

## Non-goals

- **Setting deadlines.** Claim, `submitted`, `wrapper_started` and `heartbeat` set
  `claim_expires_at`, `submit_deadline` and `liveness_deadline`. That's M3 (endpoints)
  and M5 (ingester). The M1 spec's "Notes for M3 and M4" give the rules. This step only
  reads, shifts and clears deadlines.
- **The v2 deprecated no-op routes** (`POST /dispatch/requeue-expired`,
  `/admin/expire-stale-runs`, `/admin/heartbeat-watchdog`). Their contract shapes are
  M3's. Nothing here triggers a sweep over HTTP.
- **The rest of v2's daily cron.** The ControlDO snapshot to R2 is replaced by Postgres
  backups (#84), and the `SinkDO` flush stays in the Worker (M5).
- **#350** (heartbeats keep arriving while the weblog has stalled). A run whose wrapper
  still heartbeats keeps its liveness deadline moving, so the sweep never kills it. That
  matches v2. The fix is the run-wrapper watchdog proposed in #350.
- **#367** (SLURM requeue after NODE_FAIL). The sweep closes the dead run as v2 did
  (liveness expiry, jobs retried). What the requeued batch sends later is a heartbeat
  or `wrapper_started` for a terminal run. M3/M5 must ignore those (see "Notes for M3
  and M5"). The `--no-requeue` template change stays with #367.
- **No change to** `cf/`, `nf-client`, the run wrapper, `etl/`, the frontend,
  `health_report.py` or the contract suite. The v1 routes and services
  (`services/lifecycle.py`, `routers/admin.py`, against the `v1_*` tables) are not
  touched.
- No ledger write to R2 on close (see Departures, 2).

## Departures from the issue and the design record

1. **The fast-clock contract run isn't this step's gate. The same scenarios run
   in-process.** The issue's done-gate is "contract-suite scenarios with the fast clock
   pass". `contract/test_timers.py` drives claim, `submitted` and run events over HTTP,
   and the Postgres service only gets those routes in M3, plus M5 for events. M4 runs in
   parallel with M3, so it can't pass that suite alone. This step proves the same three
   scenarios (claim expiry, liveness expiry with retry, liveness expiry to the DLQ) in
   `tests/test_deadlines.py`, against the M1 schema with an injected clock, plus the
   grace-period test. The HTTP fast-clock run (Test plan, "Deferred") is an acceptance
   item for whichever of M3/M4 merges second, and M7 runs it again on staging.
2. **Temporary duplicate transitions.** M3 owns the run and job state transitions
   (#373: "one service module that M4's sweep and M5's ingester can call"). That module
   doesn't exist yet. `services/deadlines.py` gets two private stand-ins, kept as small
   as v2's originals:
   - `_expire_claim` copies `ControlDO.expireClaim`.
   - `_fail_run` copies `ControlDO.closeRun(run, "failed", reason)` and its
     `sweepIncomplete`. It leaves out what the sweep doesn't need: the R2 ledger append
     (`ledger/YYYY/MM/<run>.jsonl`) and the `outcome` return value.

   Until they're replaced, Postgres has a second place that writes terminal run status.
   That goes against v2's "ONE place that writes job/run status" rule. It's safe because
   the sweep is off by default (`DEADLINE_SWEEP_ENABLED=0`) and M8 turns it on. The
   replacement rule:
   - If M3 is merged when this is implemented, call M3's functions and don't add the
     stand-ins.
   - Otherwise the second of the two PRs to merge deletes them and calls M3's.

   Either way, `tests/test_deadlines.py` passes unchanged. That keeps M3's functions
   honest about v2's semantics and puts the ledger write back.
3. **The sweep is off by default.** The issue says it "runs every 30–60 s". Until
   cutover, though, the new tables are empty or hold rehearsal data, and M8 step 4 is
   "start the sweep". So `DEADLINE_SWEEP_ENABLED` and `RETIRED_ARCHIVE_ENABLED` default
   to off, and M8 sets both to `1`.
4. **The archive's bucket path is config.** `RETIRED_ARCHIVE_URL`, default
   `r2://nf-telemetry/archive/jobs`, is v2's bucket and prefix. Tests use a local
   directory. The keys below that prefix are v2's.
5. **One new table, `deadline_sweep`.** The M1 spec left the sweep-time record to M4
   ("adds its own migration if it needs a table").

## Design

### Files

| File | Change |
|---|---|
| `src/nextflow_telemetry/migrations/versions/20261030_0d5e1a7c_deadline_sweep.py` | **new.** `revision = "0d5e1a7c"`, `down_revision = "fa0b1c2d"`. Creates `deadline_sweep` |
| `src/nextflow_telemetry/control_db.py` | add `deadline_sweep_tbl`, same as the migration |
| `src/nextflow_telemetry/config.py` | seven settings (see Config) |
| `src/nextflow_telemetry/services/deadlines.py` | **new.** `DeadlinePolicy`, `SweepResult`, `sweep_deadlines`, `run_deadline_sweeps`, and the stand-ins `_expire_claim`/`_fail_run` |
| `src/nextflow_telemetry/services/retired_archive.py` | **new.** `RetiredJobArchive`, `ArchiveResult`, `seconds_until_archive` |
| `src/nextflow_telemetry/routers/retired_archive.py` | **new.** `create_retired_archive_router(archive)`, which serves `POST /admin/archive-retired` |
| `src/nextflow_telemetry/models.py` | `ArchiveRetiredResponse` |
| `src/nextflow_telemetry/main.py` | a `lifespan` that starts and cancels the two background tasks; build `RetiredJobArchive`; include the router |
| `deploy/onclappc02/.env.example` | a commented block with the seven keys and their defaults |
| `tests/test_deadlines.py` | **new** |
| `tests/test_retired_archive.py` | **new** |
| `tests/test_control_plane_migration.py` | `test_columns_match_table_definitions` also checks `deadline_sweep` |

No new dependency. R2 writes go through DuckDB (already a dependency, `duckdb>=1.5.2`;
1.5.6 is locked), the way `services/event_archive.py` reads.

### Migration `0d5e1a7c`

Docstring: "Deadline sweep record (ADR-0013 decision 4, #374)." Upgrade:

```python
op.create_table(
    "deadline_sweep",
    sa.Column("id", sa.SmallInteger(), primary_key=True),
    sa.Column("last_swept_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("id = 1", name="ck_deadline_sweep_singleton"),
)
```

Downgrade: `op.drop_table("deadline_sweep")`. The migration inserts no row. The first
sweep inserts `(1, NULL)` itself, so a `metadata.create_all` test database behaves the
same as a migrated one.

`control_db.py`, appended at the end:

```python
# One row (id = 1): when the last deadline sweep committed. The outage grace
# period measures the gap from it (#374).
deadline_sweep_tbl = Table(
    "deadline_sweep", metadata,
    Column("id", SmallInteger, primary_key=True),
    Column("last_swept_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("id = 1", name="ck_deadline_sweep_singleton"),
)
```

Add `SmallInteger` to the existing `sqlalchemy` import.

### Config (`config.py`)

Add these fields to `Settings`, with a comment block in the style already there, and
their `os.environ` reads in `settings = Settings(...)`:

| Field | Type | Env read | Default |
|---|---|---|---|
| `CLAIM_TTL_MINUTES` | `float` | `float(os.environ.get("CLAIM_TTL_MINUTES", "5"))` | 5 |
| `SUBMIT_BACKSTOP_HOURS` | `float` | `float(os.environ.get("SUBMIT_BACKSTOP_HOURS", "168"))` | 168 |
| `LIVENESS_MINUTES` | `float` | `float(os.environ.get("LIVENESS_MINUTES", "10"))` | 10 |
| `DEADLINE_SWEEP_SECONDS` | `float` | `float(os.environ.get("DEADLINE_SWEEP_SECONDS", "30"))` | 30 |
| `DEADLINE_SWEEP_ENABLED` | `bool` | `_as_bool(os.environ.get("DEADLINE_SWEEP_ENABLED", "0"))` | off |
| `RETIRED_ARCHIVE_ENABLED` | `bool` | `_as_bool(os.environ.get("RETIRED_ARCHIVE_ENABLED", "0"))` | off |
| `RETIRED_ARCHIVE_URL` | `str` | `os.environ.get("RETIRED_ARCHIVE_URL", "r2://nf-telemetry/archive/jobs")` | v2's bucket and prefix |

The names and units are v2's (`cf/wrangler.jsonc` vars). A local value such as
`CLAIM_TTL_MINUTES=0.1` therefore means the same thing on both targets. The archive
reuses the existing `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`
settings. The fast-clock contract target uses `DEADLINE_SWEEP_ENABLED=1`,
`DEADLINE_SWEEP_SECONDS=1`, `CLAIM_TTL_MINUTES=0.1` and `LIVENESS_MINUTES=0.1`.

In `deploy/onclappc02/.env.example`, add a commented block titled "Control-plane
deadlines and retired-job archive (#374; enabled at cutover, M8)" that lists the seven
keys with their defaults, commented out like the "Historical metrics tier" block.

### `services/deadlines.py`

Module docstring: the sweep replaces RunDO alarms (ADR-0013 decision 4). Expired rows
are taken with `FOR UPDATE SKIP LOCKED`, and the outage grace works as described below.
`_expire_claim` and `_fail_run` are temporary copies of v2's transitions until M3's
module replaces them (Departure 2).

```python
CLAIM_EXPIRED_REASON = "claim expired without submitted confirmation"
BACKSTOP_REASON = "no wrapper activity within submit backstop"
LIVENESS_REASON = "presumed dead: heartbeats stopped"
RUN_TERMINAL = ("completed", "failed", "expired")
JOB_INFLIGHT = ("claimed", "submitted", "running")


@dataclass(frozen=True)
class DeadlinePolicy:
    claim_ttl: timedelta
    submit_backstop: timedelta
    liveness: timedelta
    sweep_interval: timedelta

    def __post_init__(self) -> None: ...
    @classmethod
    def from_settings(cls, s: Settings) -> DeadlinePolicy: ...


@dataclass
class SweepResult:
    skipped: bool = False            # another sweep holds deadline_sweep
    gap: timedelta | None = None     # since the previous sweep; None on the first
    shifted: int = 0                 # runs whose deadlines the grace period pushed out
    expired_claims: int = 0
    backstop_failures: int = 0
    liveness_failures: int = 0
    errors: int = 0                  # runs whose transition raised; retried next sweep


async def sweep_deadlines(conn: AsyncConnection, policy: DeadlinePolicy, now: datetime) -> SweepResult: ...
async def run_deadline_sweeps(engine: AsyncEngine, policy: DeadlinePolicy) -> None: ...
async def _expire_claim(conn: AsyncConnection, run_name: str, now: datetime) -> None: ...
async def _fail_run(conn: AsyncConnection, run_name: str, reason: str, now: datetime) -> None: ...
```

**`DeadlinePolicy`**
- `from_settings` builds the policy as `timedelta(minutes=s.CLAIM_TTL_MINUTES)`,
  `timedelta(hours=s.SUBMIT_BACKSTOP_HOURS)`, `timedelta(minutes=s.LIVENESS_MINUTES)`
  and `timedelta(seconds=s.DEADLINE_SWEEP_SECONDS)`.
- `__post_init__` raises `ValueError` if any of the four is ≤ 0. It also raises if
  `sweep_interval * 2 > liveness`, because a healthy sweep's own gap would then trip the
  grace period every time, and deadlines would never expire. The message names both
  settings.
- M3 should take its deadline lengths from `DeadlinePolicy.from_settings(settings)`, so
  the values are defined once. That's a recommendation, not built here.

**`sweep_deadlines(conn, policy, now)`** runs inside the caller's transaction. `now` is
timezone-aware UTC. Steps, in order:

1. `pg_insert(deadline_sweep_tbl).values(id=1).on_conflict_do_nothing(index_elements=["id"])`.
2. `select(deadline_sweep_tbl.c.last_swept_at).where(id == 1).with_for_update(skip_locked=True)`.
   No row means another sweep holds it, so return `SweepResult(skipped=True)`.
3. **Grace.** If `last_swept_at` isn't null, set `gap = now - last_swept_at`. If
   `gap > policy.liveness`, run:
   ```python
   update(runs_tbl)
     .where(func.num_nonnulls(runs_tbl.c.claim_expires_at, runs_tbl.c.submit_deadline,
                              runs_tbl.c.liveness_deadline) > 0)
     .values(claim_expires_at=runs_tbl.c.claim_expires_at + gap,
             submit_deadline=runs_tbl.c.submit_deadline + gap,
             liveness_deadline=runs_tbl.c.liveness_deadline + gap)
   ```
   (null + interval stays null). Set `shifted` to its rowcount, then
   `logger.warning("deadline.sweep.grace", extra={"gap_seconds": gap.total_seconds(), "shifted": shifted})`.
   A run whose deadline was still in the future at the previous sweep therefore keeps
   exactly the time it had left then. This UPDATE waits on row locks instead of skipping
   them, so no open run misses the shift.
4. **Expired rows.**
   ```python
   select(runs_tbl.c.run_name, runs_tbl.c.claim_expires_at, runs_tbl.c.submit_deadline,
          runs_tbl.c.liveness_deadline)
     .where(or_(runs_tbl.c.claim_expires_at <= now, runs_tbl.c.submit_deadline <= now,
                runs_tbl.c.liveness_deadline <= now))
     .order_by(runs_tbl.c.run_name)
     .with_for_update(skip_locked=True)
   ```
   The three partial indexes from M1 serve this. A row another transaction has locked
   (the ingester closing it, an operator) is skipped and seen again next sweep.
5. **Each row**, in `try: async with conn.begin_nested():`. `ck_runs_one_deadline`
   guarantees exactly one deadline column is set. Apply:
   - `claim_expires_at` → `_expire_claim(conn, run_name, now)`; `expired_claims += 1`.
   - `submit_deadline` → `_fail_run(conn, run_name, BACKSTOP_REASON, now)`;
     `backstop_failures += 1`.
   - `liveness_deadline` → `_fail_run(conn, run_name, LIVENESS_REASON, now)`;
     `liveness_failures += 1`.

   Then **disarm**: `update(runs_tbl).where(run_name == r).values(claim_expires_at=None, submit_deadline=None, liveness_deadline=None)`.
   That matches RunDO's `deleteAll()` after every alarm. It matters when the transition
   was a no-op, for example a claim deadline on a run that is no longer `claimed`.
   Otherwise the row would be selected again on every sweep. `except Exception`: log
   `logger.exception("deadline.sweep.transition_failed", extra={"run_name": r})` and
   `errors += 1`. The savepoint has rolled back, so the run keeps its deadline and the
   next sweep retries it.
6. `update(deadline_sweep_tbl).where(id == 1).values(last_swept_at=now)`. Return the
   result with `gap` set.

**`_expire_claim(conn, run_name, now)`** copies `expireClaim`:
1. `update(runs_tbl).where(run_name == r, status == "claimed").values(status="expired", completed_at=now, slurm_reason=CLAIM_EXPIRED_REASON, claim_expires_at=None, submit_deadline=None, liveness_deadline=None)`.
2. Only if that matched a row:
   `update(jobs_tbl).where(run_name == r, status == "claimed").values(status="pending", run_name=None)`.
   `retry_count` is unchanged, so an expired claim uses up no retry.

**`_fail_run(conn, run_name, reason, now)`** copies `closeRun(run, "failed", reason)`
and `sweepIncomplete`:
1. `update(runs_tbl).where(run_name == r, status.not_in(RUN_TERMINAL)).values(status="failed", completed_at=now, slurm_reason=reason, claim_expires_at=None, submit_deadline=None, liveness_deadline=None)`.
   An already-closed run keeps its status, as in v2.
2. Exhausted jobs fail and go to the dead-letter queue. This is one statement, run with
   `text()` and bind params `run_name`, `reason` and `now`:
   ```sql
   with failed as (
     update jobs j
        set retry_count = j.retry_count + 1, status = 'failed',
            failed_at = :now, failure_reason = :reason
       from workflows w
      where w.id = j.workflow_pk and j.run_name = :run_name
        and j.status in ('claimed', 'submitted', 'running')
        and j.retry_count >= w.max_retries
     returning j.id, j.sample_id, j.workflow_id, j.workflow_version)
   insert into dead_letter (job_id, run_name, sample_id, workflow_id, workflow_version, reason, created_at)
   select id, :run_name, sample_id, workflow_id, workflow_version, :reason, :now from failed
   on conflict (job_id) do nothing
   ```
3. Jobs within budget go back to pending:
   ```sql
   update jobs j
      set retry_count = j.retry_count + 1, status = 'pending', run_name = null,
          failed_at = null, failure_reason = null
     from workflows w
    where w.id = j.workflow_pk and j.run_name = :run_name
      and j.status in ('claimed', 'submitted', 'running')
      and j.retry_count < w.max_retries
   ```

Completed and failed jobs are never touched, so `MARK_COMPLETE` results stand.
`on conflict (job_id) do nothing` is v2's `insert or ignore`; see Risks.

**`run_deadline_sweeps(engine, policy)`** is the background loop:

```python
while True:
    try:
        async with engine.begin() as conn:
            result = await sweep_deadlines(conn, policy, datetime.now(timezone.utc))
        if result.shifted or result.expired_claims or result.backstop_failures \
                or result.liveness_failures or result.errors:
            logger.info("deadline.sweep", extra={**asdict(result), "gap": None if result.gap is None else result.gap.total_seconds()})
    except Exception:
        logger.exception("deadline.sweep.failed")
    await asyncio.sleep(policy.sweep_interval.total_seconds())
```

`CancelledError` isn't an `Exception`, so shutdown cancels the loop cleanly. A failed
sweep doesn't update `last_swept_at`, so a sweep that keeps failing for longer than the
liveness window is treated as an outage once it recovers.

### `services/retired_archive.py`

Module docstring: this is v2's `archiveRetiredJobs`, on Postgres. R2 writes go through
DuckDB `COPY … (FORMAT BLOB)`, using the same R2 secret pattern as `event_archive.py`.

```python
ARCHIVE_CHUNK = 5_000      # rows per object, as v2
ARCHIVE_HOUR_UTC = 6       # v2's cron "0 6 * * *"


@dataclass
class ArchiveResult:
    workflows_archived: int = 0
    jobs_archived: int = 0


def seconds_until_archive(now: datetime) -> float:
    """Seconds from `now` to the next ARCHIVE_HOUR_UTC:00 UTC strictly after it."""


class RetiredJobArchive:
    def __init__(self, engine: AsyncEngine, url: str, r2_account_id: str = "",
                 r2_access_key_id: str = "", r2_secret_access_key: str = "") -> None: ...
    async def archive(self, workflow_pk: int | None = None) -> ArchiveResult: ...
    async def run_daily(self) -> None: ...
    def _put(self, key: str, body: bytes) -> None: ...
```

**`__init__`** stores the arguments and `self._secret_dir = tempfile.mkdtemp(prefix="nf-telemetry-duckdb-")`.
It uses an isolated secret directory, for the reason `event_archive.py` gives.

**`archive(workflow_pk=None)`**
1. Targets: `select(workflows_tbl.c.id).where(status == "retired")`, filtered to
   `id == workflow_pk` when one is given, ordered by `id`. A non-retired `workflow_pk`
   yields no targets, and the result is `ArchiveResult()`, as in v2.
2. For each `pk`, set `n = 0`. Loop, with **one transaction per chunk**
   (`async with self.engine.begin() as conn`):
   1. `select(workflows_tbl.c.id).where(id == pk, status == "retired").with_for_update()`.
      If no row comes back, the version was re-activated, so stop this `pk`. The lock
      makes a concurrent `PATCH …/status` wait for the chunk.
   2. In-flight check:
      `select(exists().where(jobs.workflow_pk == pk, jobs.status.in_(JOB_INFLIGHT)))`.
      If it's true, stop this `pk`; tomorrow's run gets it (v2: "Retiring does not
      cancel in-flight runs").
   3. Chunk:
      `select(jobs_tbl).where(workflow_pk == pk).order_by(jobs_tbl.c.id).limit(ARCHIVE_CHUNK).with_for_update()`.
      If it's empty, stop.
   4. `dlq = select(dead_letter_tbl).where(dead_letter_tbl.c.job_id.in_(ids))`.
   5. Body:
      `b"".join(orjson.dumps(dict(r._mapping)) + b"\n" for r in [*rows, *dlq])`. Job
      lines first, then dead-letter lines, as v2 does. orjson writes `datetime` as
      RFC 3339.
   6. Key:
      `f"{self.url.rstrip('/')}/{pk}/{now:%Y-%m-%dT%H%M%S}{now.microsecond // 1000:03d}Z-{n}.ndjson.gz"`,
      with `now = datetime.now(timezone.utc)`. That is v2's
      `iso().replace(/[:.]/g, "")` stamp, plus `-{n}`, where `n` is the number of rows
      already archived for this `pk`.
   7. `await asyncio.to_thread(self._put, key, body)`. **R2 first:** if the put
      raises, the transaction rolls back and nothing is deleted. The exception
      propagates.
   8. Delete from `dead_letter` where `job_id` in `ids`, then delete from `jobs` where
      `id` in `ids`. The transaction then commits.
   9. `n += len(rows)`.

   When `n > 0`, add 1 to `workflows_archived` and `n` to `jobs_archived`.
3. `logger.info("retired_archive.done", extra=asdict(result))`, then return the result.

A crash between the put and the commit leaves a duplicate object on the next run, never
lost rows. That's the same trade v2 makes.

**`_put(key, body)`** is synchronous and runs in a thread:

```python
con = duckdb.connect(config={"secret_directory": self._secret_dir})
try:
    if self._r2[1]:
        con.execute("CREATE OR REPLACE SECRET r2 (TYPE r2, ACCOUNT_ID $a, KEY_ID $k, SECRET $s)",
                    dict(zip("aks", self._r2)))
    if not key.startswith("r2://"):
        Path(key).parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY (SELECT $1::BLOB) TO '{key}' (FORMAT BLOB, COMPRESSION gzip)", [body])
finally:
    con.close()
```

`body` is the uncompressed NDJSON, and DuckDB gzips it. Checked on duckdb 1.5.6 while
writing this spec: `COPY (SELECT $1::BLOB) TO 'x.ndjson.gz' (FORMAT BLOB, COMPRESSION gzip)`
with a bound `bytes` writes a file whose `gzip.decompress` equals the input. Passing
already-gzipped bytes to a `.gz` target compresses them twice, so don't. DuckDB's docs
(1.4 LTS, `sql/statements/copy`, "BLOB Options") document `FORMAT BLOB` with
`COMPRESSION`. `core_extensions/httpfs/s3api` documents S3 writes through `COPY … TO`
with multipart upload, and R2 secrets for `r2://` URLs. A key holds only digits, the
stamp and the configured prefix, so the f-string quoting is safe.

**`run_daily()`**

```python
while True:
    await asyncio.sleep(seconds_until_archive(datetime.now(timezone.utc)))
    try:
        await self.archive()
    except Exception:
        logger.exception("retired_archive.failed")
```

If the service is down at 06:00, that day is skipped, and the next run archives
everything that's eligible. v2's cron has the same property.

### Endpoint

`routers/retired_archive.py`:

```python
def create_retired_archive_router(archive: RetiredJobArchive) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/archive-retired", response_model=models.ArchiveRetiredResponse,
                 summary="Archive retired versions' jobs to R2 now instead of at the daily 06:00 UTC run",
                 dependencies=[Depends(require_role_or_token("admin"))])
    async def archive_retired(workflow_pk: int | None = None) -> dict[str, int]:
        return asdict(await archive.archive(workflow_pk))

    return router
```

- **Path, method and query** are v2's: `POST /api/admin/archive-retired?workflow_pk=`.
- **Response:** `200 {"workflows_archived": int, "jobs_archived": int}`.
  `models.ArchiveRetiredResponse(BaseModel)` has those two `int` fields.
- **Auth:** the existing `require_role_or_token("admin")`, meaning an admin session or
  `OPERATOR_TOKEN` (ADR-0013 decision 6). With neither, the route returns 401.
- **Errors:** if R2 fails, the route returns 500 and deletes nothing.

It's a separate router so this step doesn't edit `routers/admin.py`, which M3 is
rewriting. M10 can fold it in.

### `main.py`

1. Imports: `asyncio`, `contextlib.asynccontextmanager`,
   `collections.abc.AsyncIterator`, `services.deadlines`,
   `services.retired_archive.RetiredJobArchive`, and
   `routers.retired_archive.create_retired_archive_router`.
2. Before `app = FastAPI(...)`:
   ```python
   @asynccontextmanager
   async def lifespan(_: FastAPI) -> AsyncIterator[None]:
       tasks: list[asyncio.Task[None]] = []
       if settings.DEADLINE_SWEEP_ENABLED:
           policy = deadlines.DeadlinePolicy.from_settings(settings)
           tasks.append(asyncio.create_task(deadlines.run_deadline_sweeps(engine, policy)))
       if settings.RETIRED_ARCHIVE_ENABLED:
           tasks.append(asyncio.create_task(retired_job_archive.run_daily()))
       yield
       for task in tasks:
           task.cancel()
       await asyncio.gather(*tasks, return_exceptions=True)
   ```
   Pass `lifespan=lifespan` to `FastAPI(...)`. `engine` and `retired_job_archive` are
   module globals that exist by the time the app starts. An invalid `DeadlinePolicy`
   fails startup, but only when the sweep is enabled.
3. After `event_archive = …`, add
   `retired_job_archive = RetiredJobArchive(engine, settings.RETIRED_ARCHIVE_URL, settings.R2_ACCOUNT_ID, settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY)`.
   Also add `app.include_router(create_retired_archive_router(retired_job_archive), prefix="/api")`.

The service runs one uvicorn worker (`Dockerfile`, `--workers 1`), so there's one
sweeper. If that changes, the `deadline_sweep` row lock (`SKIP LOCKED`) keeps a second
sweeper from overlapping. A second archiver would only produce a duplicate object.

### Callers affected

None. `nf-client`, `etl/v2.py`, the frontend and `health_report.py` still talk to v2.
`POST /api/admin/archive-retired` is new on the Python service, and nothing calls it
until M9 repoints clients. With both flags off, which is the default, a running service
behaves exactly as before.

### Notes for M3, M5, M6 and M8 (not built here)

- **M3/M5: heartbeats on terminal runs.** Heartbeat, `wrapper_started` and every
  deadline-setting write must be guarded with `status not in ('completed','failed','expired')`.
  A requeued SLURM batch (#367) heartbeats a run the sweep has already failed. Without
  the guard, the update violates `ck_runs_terminal_closed` and the call or the ingester
  batch fails.
- **M3: lock order.** Lock `runs` before `jobs` in every transition, the order the
  sweep uses. That avoids deadlocks with it.
- **M3: the swap** (Departure 2). The sweep calls M3's equivalents of
  `_expire_claim(conn, run_name, now)` and `_fail_run(conn, run_name, reason, now)`.
  Matching those signatures, or `close_run(conn, run_name, "failed", reason, now)`,
  makes the swap an import change.
- **M6/M8: the switchover gap.** After importing the snapshot, set the sweep record to
  the export time:
  `insert into deadline_sweep (id, last_swept_at) values (1, :exported_at) on conflict (id) do update set last_swept_at = excluded.last_swept_at`.
  The first sweep after cutover then pushes every imported deadline out by the
  switchover gap, if that gap is longer than the liveness window. That's M8 step 4,
  "start the sweep, with the grace period covering the switchover gap". Nothing else is
  needed from this step.

## Acceptance criteria

- [ ] `alembic upgrade head` then `downgrade fa0b1c2d` then `upgrade head` work on a
      scratch database. `deadline_sweep` exists only at head, and `alembic heads`
      shows one head.
- [ ] `tests/test_control_plane_migration.py::test_columns_match_table_definitions`
      covers `deadline_sweep` and passes.
- [ ] **Claim expiry:** after the sweep, the run is `expired`, with
      `CLAIM_EXPIRED_REASON`, `completed_at = now` and no deadlines. Its claimed jobs
      are `pending`, with `run_name` null and `retry_count` unchanged.
- [ ] **Liveness expiry → retry** (`max_retries=1`): the run is `failed` with
      `LIVENESS_REASON`. The job is `pending` with `retry_count` 1, and there's no
      dead-letter row.
- [ ] **Liveness expiry → DLQ** (`max_retries=0`): the run is `failed`. The job is
      `failed`, with `failed_at` and `failure_reason`, and there's one `dead_letter`
      row carrying the run name and the reason.
- [ ] **Backstop expiry:** the run is `failed` with `BACKSTOP_REASON`, and its jobs are
      swept the same way.
- [ ] Completed jobs under a failed run stay `completed`.
- [ ] A deadline that hasn't been reached (`now` < deadline) changes nothing.
- [ ] **Grace:** with `last_swept_at = T` and a sweep at `T + 2 h`:
  - every open deadline moves by exactly 2 h and nothing expires;
  - `SweepResult.shifted` counts the runs;
  - a sweep after the shifted liveness deadline then fails that run.
- [ ] No shift when the gap equals the liveness window, and none on the first sweep
      (`last_swept_at` null).
- [ ] `last_swept_at` equals `now` after each sweep that wasn't skipped.
- [ ] A second sweep while the `deadline_sweep` row is locked returns `skipped=True`
      and changes nothing.
- [ ] A run row locked by another connection is skipped, and the next sweep after the
      lock is released expires it.
- [ ] A no-op transition (a claim deadline on a `submitted` run) clears the deadline and
      leaves the status alone.
- [ ] A transition that raises affects only its own run: that run keeps its deadline,
      the other runs are processed, and `errors == 1`.
- [ ] `DeadlinePolicy.from_settings(settings)` with default env gives 5 min, 168 h,
      10 min and 30 s. `DeadlinePolicy` raises `ValueError` when
      `sweep_interval * 2 > liveness` and when a value is ≤ 0.
- [ ] `run_deadline_sweeps` with a sub-second policy expires a claim in real time, and
      it stops when cancelled.
- [ ] With `DEADLINE_SWEEP_ENABLED=1`, starting the app (TestClient context) expires a
      seeded claim without any request. With the flag off, nothing is swept.
- [ ] **Archive:** a retired version's jobs, and their dead-letter rows, end up in
      `<url>/<pk>/<stamp>-0.ndjson.gz` and are gone from Postgres. The result is
      `{"workflows_archived": 1, "jobs_archived": N}`. Active versions are untouched.
- [ ] The archive skips a retired version that has an in-flight job, and archives it
      once the job is no longer in flight.
- [ ] With `ARCHIVE_CHUNK` patched to 2 and 5 jobs, the archive writes three objects,
      suffixed `-0`, `-2` and `-4`.
- [ ] When the put fails, the archive raises and every row stays in place.
- [ ] `seconds_until_archive` gives 60 at 05:59:00 UTC, 86400 at 06:00:00, and 82800 at
      07:00:00.
- [ ] `POST /api/admin/archive-retired` returns 401 without credentials, and 200 with
      the operator token and the response shape.
- [ ] `uv run pytest` passes. So do `uv run pytest tests/test_source_catalog.py` and
      `uv run mypy src/nextflow_telemetry`.
- [ ] **Deferred** (Departure 1), checked by the second of M3/M4 to merge: the
      stand-ins have been replaced by M3's functions, `tests/test_deadlines.py` still
      passes, and `contract/test_timers.py` passes against the Python service with the
      fast-clock env.

## Test plan

All Postgres tests use the session testcontainers database (`db_asyncpg_url`; the
autouse `clean_db` empties every table first) and pytest-asyncio's auto mode
(`async def` tests). Fixture: `engine`, which is
`create_async_engine(db_asyncpg_url)`, yielded and then disposed. The clock is always
passed in explicitly. `T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)`, and
`POLICY = DeadlinePolicy(timedelta(minutes=5), timedelta(hours=168), timedelta(minutes=10), timedelta(seconds=30))`.

**`tests/test_deadlines.py`**
- Helper `_seed(conn, run_name, status, deadline_column, deadline, jobs, max_retries=3) -> int`.
  It inserts a workflow (if absent), one sample per job, the run with the one deadline,
  and jobs given as `(status, retry_count)` under the run, using the `control_db`
  tables. It returns the workflow pk. Helper `_sweep(engine, now) -> SweepResult` runs
  `sweep_deadlines` in `engine.begin()`.
- `test_claim_expiry_requeues_without_burning_a_retry`: sweep at T0 (records the first
  sweep), then at the deadline + 1 s.
- `test_unexpired_deadline_is_left_alone`
- `test_liveness_expiry_retries_within_budget`
- `test_liveness_expiry_dead_letters`
- `test_backstop_expiry_fails_run`
- `test_failed_run_keeps_completed_jobs`
- `test_outage_grace_pushes_open_deadlines_out`: three runs, one per deadline column.
- `test_gap_within_liveness_does_not_shift`, `test_first_sweep_does_not_shift`
- `test_concurrent_sweep_is_skipped`: a second connection holds
  `select … from deadline_sweep for update`.
- `test_locked_run_is_skipped_until_released`
- `test_noop_transition_disarms_deadline`
- `test_failing_transition_is_isolated`: monkeypatch `deadlines._fail_run` (after the
  swap, M3's function) to raise for one run name.
- `test_policy_from_settings_defaults`, `test_policy_rejects_bad_intervals`
- `test_sweep_loop_fires_in_real_time`: policy of 0.6 s claim TTL, 0.05 s interval and
  1.2 s liveness. Seed a claim deadline at `now + 0.3 s`, start the task, and poll for
  up to 5 s. Then cancel the task.
- `test_lifespan_starts_sweep_when_enabled`: `monkeypatch.setenv` with
  `DEADLINE_SWEEP_ENABLED=1`, `DEADLINE_SWEEP_SECONDS=0.05` and `LIVENESS_MINUTES=0.01`.
  Seed an expired claim, then `request.getfixturevalue("integration_client")`, which
  re-imports `main` and enters the `TestClient` context. Poll the database for up to
  5 s.

**`tests/test_retired_archive.py`**
- `RetiredJobArchive(engine, str(tmp_path / "archive" / "jobs"))`. Objects are read with
  `gzip.open` and `orjson.loads` per line.
- `test_archives_and_purges_retired_jobs`, `test_skips_version_with_jobs_in_flight`,
  `test_active_and_unknown_versions_untouched`, `test_archive_chunks` (monkeypatch
  `retired_archive.ARCHIVE_CHUNK = 2`), `test_failed_put_keeps_rows` (the URL is a path
  under a regular file, so the put fails), and `test_seconds_until_archive`.
- `test_archive_endpoint_requires_operator`: `monkeypatch.setenv("OPERATOR_TOKEN", "t")`
  and `RETIRED_ARCHIVE_URL=<tmp>`, then `integration_client`. Check 401 without the
  header, and 200 plus the shape with `Authorization: Bearer t`.

**Commands**
- `uv run pytest tests/test_deadlines.py tests/test_retired_archive.py tests/test_control_plane_migration.py`
- `uv run pytest`
- `uv run pytest tests/test_source_catalog.py`
- `uv run mypy src/nextflow_telemetry`

**Deferred** (Departure 1; run by the second of M3/M4 to merge, and again in M7):
1. Start the Python service:
   `DEADLINE_SWEEP_ENABLED=1 DEADLINE_SWEEP_SECONDS=1 CLAIM_TTL_MINUTES=0.1 LIVENESS_MINUTES=0.1 OPERATOR_TOKEN=contract-token uv run uvicorn nextflow_telemetry.main:app --port 8000`
   on a scratch database at head, plus whatever M3/M5 need to apply events.
2. Run `CONTRACT_BASE_URL=http://127.0.0.1:8000/api CONTRACT_TOKEN=contract-token CONTRACT_ALLOW_WRITES=1 CONTRACT_FAST_CLOCK_SECONDS=20 uv run pytest contract/test_timers.py -v`.

## Risks

- **The R2 key may not allow writes.** The service's `R2_*` keys (GSM `cdsci-r2-*`)
  were provisioned to read the event archive. If they're read-only on `nf-telemetry`,
  the archive returns 500 and deletes nothing. Check them in M7 with
  `POST /api/admin/archive-retired` on staging before M8 sets
  `RETIRED_ARCHIVE_ENABLED=1`. Fixing the token is a monode Terraform change, not code.
  Also, `_put` runs `CREATE SECRET` for `r2://`, which autoloads DuckDB's `httpfs`. The
  container downloads it on first use, as the historical tier already does.
- **The grace period covers an outage of the service, not of the ingester.** If the API
  is up and the M5 ingester is stalled, heartbeats sit in R2, and the sweep fails live
  runs after 10 minutes. M5 should alarm on ingester lag, or keep the lag far below the
  liveness window. Making the sweep wait on the ingest cursor is a possible M5
  follow-up, not done here.
- **Ingester catch-up after an outage.** The shift gives each run the time it had left
  at the last sweep. For a healthy run that's about 8–9 minutes, with heartbeats every
  60 s and about 1 minute of lag. The ingester must work through the outage's backlog
  within that time.
- **Two writers of terminal state** until the swap (Departure 2). This is safe only
  while `DEADLINE_SWEEP_ENABLED=0`. M8 must not enable it before the swap; put that in
  the M7 runbook.
- **DLQ re-entry.** As in v2, `on conflict (job_id) do nothing` means a job that was
  dead-lettered, requeued (`resolved_at` set) and then fails again keeps its old,
  resolved row, so the unresolved DLQ count misses it. This is v2 behavior and is
  copied deliberately. #299 (M3, the DLQ admin) should decide whether a new failure
  re-opens the row. The swap then picks up M3's answer.
- **Alembic heads.** M3 and M6 may also add migrations on top of `fa0b1c2d`. The second
  to merge rebases its `down_revision` so that `alembic heads` shows one head. The
  revision id `0d5e1a7c` deliberately breaks the `fa0b1c2d` sequence, so the parallel
  branches don't choose the same id.
- **Clock.** The sweep uses the service's `datetime.now(timezone.utc)`, and M3 sets
  deadlines from the same host. If M3 uses database `now()` instead, the two clocks are
  the same machine, so the skew is negligible.
