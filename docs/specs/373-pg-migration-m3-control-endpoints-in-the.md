# Spec #373: PG migration M3, control endpoints in the Python service

Issue: [#373](https://github.com/seandavi/nextflow_telemetry/issues/373). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), the move of the control
plane to Postgres ([ADR-0013](../adr/0013-postgres-control-plane-edge-event-sink.md)), step
M3. It depends on M1 (#371, schema, merged) and M2 (#372, contract suite, merged), and runs
alongside M4 (#374, deadline sweep) and M6 (#376, export/import). M5 (#375, ingester) and M7
(#377, staging rehearsal) depend on it.

## Goal

The FastAPI service serves v2's control API (same paths, payloads and status codes as the
Worker in `cf/src/index.ts`) from the M1 tables in `control_db.py`, using SQLAlchemy Core.
On top of that it adds:

- claims that use `FOR UPDATE SKIP LOCKED`, ordered by `priority`, then `created_at`, then
  `id` (#298);
- reconcile as one set-based `INSERT … SELECT … ON CONFLICT DO NOTHING` (#298);
- **bulk registration**, `POST /api/samples/bulk`: NDJSON or TSV, `COPY` into a staging
  table, then a merge, all in one transaction. `nf-client add-samples` uses it when the
  server has it;
- the dead-letter routes: `GET /api/admin/dead-letter` (filterable), and
  `POST /api/admin/requeue-dead-letter` with filters (#299);
- the daemon fixes from #209: `started_at` resets on restart, a `stopped` status, and
  stale rows pruned when the list is read;
- **one transitions module**, `services/transitions.py`. It is the only code that writes job
  or run status. The routers, M4's sweep and M5's ingester all call it.

**Done when** the M2 contract suite passes against the service on a scratch database, both
locally (`just pg-local` + `just contract`) and in a new CI job. The timer tier stays skipped
until M4 lands (see Departures, 1).

## Non-goals

- **The deadline sweep, the outage grace period and `POST /admin/archive-retired`.** These
  are M4. M3 writes the deadline columns as each transition happens, but nothing expires a
  run until M4's sweep exists.
- **The R2 ingester, the sink-only Worker and `nf-client event_url`.** These are M5.
- **Export and import.** These are M6. That includes filling `readset_runs` for imported
  samples.
- **Repointing callers** (the add-study bot, the dashboard, `health_report.py`,
  `etl/v2.py`, `V2_API_URL`) is M9. None of them changes here, apart from `nf-client`'s
  bulk path.
- **`/submissions`, `/curated`, `/auth/*`.** The catalog routes are unchanged. They still
  write to v1 tables, with the v2 write-through, until M9.
- **Historical-tier queries.** The DuckDB queries over R2 in
  `services/process_metrics.py` stay as they are, apart from `running()`.
- **No new migration.** The M1 schema is enough.
- **No API for setting job priority.** It is set with SQL on onclappc02 (Design, Notes for
  operators). #298's operator route can come later if SQL turns out not to be enough.
- **Not ported:** `POST /admin/reset`, which is v2-only dev tooling.
- **No `failure_reason` in the job list.** That was #299 item 2; the #299 comment folds only
  the dead-letter list and the filtered requeue into M3.
- **No `nf-client` shutdown signal handler.** The server accepts `status: "stopped"`;
  having the daemon send it on SIGTERM is a separate client change.

## Departures from the issue and the design record

1. **Timers come with M4.** The issue's done-gate is "the M2 suite is green". Here that
   means green with `CONTRACT_FAST_CLOCK_SECONDS` unset, so the three `test_timers.py` tests
   are skipped. Claim expiry and liveness expiry need M4's sweep, and #374's own done-gate
   is those fast-clock scenarios.
2. **The origin serves the event routes too.** ADR-0013 §2 sends `/telemetry`,
   `/runs/{run}/event` and `POST /task-logs` to the edge. The origin serves them as well, and
   applies the events synchronously through the same transition functions. This has to
   happen, because the contract suite can't reach Postgres any other way before M5 exists.
   Events posted to the origin are **not** appended to the R2 event archive. Production
   reporters keep the edge URL (ADR §2 and §7), so the history in R2 stays complete. Log
   attachments and task logs posted to the origin are written to the same blob-store keys
   the Worker uses.
3. **Claim order across workflows.** v2 picks the alphabetically first `workflow_id` and
   then the oldest job within it. Here the claim picks the pending job with the highest
   `priority`, then the oldest `created_at`, then the lowest `id`, across every workflow the
   daemon asked for, and fills the batch from that job's workflow version. The issue asks
   for this order.
4. **Behaviour that changes from v2 because v2 was wrong, or because Postgres enforces
   constraints SQLite didn't:**
   - Dead-lettering a job that was requeued earlier resets its `dead_letter` row (new
     `reason`, `run_name` and `created_at`, `resolved_at = null`). v2 used
     `insert or ignore`, so the old row stayed resolved and the job dropped out of the
     queue without a trace.
   - `requeue-dead-letter` moves a job back to `pending` only if it is still `failed`. It
     still marks every matching dead-letter row resolved. v2 rewrote the job whatever its
     state, so a job that `reset-running` had already put back under a new claim could
     lose its `run_name`.
   - `reset-running` marks the dead-letter rows of the `failed` jobs it resets as resolved.
     Without that, `dead_letter_unresolved` keeps counting jobs that are pending again.
   - Retiring a version deletes the `dead_letter` rows of the pending jobs it deletes.
     Otherwise the foreign key from `dead_letter.job_id` blocks the delete.
   - `record_task` skips runs that don't exist, because `run_task_counts` and
     `run_failed_tasks` have foreign keys to `runs`. v2 counted tasks for unknown run names.
   - A weblog `started` on a run that is still `claimed` replaces the claim deadline with
     the submit backstop. In v2 the claim alarm fired, found nothing to expire, and left the
     run with no deadline at all.
   - Registration returns 409 when one of its runs already belongs to a different readset.
     This is the `readset_runs` primary key from M1, whose issue says the check used to be
     done by hand.
   - Daemon heartbeats require `hostname` (the column is `not null`), and `status` must be
     `idle`, `running` or `stopped`. v2 accepted any string. `nf-client` always sends
     both.
5. **`GET /samples/by-srr/{srr}` reads `readset_runs`.** A sample whose run list holds a
   non-run entry has no readset (ADR-0007), so it has no `readset_runs` rows, and this
   lookup returns 404 for it. v2 matched with `LIKE` on `ncbi_accession`. M1 named
   `readset_runs` as the structure for this lookup.
6. **`/metrics/processes/running` is built from the R2 archive.** v2 kept live per-process
   counters in SinkDO, and Postgres doesn't hold task events (ADR-0013 §5). Here the active
   runs come from Postgres and the per-process counts come from DuckDB over the archive, so
   the numbers trail by up to `EVENTS_ARCHIVE_REFRESH_SECONDS` plus the 60 s sink flush.
7. **The historical tier still reads membership and run attribution over HTTP from
   `V2_API_URL`** (`services/v2_api.py`, used by `cohorts/{id}/failures` and
   `EventArchive`). The service now serves the same `GET /runs`, `/cohorts`, `/samples` and
   `/workflows` shapes, so pointing `V2_API_URL` at the service's own `/api` is all a
   Postgres-backed deployment needs. The contract target does that now. M9 does it in
   production.
8. **One write token, through the existing gate.** Every control write depends on
   `deps.require_service`, which checks `DISPATCH_TOKEN`; the event routes are exempt, as
   in v2. That gives one bearer token for daemons and operators, as v2's `API_TOKEN` was
   (ADR-0013 §6). Deployments set it to the v2 token
   (`cdsci-nf-telemetry-v2-api-token`), so `nf-client` configs keep working after M9
   repoints them. A dashboard session cookie doesn't authorize control writes. That's the
   same as v2 today, and #179 covers it.
9. **The v1 control code is deleted now, not in M10.** The v1 routers are rewritten in
   place because the paths are the same, so the v1 service modules and tests that only
   they used have no callers left. They are deleted in this step (Design, Files). M10
   still drops the `v1_*` tables and whatever v1 code M9 leaves unused.
10. **422 bodies carry a string `detail`** on the control routers, as v2 does: the contract's
    `Detail` model needs `detail: str`. A route class scoped to those routers turns
    FastAPI's error list into a single string. The catalog routers keep FastAPI's default
    shape.
11. **Contract-suite changes.** As well as the three #394 fixes the issue asks for, the
    cohort route in `test_historical_tier` skips its descent when the target has no
    cohorts, the same way the other descents do. The service answers 404 for an unknown
    cohort, as v1 did. v2 turned that 404 into a 501.
12. **The R2 run ledger is dropped.** v2 wrote `ledger/YYYY/MM/{run}.jsonl` when a run
    closed. Nothing reads it (checked: `etl/`, `scripts/`, `src/`, the frontend). The
    outcome lives in Postgres, which #84 backs up.
13. **`Stats` has no `storage` field.** That was Durable Object bytes. The M2 mirror doesn't
    require it (M2 departure 5).

## Design

### Files

| File | Change |
|---|---|
| `src/nextflow_telemetry/services/transitions.py` | **new.** Every job and run status write: reconcile, claim, submitted, weblog and run events, close, expire, requeue, reset |
| `src/nextflow_telemetry/services/registry.py` | **new.** Samples (single and bulk), lookups, facets, workflows, job summary and job list |
| `src/nextflow_telemetry/services/control_reads.py` | **new.** Runs list and detail, cohorts, stats, dispatchability, the dead-letter list |
| `src/nextflow_telemetry/services/fleet.py` | **new.** Daemon heartbeat, list (with pruning), delete |
| `src/nextflow_telemetry/services/blobs.py` | **new.** The object store for task logs and run logs (obstore) |
| `src/nextflow_telemetry/readset.py` | **new.** ADR-0007 readset id, a twin of `nf_client/readset.py`, plus `normalize_runs` and `readset_id_for_runs` |
| `src/nextflow_telemetry/control_models.py` | **new.** Pydantic request and response models for the control routes |
| `src/nextflow_telemetry/routers/control_route.py` | **new.** `ControlRoute`, the APIRoute subclass that gives 422s a string `detail` |
| `src/nextflow_telemetry/routers/dispatch.py`, `daemons.py`, `workflows.py`, `samples.py`, `admin.py`, `runs.py`, `task_logs.py` | **rewritten** onto the new services |
| `src/nextflow_telemetry/routers/cohorts.py` | list, leaderboard and summary onto `control_reads`. The `/{id}/failures` handler and its models stay as they are |
| `src/nextflow_telemetry/services/process_metrics.py` | `running()` rewritten (below). Nothing else changes |
| `src/nextflow_telemetry/main.py` | `/telemetry` onto `transitions`, `/api/health` added, `TelemetryService` removed, router factories take what they need (below) |
| `src/nextflow_telemetry/config.py` | four settings (below), and the `DISPATCH_TOKEN` comment |
| `src/nextflow_telemetry/deps.py` | docstrings only: `require_service` now guards every control write |
| deleted: `services/dispatch.py`, `services/lifecycle.py`, `services/reconcile.py`, `services/telemetry.py`, `services/sample.py`, `services/workflow.py`, `services/cohort.py` | v1 control services with no callers left. Before deleting, confirm with `grep -rn "services\.\(dispatch\|lifecycle\|reconcile\|telemetry\|sample\|workflow\|cohort\)\b\|from \.\(dispatch\|lifecycle\|reconcile\|telemetry\|sample\|workflow\|cohort\) import" src scripts tests` that only the deleted files and tests import them. `services/collection.py` and `utils.py` stay, because `submission.py` uses them |
| deleted: `tests/test_integration.py`, `tests/test_e2e.py`, `tests/test_runs_router.py`, `tests/test_task_logs_router.py`, `tests/test_cohorts_router.py`, `tests/test_lifecycle.py`, `tests/test_dispatch_service.py` | tests of the deleted v1 routes and services. The contract suite and the new tests cover their behaviour |
| `tests/test_api.py` | delete `test_parse_tag_handles_bare_and_colon_forms` and `test_telemetry_happy_path_calls_ingest`. Keep the two health tests |
| `tests/test_readset.py`, `tests/test_control_transitions.py`, `tests/test_control_registry.py`, `tests/test_control_api.py` | **new** (Test plan) |
| `tests/conftest.py` | sets `BLOB_STORE_URL` to a temp directory before anything imports `config`, because `main` refuses to import without it |
| `tests/test_historical_metrics.py` | add `test_running_counts_tasks_of_running_runs` |
| `packages/nf_client/src/nf_client/client.py`, `cli.py` | `register_samples_bulk`, and `_ingest_rows` uses it, falling back to single POSTs |
| `packages/nf_client/tests/test_ingest.py` | bulk and fallback tests |
| `contract/support.py`, `contract/schemas.py`, `contract/conftest.py`, `contract/test_read.py` | #394 fixes and the cohort skip |
| `contract/test_support.py` | **new**, network-free (compat-r0-3) |
| `justfile` | `pg-local` recipe; `contract` gains a `fast_clock` argument; `help` lines |
| `.github/workflows/contract.yml` | a second job, `pg-local` |
| `deploy/onclappc02/fetch-secrets.sh` | writes `DISPATCH_TOKEN` from `cdsci-nf-telemetry-v2-api-token` |
| `pyproject.toml`, `uv.lock` | `uv add "obstore>=0.12.1,<0.13.0"` |
| `README.md` | the "HTTP contract suite" subsection: `CONTRACT_DISPOSABLE_HOST` and `just pg-local`. The "API reference" section: the catalog service's control routes are no longer "v1 copies", the table gains `POST /samples/bulk`, `GET /workflows/{pk}/jobs`, `GET /admin/dead-letter`, `POST /admin/requeue-dead-letter` and `/api/health`, and heartbeat is `PUT` |
| `justfile` (`run`), `.gitignore`, `env` | `just run` defaults `BLOB_STORE_URL` to `$PWD/.blobs` (ignored by git); the root `env` template sets `BLOB_STORE_URL=./.blobs` for the dev compose container |
| `README.md` Quick start, `CONTRIBUTING.md`, `AGENTS.md` | one line each: `BLOB_STORE_URL` is required |
| `deploy/onclappc02/README.md` | `.env` description names `BLOB_STORE_URL`; a one-time "add `BLOB_STORE_URL=r2://nf-telemetry` to `.env` before the first M3 deploy" step; the secrets table gains the `DISPATCH_TOKEN` row |
| `deploy/onclappc02/.env.example` | `DISPATCH_TOKEN` comment (set by `fetch-secrets.sh`, guards every control write); `BLOB_STORE_URL=r2://nf-telemetry` set explicitly; the other three settings as commented defaults |

`models.py` and the `v1_*` tables in `db.py` don't change. Some models in `models.py` lose
their last user; M10 prunes them.

### Configuration

Add to `Settings` in `config.py`, with the same `os.environ.get` style:

| Setting | Type | Default | Use |
|---|---|---|---|
| `CLAIM_TTL_MINUTES` | `float` | `5` | `runs.claim_expires_at = claimed_at + ttl` |
| `SUBMIT_BACKSTOP_HOURS` | `float` | `168` | `runs.submit_deadline = submitted_at + backstop` |
| `LIVENESS_MINUTES` | `float` | `10` | `runs.liveness_deadline = now + liveness`, on every wrapper sign of life |
| `BLOB_STORE_URL` | `str` | empty | Where task logs and run logs live: `r2://<bucket>`, or a local directory path for tests and the contract target. **No default:** `blob_store()` raises at startup when it is empty, so a dev or contract run can never write to the production bucket the Worker owns. Production sets `r2://nf-telemetry` in `deploy/onclappc02/.env` |

The values match `cf/wrangler.jsonc`. They are floats so a contract target can use
fractional minutes, as v2 does. **M3 owns these three deadline settings, and M4 reads them
rather than adding its own.** M4 adds its sweep interval itself.

`DISPATCH_TOKEN` keeps its semantics: when empty, writes are open and a warning is logged
once. Replace its comment with: "Bearer token for every control-plane write (ADR-0013 §6):
dispatch, registration, workflows, daemons, admin. The event routes are exempt. Set to the v2
API token (GSM cdsci-nf-telemetry-v2-api-token)." `deploy/onclappc02/fetch-secrets.sh`
gains `echo "DISPATCH_TOKEN=$(get cdsci-nf-telemetry-v2-api-token)"`. Production control
writes then aren't open once M3 is deployed.

### `readset.py`

```python
RUN_ACCESSION = re.compile(r"^[SED]RR\d+$")
def sha512t24u(data: bytes) -> str
def canonical_json(obj: Any) -> bytes
def readset_id(units: Iterable[str]) -> str            # copied verbatim from nf_client/readset.py
def normalize_runs(ncbi_accession: str) -> str
    # re.split(r"[;,\s]+", ...), strip, drop empties, dedupe, sort, ";".join (cf normalizeSrrs)
def readset_id_for_runs(normalized: str) -> str | None
    # None when empty or any entry fails RUN_ACCESSION; else readset_id("insdc.sra:" + r for r ...)
```

The module docstring names its twins: `packages/nf_client/src/nf_client/readset.py` and
`cf/src/readset.ts`.

### `services/blobs.py`

```python
from obstore.store import LocalStore, ObjectStore, S3Store

def blob_store(url: str, *, r2_account_id: str, r2_access_key_id: str, r2_secret_access_key: str) -> ObjectStore
    # "r2://<bucket>" -> S3Store(<bucket>, endpoint=f"https://{r2_account_id}.r2.cloudflarestorage.com",
    #                            access_key_id=..., secret_access_key=..., region="auto")
    # anything else (a path, or "file://<path>" with the scheme stripped) -> LocalStore(<path>, mkdir=True)
async def put_text(store: ObjectStore, key: str, data: bytes) -> None     # obstore.put_async
async def list_keys(store: ObjectStore, prefix: str) -> list[tuple[str, datetime]]
    # (path, last_modified) from `async for batch in obstore.list(store, prefix=prefix)`, sorted by path
async def get_text(store: ObjectStore, key: str) -> str | None
    # bytes(await (await obstore.get_async(store, key)).bytes_async()).decode("utf-8", "replace");
    # None on FileNotFoundError

NEXTFLOW_LOG_KEY = "nextflow-logs/{run_name}/nextflow.log"
WRAPPER_LOG_KEY = "nextflow-logs/{run_name}/wrapper_output.log"
TASK_LOG_KEY = "task-logs/{run_name}/{task_hash}/{log_type}"
```

The keys are the Worker's (`cf/src/index.ts`), so the origin reads what the edge writes. No
custom metadata is written, because `LocalStore` rejects attributes: `uploaded_at` comes from
`last_modified` for every object. These obstore 0.12.1 calls were checked against the
installed package: `S3Store(..., region="auto")`, `LocalStore(path, mkdir=True)`,
`put_async`, `get_async(...).bytes_async()`, `list` as an async iterator of batches of dicts
with `path` and `last_modified`, an empty listing for a missing prefix, and
`FileNotFoundError` for a missing key.

`main.py` builds one store at import,
`app.state.blob_store = blob_store(settings.BLOB_STORE_URL, r2_account_id=settings.R2_ACCOUNT_ID, ...)`,
and passes it to `create_runs_router` and `create_task_logs_router`.

### Transaction shape

Every service function takes an `AsyncConnection` first and leaves the transaction to the
caller. Routers use `async with engine.begin() as conn:` for writes and
`async with engine.connect() as conn:` for reads. M4's sweep and M5's ingester open their own
transaction and call the same functions in it.

Functions that change state take a keyword `now: datetime` (UTC-aware). Routers pass
`datetime.now(timezone.utc)`, and M5 passes each event's `received_at`. All SQL is
SQLAlchemy Core: `select`, `insert` (from `sqlalchemy.dialects.postgresql` where
`on_conflict_*` is needed), `update` and `delete` on the `control_db` tables. `text()` is
used only for the reconcile statement and the bulk merge statements given below. WHERE
clauses for optional filters are built in Python, and a clause is added only when its
filter is set. Status strings are module constants in `transitions.py`:
`RUN_TERMINAL = ("completed", "failed", "expired")` and
`JOB_INFLIGHT = ("claimed", "submitted", "running")`.

### `services/transitions.py`

Module docstring: "The one place that writes job and run status (ADR-0013 #373). Routers,
the deadline sweep (M4) and the event ingester (M5) call these functions inside their own
transaction. Every function is idempotent under a repeated call with the same arguments,
except `record_task`, which counts."

```python
SWEEP_REASON_DEFAULT = "run completed without MARK_COMPLETE"
RUN_FAILED_TASKS_MAX = 200

@dataclass(frozen=True)
class CloseResult:
    already_closed: bool
    swept: int

async def reconcile_jobs(conn) -> int
async def claim_batch(conn, *, limit: int, workflow_ids: list[str] | None, workflow_version: str | None, now: datetime) -> dict[str, Any] | None
async def mark_submitted(conn, run_name: str, executor_job_id: str | None, *, now: datetime) -> bool
async def mark_running(conn, run_name: str, run_id: str | None, *, now: datetime) -> None
async def complete_sample(conn, run_name: str, tag: str, *, now: datetime) -> int
async def record_task(conn, run_name: str, sample_id: str | None, trace: dict[str, Any], *, now: datetime) -> None
async def close_run(conn, run_name: str, terminal: str, reason: str | None, *, now: datetime) -> CloseResult | None
async def expire_claim(conn, run_name: str, *, now: datetime) -> bool
async def apply_weblog_event(conn, body: dict[str, Any], *, now: datetime) -> None
async def apply_run_event(conn, run_name: str, event: dict[str, Any], *, now: datetime) -> None
async def mark_log_uploaded(conn, run_name: str, *, now: datetime) -> None
async def requeue_dead_letter(conn, *, workflow_pk: int | None, collection: str | None, job_ids: list[int] | None, now: datetime) -> int
async def reset_jobs_to_pending(conn, workflow_pk: int, *, now: datetime) -> int
```

The deadline lengths are read from `settings` as `timedelta(minutes=settings.CLAIM_TTL_MINUTES)`
and so on.

**`reconcile_jobs`** runs one statement and returns its `rowcount`:

```sql
insert into jobs (sample_id, workflow_pk, workflow_id, workflow_version, status, retry_count)
select s.sample_id, w.id, w.workflow_id, w.version, 'pending', 0
  from workflows w cross join samples s
 where w.status = 'active'
   and (w.collections is null or exists (
         select 1 from collection_samples cs
          where cs.sample_id = s.sample_id and cs.collection_id = any(w.collections)))
   and (w.sample_key = 'sample_id'
        or s.id = (select min(s2.id) from samples s2 where s2.readset_id = s.readset_id))
on conflict (sample_id, workflow_pk) do nothing
```

This is v2's `reconcileJobs`. A readset-keyed registration gets one job per readset, on the
earliest sample row that carries it, and no job for a sample whose `readset_id` is null.

**`claim_batch`**:
1. **Pick.** `select j.workflow_pk … from jobs j join workflows w on w.id = j.workflow_pk where j.status = 'pending' and w.status = 'active'`,
   plus `j.workflow_id = any(:ids)` when `workflow_ids` is given and
   `j.workflow_version = :v` when `workflow_version` is given;
   `order by j.priority desc, j.created_at, j.id limit 1 for update of j skip locked`. No
   row means return `None`.
2. **Take.** `select j.id, j.sample_id from jobs j where j.status = 'pending' and j.workflow_pk = :pk order by j.priority desc, j.created_at, j.id limit :limit for update skip locked`.
   This uses `ix_jobs_pending_claim`.
3. **Mint.** `run_name = "r" + str(uuid7())`. Import `uuid7` from `uuid` on 3.14+, otherwise
   from `uuid_extensions`, as the deleted `services/dispatch.py` did.
4. **Insert the run.** Insert into `runs`: `status='claimed'`, `claimed_at=now`,
   `claim_expires_at = now + ttl`, and `workflow_id`, `workflow_version`, `workflow_pk` and
   `revision` from the workflow.
5. **Claim the jobs.** `update jobs set status='claimed', run_name=:run where id = any(:ids)`.
6. **Return** the v2 `ClaimedBatch` dict: `run_name`, `workflow_pk`, `workflow_id`,
   `workflow_version`, `repository_url`, `revision`, `params`, and
   `jobs: [{sample_id, ncbi_accession, metadata}]`. In each job, `sample_id` is the sample's
   `readset_id` when the workflow's `sample_key` is `readset_id`, and the md5 `sample_id`
   otherwise. Jobs are in take order.

With SQLAlchemy Core, `.with_for_update(skip_locked=True, of=jobs_tbl)` renders the two
locking clauses.

**`mark_submitted`** updates the run where `status = 'claimed'`: `status='submitted'`,
`submitted_at=now`, `executor_job_id`, `claim_expires_at=null`, and
`submit_deadline = case when liveness_deadline is null then now + backstop end`. With no
row updated it returns `False`. Otherwise it sets the run's `claimed` jobs to `submitted`
and returns `True`.

**`mark_running`** updates the run where `status in ('claimed','submitted')`:
`status='running'`, `run_id`, `started_at=now`, `claim_expires_at=null`, and
`submit_deadline = case when liveness_deadline is null then coalesce(submit_deadline, now + backstop) end`
(Departures, 4). It then sets the run's jobs that are `claimed` or `submitted` to `running`.

**`complete_sample`** is v2's statement:

```sql
update jobs set status = 'completed', completed_at = :now
 where run_name = :run and status not in ('completed', 'failed')
   and (sample_id = :tag or sample_id in (select sample_id from samples where readset_id = :tag))
```

It returns the rowcount. A swept job has `run_name` null or `failed`, so a late
`MARK_COMPLETE` can't flip it.

**`record_task`**:
- Upsert `run_task_counts (run_name, status, n=1)`, adding 1 to `n` on conflict, as an
  `insert … select … where exists (select 1 from runs where run_name = :run)`.
- `status` is `str(trace.get("status") or "unknown")`.
- When the status isn't `COMPLETED` and the run has fewer than `RUN_FAILED_TASKS_MAX`
  `run_failed_tasks` rows, insert one:
  - `process`: `str(trace.get("process") or "unknown")`;
  - `sample_id`;
  - `exit_code`: `None if trace.get("exit") is None else str(trace["exit"])`;
  - `task_hash`: `trace.get("hash")`;
  - `attempt`: `trace.get("attempt")`;
  - `error_action`: `trace.get("error_action")`;
  - `utc_time`: `now`.
- Unknown runs are skipped (Departures, 4).

**`close_run`**:
1. `select status from runs where run_name = :run for update`. No row means return `None`.
2. `already_closed = status in RUN_TERMINAL`.
3. If it isn't closed, update the run: `status=:terminal`, `completed_at=now`,
   `slurm_reason = coalesce(:reason, slurm_reason)`, and all three deadlines set to null.
4. Sweep, whether or not the run was already closed (as v2 does). Select the run's jobs in
   `JOB_INFLIGHT` joined to `workflows.max_retries`, `for update of jobs`.
   - Jobs with `retry_count < max_retries` get `retry_count = retry_count + 1`,
     `status='pending'`, `run_name=null`, `failed_at=null`, `failure_reason=null`.
   - The others get `retry_count = retry_count + 1`, `status='failed'`, `failed_at=now`,
     `failure_reason = reason or SWEEP_REASON_DEFAULT`. Each gets a `dead_letter` row
     (`job_id`, `run_name`, `sample_id`, `workflow_id`, `workflow_version`, `reason`,
     `created_at=now`), upserted on `job_id` so that it updates `run_name`, `reason` and
     `created_at` and sets `resolved_at=null`.
5. Return `CloseResult(already_closed, swept=<number of jobs selected>)`.

**`expire_claim`** updates the run where `status = 'claimed'`: `status='expired'`,
`completed_at=now`, `slurm_reason='claim expired without submitted confirmation'`, and the
three deadlines null. It then sets the run's `claimed` jobs to `pending` with
`run_name=null`, without touching `retry_count`, and returns whether the run was updated. M4
calls this.

**`apply_weblog_event`** ports `POST /telemetry` from `index.ts`:
- `run_name = str(body.get("runName") or body.get("run_name") or "")`.
- `event = str(body.get("event") or "")`.
- `trace` is `body["trace"]` when it is a dict, otherwise `None`.
- `sample_id = str(trace["tag"]).split(":")[0]` when the trace has a `tag`.

Then:
- `started` calls `mark_running(run_name, body.get("runId") or body.get("run_id"))`.
- `process_completed` calls `complete_sample` when there is a `sample_id`, the trace's
  `process` ends with `MARK_COMPLETE` and its `status` is `COMPLETED`. Any `process_completed`
  with a trace then calls `record_task`.
- `completed`: take `wf = (body.get("metadata") or {}).get("workflow") or {}`. When
  `wf.get("success") is False`, call
  `close_run(…, "failed", reason[:1000])`, where `reason` is the first of
  `wf.get("errorMessage")`, `wf.get("errorReport")` and `"nextflow reported success=false"`
  that isn't `None`. Otherwise call `close_run(…, "completed", None)`.

**`apply_run_event`** ports `applyRunEvent` and the deadline calls around it in `index.ts`.
`type = event.get("type")`:
- `wrapper_started`: `mark_submitted(run_name, None)` (the wrapper can get ahead of
  `/dispatch/submitted`), then the liveness refresh.
- `heartbeat`: the liveness refresh.
- `pre_nextflow`: `wait_seconds = event["wait_seconds"]` when it is present.
- `slurm_state`: `last_known_slurm_state = event.get("state")` and
  `slurm_reason = event.get("reason")`. These are the fields v2 reads.
- `wrapper_exited`: `wrapper_exit_code = event.get("exit_code")`, then
  `close_run(…, "completed" if exit_code == 0 else "failed", None if exit_code == 0 else f"wrapper exited {exit_code}")`.
- Other types change nothing.

The **liveness refresh** is one update. It always sets `last_heartbeat_at = now`. When
`status not in RUN_TERMINAL` it also sets `claim_expires_at = null`, `submit_deadline = null`
and `liveness_deadline = now + liveness`. Updates for an unknown run are no-ops.

**`mark_log_uploaded`** sets `nextflow_log_uploaded_at = now`.

**`requeue_dead_letter`**:
1. Select `dead_letter.id` and `job_id` where `resolved_at is null`, joined to `jobs`, with
   these filters as given: `jobs.workflow_pk = :pk`;
   `exists (select 1 from collection_samples where sample_id = dead_letter.sample_id and collection_id = :c)`;
   `dead_letter.job_id = any(:job_ids)`. Use `for update of dead_letter`.
2. Update those jobs where `status = 'failed'`: `status='pending'`, `retry_count=0`,
   `run_name=null`, `failed_at=null`, `failure_reason=null`.
3. Set `resolved_at=now` on the selected dead-letter rows.
4. Return the number of dead-letter rows resolved (v2's `requeued`).

With no filters it behaves as v2: every unresolved row.

**`reset_jobs_to_pending`**:
1. Mark the unresolved dead-letter rows of the workflow's `failed` jobs as resolved.
2. Then, for jobs with `workflow_pk = :pk and status in ('running','failed')`, set
   `status='pending'`, `run_name=null`, `retry_count=0`, `failed_at=null` and
   `failure_reason=null`.
3. Return the number of jobs updated.

**For M4:** the sweep calls `expire_claim` for an expired `claim_expires_at`. For an
expired `submit_deadline` it calls `close_run(conn, run, "failed", "no wrapper activity within submit backstop")`,
and for an expired `liveness_deadline`
`close_run(conn, run, "failed", "presumed dead: heartbeats stopped")`. These are v2's
`RunDO.alarm` reasons, and the contract's liveness test checks for "presumed dead". Nothing
here needs to be duplicated in M4.

**For M5:** the ingester calls `apply_weblog_event(conn, envelope["payload"], now=received_at)`
for `source == "weblog"`, and `apply_run_event(conn, run_name, envelope["payload"], now=received_at)`
for `source == "run_event"`. `record_task` counts every call, so the ingester's cursor must
apply each NDJSON line once.

### `services/registry.py`

```python
class ReadsetConflict(Exception):
    def __init__(self, runs: list[str]) -> None  # run accessions already in another readset

class BulkParseError(Exception): ...           # message names the line

@dataclass(frozen=True)
class RegistrationResult:
    received: int
    samples_inserted: int
    samples_updated: int
    collections_created: int
    memberships_added: int
    readset_runs_added: int

def parse_bulk(body: bytes, media_type: str, default_collection: str | None) -> list[SampleRegistration]
async def register_samples(conn, rows: Sequence[SampleRegistration], *, now: datetime) -> RegistrationResult
async def get_sample(conn, sample_or_readset_id: str) -> dict | None   # "RS." prefix -> readset_id, else sample_id; order by id limit 1
async def get_sample_by_run(conn, run_accession: str) -> dict | None   # join readset_runs; order by samples.id limit 1
async def samples_by_biosample(conn, biosample_id: str) -> list[dict]  # order by created_at desc
async def list_samples(conn, *, limit: int, offset: int, search: str | None, collection: str | None) -> tuple[list[dict], int]
async def collection_facets(conn) -> dict
async def register_workflow(conn, req: RegisterWorkflowRequest, *, now: datetime) -> tuple[dict, bool]   # (row, conflict)
async def list_workflows(conn, status: str | None) -> list[dict]       # order by workflow_id, version
async def get_workflow(conn, pk: int) -> dict | None
async def set_workflow_status(conn, pk: int, status: str, *, now: datetime) -> dict | None
async def set_workflow_revision(conn, pk: int, revision: str, *, now: datetime) -> dict | None
async def job_summary(conn, pk: int) -> dict | None
async def list_jobs(conn, pk: int, *, status: str | None, after: int, limit: int) -> list[dict]
```

**`parse_bulk`**:
- `application/x-ndjson`: one JSON object per non-blank line.
- `text/tab-separated-values`: `csv.DictReader(…, delimiter="\t")`. The header must include
  `sample_id` and `ncbi_accession`. It may include `biosample_id` and `collection`. Empty
  cells are `None`. TSV rows carry no `metadata`.
- Each row is validated with `SampleRegistration.model_validate`, and a row without its own
  `collection` gets `default_collection`.
- The body is decoded as UTF-8; a decode error is a `BulkParseError`.
- At most 20 errors are reported, as `"line {n}: {field}: {msg}"`, joined with `"; "`.
- More than `BULK_MAX_ROWS = 100_000` rows is a `BulkParseError` that the router maps to
  413.

**`register_samples`** is the one write path for `POST /samples` (one row) and
`POST /samples/bulk` (many). It runs in the caller's transaction:

1. In Python, for each row: `acc = readset.normalize_runs(row.ncbi_accession)` and
   `rs = readset.readset_id_for_runs(acc)`. Build the tuple
   `(line, sample_id, rs, acc, biosample_id, json.dumps(metadata or {}), collection)`.
2. `await conn.execute(text("create temp table sample_stage (line int, sample_id text, readset_id text, ncbi_accession text, biosample_id text, metadata text, collection text) on commit drop"))`.
   This first statement also opens the transaction on the driver connection.
3. `raw = await conn.get_raw_connection()`, then
   `await raw.driver_connection.copy_records_to_table("sample_stage", records=tuples, columns=[...])`.
   This was checked on 2026-10-30 against SQLAlchemy 2.0.46, asyncpg 0.31.0 and Postgres 16:
   the COPY joins the open transaction and rolls back with it.
4. **Readset conflict check.** One query over
   `(select distinct line, readset_id, unnest(string_to_array(ncbi_accession, ';')) as run from sample_stage where readset_id is not null)`.
   A run conflicts when `readset_runs` has it with a different `readset_id`, or when two
   staged rows give it different readset ids. If any run conflicts, raise
   `ReadsetConflict(sorted runs)[:20]`. The caller's transaction then rolls back.
5. **Samples.** Insert into `samples (sample_id, readset_id, ncbi_accession, biosample_id, metadata, created_at, updated_at)`
   from `select distinct on (sample_id) … from sample_stage order by sample_id, line desc`,
   so the last line wins. On conflict on `sample_id`, update `readset_id`,
   `ncbi_accession`, `biosample_id`, `metadata` and `updated_at`. That replaces them, nulls
   included, as v2 does. Use `returning (xmax = 0) as inserted` to count inserts against
   updates.
6. **Collections.** Insert `select distinct collection` (not null) into
   `collections (collection_id, source)`. `source` is `case when collection like 'PRJ%' then 'bioproject' when collection ~ '^[DES]RP[0-9]+$' then 'sra_study' else 'manual' end`,
   v2's rule. On conflict, update `updated_at=:now`. Count `returning (xmax = 0)`.
7. **Memberships.** Insert `select distinct collection, sample_id` into
   `collection_samples`, on conflict do nothing. Count with `rowcount`.
8. **Runs.** Insert `select distinct run, readset_id` into
   `readset_runs (run_accession, readset_id)`, on conflict do nothing. Count with
   `rowcount`. This is the "writes both" from M1, departure 2.

`ReadsetConflict` becomes 409:
`{"detail": "runs already belong to another readset: SRR1, SRR2"}`.

**`register_workflow`** ports `registerWorkflow`:
- `params` is stored as given. jsonb equality ignores key order, so a reordered
  re-registration matches.
- If `(workflow_id, version)` exists with different `params`, return `(existing, True)` and
  write nothing.
- Otherwise upsert. A new row gets `status='active'` and `sample_key='readset_id'`. The
  request's `status` is ignored, as in v2. On conflict, update `repository_url`, `revision`,
  `manifest_version`, `max_retries` (defaulting to 3 when omitted, as v2 does),
  `description`, `collections` and `updated_at`. `status`, `params` and `sample_key` never
  change, and M1's trigger enforces the last two.
- `collections`: when the field is absent from `req.model_fields_set`, keep the existing
  value (null on insert). An explicit `null` stores null. A list is stored as given (the
  request model has already rejected `[]`).
- The router's 409 message is v2's:
  `f"{workflow_id} {version} is registered with params {json.dumps(existing_params)}; params are the output contract, so register a new version to change them (ADR-0004)"`.

**`set_workflow_status`** updates `status` and `updated_at`. When the status is `retired` it
then deletes the dead-letter rows of the workflow's `pending` jobs, then those jobs. It
returns the row, or `None` when the workflow is unknown.

**`job_summary`** counts `select status, count(*) from jobs where workflow_pk = :pk group by status`.
That's an index-only scan on `ix_jobs_workflow_pk_status`; M1 measured it. `dead_letter` is
`count(*)` over `dead_letter join jobs on job_id` for the pk, resolved rows included, as v2
does (M2 Risks). It returns v2's `JobSummary` keys, with
`completion_pct = round(100 * completed / total, 2)` (0 when `total` is 0).

**`list_jobs`** ports `listJobs`:
- select `j.id as job_id`;
- `sample_key`: `case when w.sample_key = 'readset_id' then s.readset_id else j.sample_id end`;
- `j.status`, `j.completed_at`, `s.sample_id`, `s.readset_id`, `s.ncbi_accession`;
- `collections`: `array(select collection_id from collection_samples where sample_id = j.sample_id order by collection_id)`;
- `from jobs j join workflows w on w.id = j.workflow_pk left join samples s on s.sample_id = j.sample_id`;
- `where j.workflow_pk = :pk and j.id > :after`, plus the status filter when given;
- `order by j.id limit :limit`.

**Sample rows** are returned hydrated by one helper,
`_sample_columns() -> list[ColumnElement]`. It holds every `samples` column plus
`array(select collection_id from collection_samples where sample_id = samples.sample_id order by collection_id).label("collections")`.

**`list_samples`**:
- With a collection, join `collection_samples`.
- `search` adds `(s.sample_id ilike :p or s.readset_id ilike :p)` with `p = f"%{search}%"`.
- `order by s.id`, with `limit` and `offset`.
- `total` is `count(*)` over the same from-clause and where-clause.

**`collection_facets`** returns `{total: count(samples), collections: [{collection, count}]}`
grouped from `collection_samples`, in order of `count desc, collection_id`.

### `services/control_reads.py`

```python
RUN_STALE = timedelta(minutes=15)

def classify_run(row: Mapping[str, Any], now: datetime) -> str    # port of classifyRun, incl. "queued"
async def list_runs(conn, *, status: str | None, workflow_id: str | None, limit: int, offset: int) -> tuple[list[dict], int]
async def get_run(conn, run_name: str, now: datetime) -> dict | None
async def list_cohorts(conn) -> list[dict]
async def leaderboard(conn) -> list[dict]
async def cohort_summary(conn, collection_id: str, *, workflow_id: str | None, workflow_version: str | None, all_workflows: bool, now: datetime) -> dict | None
async def stats(conn) -> dict
async def dispatchability(conn, now: datetime) -> dict
async def active_run_names(conn) -> list[str]
async def list_dead_letter(conn, *, workflow_pk: int | None, collection: str | None, resolved: bool | None, limit: int, offset: int) -> tuple[list[dict], int]
```

- **`list_runs`** selects every `runs` column, so the deadline columns are on the wire too
  (`Run` is a loose object). It filters on `status` and `workflow_id`, orders by
  `claimed_at desc nulls last, run_name desc`, and adds `classification`. `total` is
  `count(*)` with the same filters.
- **`get_run`** returns the row and `classification`, plus:
  - `job_status_counts`: `{status: n}` from `jobs where run_name` (an empty dict when there
    are none);
  - `task_status_counts`: from `run_task_counts`;
  - `failed_tasks`: `process`, `sample_id`, `exit_code`, `task_hash`, `attempt` and
    `error_action` from `run_failed_tasks`, ordered by `utc_time desc, id desc`.

  The router adds `nextflow_log_available` and `wrapper_output_log_available` from
  `blobs.list_keys(store, f"nextflow-logs/{run_name}/")`.
- **`list_cohorts`** returns `collection_id`, `source`, `label`, `sample_count` (a count of
  `collection_samples`), `created_at` and `updated_at`, ordered by `created_at desc`.
- **`leaderboard`** is one grouped query:

  ```sql
  with m as (
    select cs.collection_id, j.sample_id, j.status, j.completed_at
      from collection_samples cs
      join jobs j on j.sample_id = cs.sample_id
      join workflows w on w.id = j.workflow_pk and w.status = 'active')
  select c.collection_id, c.source, c.label,
         (select count(*) from collection_samples x where x.collection_id = c.collection_id) as sample_count,
         count(distinct m.sample_id) filter (where m.status = 'completed') as samples_completed,
         count(distinct m.sample_id) filter (where m.status = 'failed') as samples_failed,
         count(distinct m.sample_id) filter (where m.status in ('claimed','submitted','running')) as samples_running,
         max(m.completed_at) filter (where m.status = 'completed') as last_completed_at
    from collections c left join m on m.collection_id = c.collection_id
   group by c.collection_id
  ```

  Python adds `samples_remaining = sample_count - samples_completed - samples_failed` and
  `completion_pct` (rounded to 2 places, 0 when the collection is empty). Rows are sorted
  by `(completion_pct, -sample_count)`, as v2 does.
- **`cohort_summary`** ports `cohortSummary`:
  - The workflow scope is `w.status = 'active'` unless `all_workflows` or `workflow_id` is
    given. `workflow_id` and `workflow_version` filter `jobs`.
  - It returns `collection_id`, `source`, `label`, `workflow_id`, `workflow_version`,
    `sample_count`, `samples_completed` (distinct samples with a completed job in scope),
    `total_jobs`, and `job_status_counts` with all six keys.
  - Also `completion_pct`, `failure_by_process: []` and `generated_at_utc`.
  - It returns `None` for an unknown collection.
- **`stats`** returns `samples`, `workflows`, `jobs_by_status`, `jobs_by_status_active`
  (jobs joined to active workflows), `runs_by_status` and `dead_letter_unresolved`.
  Statuses with no jobs are left out.
- **`dispatchability`** ports v2:
  - **Pending:** `workflow_pk`, `workflow_id` and `count(*)` of pending jobs on active
    workflows, `group by`.
  - **Active daemons:** those with `status <> 'stopped'` and
    `last_seen_at > now - 2 min`. Each one's filter is its comma-split `workflow_id`, or
    "every workflow" when that is blank.
  - **Stuck:** pending groups that no active daemon's filter covers, each with
    `reason: "no active daemon claims this workflow"`.
  - It returns `checked_at`, `active_daemons`, `stuck` and `stuck_pending_total`.
- **`list_dead_letter`** (#299) selects:
  - `d.id`, `d.job_id`, `j.workflow_pk`, `d.workflow_id`, `d.workflow_version`,
    `d.sample_id`, `s.readset_id`;
  - `collections` (the same `array(...)` as for samples);
  - `d.run_name`, `d.reason`, `d.created_at`, `d.resolved_at`;
  - `j.status as job_status`, `j.retry_count`.

  It reads `from dead_letter d join jobs j on j.id = d.job_id left join samples s on s.sample_id = d.sample_id`.
  The filters are `j.workflow_pk`; collection membership as an `exists`;
  `resolved=True` (`d.resolved_at is not null`) and `resolved=False` (`is null`). `None`
  means every row. It orders by `d.created_at desc, d.id desc`, with a `total`.

### `services/fleet.py`

```python
ACTIVE_WINDOW = timedelta(minutes=2)
PRUNE_AFTER = timedelta(days=7)   # ponytail: fixed; make it a setting if 7 days ever needs changing

async def heartbeat(conn, req: DaemonHeartbeatRequest, *, now: datetime) -> dict
async def list_daemons(conn, *, active_only: bool, now: datetime) -> list[dict]
async def delete_daemon(conn, agent_id: str) -> bool
def with_activity(row: Mapping[str, Any], now: datetime) -> dict
```

- **`heartbeat`** upserts on `agent_id`. It sets every request field, plus
  `active_runs = req.active_runs or 0`, `status`, `last_seen_at = now` and, on insert,
  `started_at = now`. On conflict it sets
  `started_at = case when daemons.status = 'stopped' or daemons.last_seen_at < :now - interval '2 minutes' then excluded.started_at else daemons.started_at end`.
  That is #209: a restart shows up as a gap or a stop. It returns `with_activity(row)`.
- **`list_daemons`** first runs `delete from daemons where last_seen_at < :now - PRUNE_AFTER`
  (#209), then lists them by `last_seen_at desc`, filtered on `is_active` when
  `active_only` is set. The router uses `engine.begin()`.
- **`with_activity`**:
  - `is_active = status != "stopped" and now - last_seen_at < ACTIVE_WINDOW`;
  - the reported `status` becomes `"stale"` when the daemon isn't active and isn't
    `stopped`.

  `stale` is never stored.

### `control_models.py`

These are Pydantic v2 models. The field sets match `contract/schemas.py`, which mirrors
`cf/src/schemas.ts`. Response row models use `ConfigDict(extra="allow")`, so every selected
column reaches the wire, as v2's `select *` does. Envelopes list exactly their fields.
Timestamps are `datetime`.

Requests:

| Model | Fields |
|---|---|
| `ClaimRequest` (extra ignore) | `limit: int = 50` (the router clamps it to 1..500), `workflow_id: str \| list[str] \| None = None`, `workflow_version: str \| None = None` |
| `SubmittedRequest` (extra ignore) | `run_name: str = ""`, `executor_job_id: str \| None = None`, `sample_ids: list[str] = []` (ignored) |
| `SampleRegistration` | `sample_id: str` (min_length 1), `ncbi_accession: str` (min_length 1, with a validator requiring a non-empty `readset.normalize_runs`), `biosample_id: str \| None = None`, `metadata: dict[str, Any] \| None = None`, `collection: str \| None = None` |
| `RegisterWorkflowRequest` | `workflow_id`, `version`, `repository_url`, `revision`: `str` (min_length 1); `manifest_version: str \| None = None`; `max_retries: int \| None = Field(None, ge=0)`; `status: Literal["active","paused","retired"] \| None = None`; `description: str \| None = None`; `params: dict[str, str \| bool \| int \| float] \| None = None`; `collections: list[Annotated[str, Field(min_length=1)]] \| None = Field(None, min_length=1)` |
| `WorkflowStatusRequest` | `status: Literal["active","paused","retired"]` |
| `WorkflowRevisionRequest` | `revision: str` (min_length 1) |
| `DaemonHeartbeatRequest` (extra ignore) | `agent_id: str`, `hostname: str`, `workflow_id`, `profile`, `nf_client_version`, `config_yaml`, `mode: str \| None = None`, `batch_size`, `max_concurrent_runs`, `active_runs: int \| None = None`, `status: Literal["idle","running","stopped"] = "idle"` |
| `CloseRunRequest` | `run_name: str \| None = None` |
| `RequeueDeadLetterRequest` | `workflow_pk: int \| None = None`, `collection: str \| None = None`, `job_ids: list[int] \| None = None` |

Responses:
- **Row models:** `Sample`, `SampleList`, `CollectionFacet`, `CollectionFacets`,
  `Workflow`, `JobListItem`, `JobList`, `JobSummary`, `ClaimedJob`, `ClaimedBatch`, `Run`,
  `RunList`, `FailedTask`, `RunDetail`, `RunEventAccepted` (`type: str`), `Cohort`,
  `CohortSummary`, `Daemon`, `Stats`, `TaskLog`, `TaskLogs`.
- **Result models:** `Reconciled`, `RequeuedDeadLetter`, `Submitted`, `RequeuedExpired`,
  `HeartbeatWatchdog`, `ExpiredStaleRuns`, `ClosedRun`, `ResetRunning`, `StuckWorkflow`,
  `Dispatchability`, `DeletedDaemon`, `BulkRegistered` (the six `RegistrationResult`
  fields), `DeadLetterItem` and `DeadLetterList` (`items`, `total`, `limit`, `offset`).

The `RunningProcessesResponse` and `HealthResponse` already in `models.py` are reused.

### `routers/control_route.py`

```python
class ControlRoute(APIRoute):
    """v2's wire errors: a 422 body is {"detail": "<message>"}, not FastAPI's error list."""
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]
        # wraps super().get_route_handler(); catches RequestValidationError and returns
        # JSONResponse({"detail": "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())}, 422)
```

Every control router is `APIRouter(prefix=..., tags=[...], route_class=ControlRoute)`. Write
routes declare `dependencies=[Depends(require_service)]`. FastAPI resolves those before it
validates the body, so an anonymous write with a bad body gets 401, not 422. This, the
string-detail conversion, an optional `Body(default=None)` model, `Form("")` fields that
accept an urlencoded body, and a route at `""` answering without a redirect were all
checked against FastAPI 0.115.14 on 2026-10-30.

### Endpoints

Every route is under `/api` unless it says otherwise. "Token" means
`Depends(require_service)`. `limit`, `offset` and `after` are plain `int` query parameters
that the handler clamps the way v2 does, rather than answering 422. Defaults are as listed.

| Method and path | Auth | Request | Response | Notes |
|---|---|---|---|---|
| `GET /health` (root) and `GET /api/health` | — | — | `HealthResponse`, 503 `HealthErrorResponse` | the existing handler, registered at both paths |
| `POST /telemetry` (root) | — | any JSON object (`await request.json()`, `{}` on parse failure) | the body, 200 | strips `availableZoneIds` as today; `transitions.apply_weblog_event` |
| `POST /dispatch/batch` | token | `ClaimRequest` | `ClaimedBatch`, or 204 with no body | `workflow_id` is normalised as in v2: a str or list, comma-split, stripped, empties dropped (#250) |
| `POST /dispatch/submitted` | token | `SubmittedRequest` | `Submitted` `{run_name, status: "submitted"}` | 404 `No claimed run with name '{run}' found` |
| `POST /dispatch/requeue-expired` | token | — | `{requeued_runs: 0}` | deprecated no-op; header `Deprecation: true` |
| `PUT /daemons/heartbeat` | token | `DaemonHeartbeatRequest` | `Daemon` | |
| `GET /daemons` and `GET /daemons/` | — | `?active_only=true` | `list[Daemon]` | prunes first |
| `DELETE /daemons/{agent_id:path}` | token | — | `{deleted}` | 404 `Agent '{id}' not found` |
| `POST /samples` | token | `SampleRegistration` | `Sample`, 201 | `register_samples([row])`, then `get_sample`; 409 on `ReadsetConflict` |
| `POST /samples/bulk` | token | raw body; `Content-Type` `application/x-ndjson` or `text/tab-separated-values`; `?collection=` sets the default collection | `BulkRegistered`, 200 | 415 for another type; 413 when the body is over 64 MiB or has more than 100,000 rows; 422 `BulkParseError`; 409 `ReadsetConflict`; one transaction |
| `GET /samples` | — | `limit` (100, 1..1000), `offset`, `search`, `collection` | `SampleList` | |
| `GET /samples/facets/collections` | — | — | `CollectionFacets` | |
| `GET /samples/by-srr/{srr}` | — | — | `Sample` | 404 `No sample found with SRR '{srr}'` |
| `GET /samples/by-biosample/{biosample_id}` | — | — | `list[Sample]` | |
| `GET /samples/{sample_id}` | — | md5 or `RS.` id | `Sample` | 404 `Sample '{id}' not found` |
| `POST /workflows` | token | `RegisterWorkflowRequest` | `Workflow`, 201 | 409 `Detail` on a params clash |
| `GET /workflows` | — | `?status=` | `list[Workflow]` | |
| `GET /workflows/{pk}` | — | — | `Workflow` | 404 `Workflow {pk} not found` (all `{pk}` routes) |
| `PATCH /workflows/{pk}/status` | token | `WorkflowStatusRequest` | `Workflow` | retiring purges pending jobs |
| `PATCH /workflows/{pk}/revision` | token | `WorkflowRevisionRequest` | `Workflow` | |
| `GET /workflows/{pk}/job-summary` | — | — | `JobSummary` | |
| `GET /workflows/{pk}/jobs` | — | `status`, `after` (0, ≥0), `limit` (1000, 1..5000) | `JobList` `{items, after, limit}` | |
| `POST /runs/{run_name}/event` | — | multipart: `event` (JSON string), optional `nextflow_log` and `wrapper_output_log` files | `RunEventAccepted`, 201 | the order of checks is below |
| `GET /runs` | — | `status`, `workflow_id`, `limit` (50, 1..500), `offset` | `RunList` | #118 |
| `GET /runs/{run_name}` | — | — | `RunDetail` | 404 `No workflow run with name '{run}'` |
| `POST /task-logs` | — | form fields `run_name`, `task_hash`, `log_type` (each `Form("")`), file `content` (`File(None)`) | `TaskLog`, 201 | the order of checks is below |
| `GET /task-logs/{run_name}/{task_hash:path}` | — | — | `TaskLogs` | below |
| `GET /cohorts` | — | — | `list[Cohort]` | |
| `GET /cohorts/leaderboard` | — | — | `list[Cohort]` | the rows carry the leaderboard fields |
| `GET /cohorts/{id}/summary` | — | `workflow_id`, `workflow_version`, `all_workflows` | `CohortSummary` | 404 `Cohort '{id}' not found.` |
| `GET /cohorts/{id}/failures` | — | unchanged | unchanged | `ProcessMetricsService.cohort_failures` |
| `GET /metrics/processes/running` | — | — | `RunningProcessesResponse` | `running()`, below |
| `GET /metrics/processes/{summary,…}` | — | unchanged | unchanged | historical tier, untouched |
| `POST /admin/reconcile-jobs` | token | — | `{jobs_created}` | |
| `POST /admin/reset-running` | token | `?workflow_pk=` (required) | `{reset}` | 422 `workflow_pk is required` |
| `POST /admin/close-run` | token | `?run_name=` or `CloseRunRequest` | `ClosedRun` `{run_name, already_closed, swept}` | `close_run(…, "completed", None)`; 422 `run_name is required`; 404 `No workflow run with name '{run}'` |
| `POST /admin/expire-stale-runs` | token | — | `{stale_runs_closed: 0, jobs_swept: 0}` | deprecated no-op |
| `POST /admin/heartbeat-watchdog` | token | — | `{checked_at, stale_after_minutes: 0, stale_runs_failed: 0, jobs_swept: 0, runs: []}` | deprecated no-op |
| `POST /admin/requeue-dead-letter` | token | optional `RequeueDeadLetterRequest` (`Body(default=None)`) | `{requeued}` | no body, or `{}`, means every unresolved row (v2) |
| `GET /admin/dead-letter` | — | `workflow_pk`, `collection`, `resolved` (bool), `limit` (100, 1..1000), `offset` | `DeadLetterList` | **new**, #299 |
| `GET /admin/dispatchability` | — | — | `Dispatchability` | |
| `GET /admin/stats` | — | — | `Stats` | |

**`POST /runs/{run_name}/event`**, in v2's order:
1. Parse `event` with `json.loads`. Failure is a 422 `event field is not valid JSON: {e}`.
2. A value that isn't a dict, or has a falsy `type`, is a 422 `event.type is required`.
3. An attachment on any `type` other than `wrapper_exited` is a 422
   `log attachments are only valid on wrapper_exited events; received type={type}.`
4. With an attachment, check that the run exists (`select 1 from runs`). If not, 404
   `No run '{run}'; refusing to store attachments as orphan.`
5. A `nextflow_log` over 16 MiB or a `wrapper_output_log` over 4 MiB is a 413.
6. Put the files at `NEXTFLOW_LOG_KEY` and `WRAPPER_LOG_KEY`.
7. In one `engine.begin()`: `mark_log_uploaded` when there is a `nextflow_log`, then
   `apply_run_event`.
8. Return 201 with `{run_name, type, nextflow_log_uploaded, wrapper_output_log_uploaded}`.

**`POST /task-logs`**:
1. An empty `run_name`, or `"null"`, is a 400 `run_name is required`.
2. `log_type` outside `command_sh`, `command_out` and `command_err` is a 422.
3. No `content` file is a 422 `content file is required`.
4. Content over 5 MiB is a 413 `Content exceeds size limit.`
5. `task_hash` becomes the short form, as `shortHash` does: `dir/rest[:6]` when `rest` is
   longer than 6.
6. The content is decoded as UTF-8 (errors replaced) and NUL characters are stripped.
7. Put at `TASK_LOG_KEY`, and return 201 with
   `{id, run_name, task_hash, log_type, content, uploaded_at: now}`.

**`GET /task-logs/{run}/{hash}`**:
- The prefix is `nextflow-logs/{run}/` when `hash == "nextflow_log"`, and
  `task-logs/{run}/{hash}/` otherwise.
- Each key, in sorted order, becomes `{id, run_name, task_hash, log_type, content, uploaded_at}`.
  For run logs `log_type` is the key's last segment with `.log` replaced by `_log`, and for
  task logs it is the last segment as is. `uploaded_at` is `last_modified`.
- An empty listing gives `logs: []`, with 200.

`id` is `_key_id(key)`, a port of `keyId`: `h = (31*h + ord(c)) & 0xFFFFFFFF` over the
characters, read as signed 32-bit, then `abs`.

### `ProcessMetricsService.running()`

1. `names = await control_reads.active_run_names(conn)` on `self.engine`. That's
   `runs where status = 'running'`.
2. With no names, return zeros and `by_process: []`.
3. Otherwise `rows = await self.archive.fetch(sql, {"runs": names})`, where `sql` is the
   existing `task_state` aggregation rewritten over the DuckDB `telemetry` view:
   `where list_contains($runs, run_name) and has_trace and event in ('process_submitted','process_started','process_completed')`,
   grouped by `run_name, task_id, process`.
4. Return `generated_at_utc`, `active_nf_runs = len(names)`, `total_running`,
   `total_queued` and `by_process` in the existing shape (Departures, 6).

### Router factories and `main.py`

- `create_dispatch_router(engine)`, `create_daemons_router(engine)`,
  `create_workflows_router(engine)`, `create_samples_router(engine)` and
  `create_admin_router(engine)` keep their signatures.
- `create_runs_router(engine, store)` and `create_task_logs_router(store)` take the blob
  store.
- `create_cohorts_router(engine, process_metrics)` is unchanged.
- `main.py`:
  - registers `healthcheck` with `@app.get("/health", …)` and `@app.get("/api/health", …)`;
  - replaces the `/telemetry` body with `async with engine.begin() as conn: await transitions.apply_weblog_event(conn, body, now=…)`
    over a plain `dict[str, Any]` body;
  - deletes `telemetry_service`.

### `nf-client` (the one caller that changes)

- `JobClient.register_samples_bulk(rows: list[dict]) -> dict | None` posts
  `"\n".join(json.dumps(r) for r in rows) + "\n"` to `samples/bulk`, with
  `Content-Type: application/x-ndjson` and `timeout=SUBMISSION_TIMEOUT_S`.
  - It returns `None` on 404 or 405: the server doesn't have the route (v2 answers 404).
  - For any other status it calls `raise_for_status()` and returns `response.json()`.
- `_ingest_rows` builds the row dicts first, with the same skip, limit and collection
  rules and the same counters: `{sample_id, ncbi_accession, collection?, biosample_id?}`,
  with keys left out where the value is `None`.
  - When `register_samples_bulk` returns `None`, it falls back to today's loop of
    `register_sample` calls.
  - The returned counts are the same either way.
- `add-samples` and `add-cmd` both go through `_ingest_rows`.

### Contract suite changes

- **compat-r0-2:** `run_event` sends `utc_time` as `datetime.now(UTC).isoformat()`. The
  weblog `utcTime` keeps `_now()` (Z form).
- **compat-r0-3:** `support.py` gains
  `LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})`. When writes are allowed,
  `target_from_env` checks each of `base_url` and `event_url`:
  1. A host in `PRODUCTION_HOSTS` raises
     `refusing writes against production host {host}: unset CONTRACT_ALLOW_WRITES`.
     This check comes first, so `CONTRACT_DISPOSABLE_HOST` can't override it.
  2. Otherwise, a host that isn't in `LOOPBACK_HOSTS` and isn't
     `environ.get("CONTRACT_DISPOSABLE_HOST")` raises
     `refusing writes against {host}: not loopback; set CONTRACT_DISPOSABLE_HOST={host} if it is disposable`.

  `conftest.py` registers an `offline: needs no target` marker. When every collected item is
  `offline`, `pytest_collection_modifyitems` returns before calling `target_from_env`.
- **code-r0-2:** the `Strict` base becomes
  `class Envelope(BaseModel): model_config = ConfigDict(extra="ignore")`, with the docstring
  "Zod z.object: unknown keys are stripped, not rejected." Every `Strict` subclass becomes an
  `Envelope` subclass. Unknown keys in envelopes are accepted, as Zod accepts them. `Detail`
  still requires a string `detail`, so FastAPI's list-shaped 422 still fails.
- **`test_read.py::test_historical_tier`:** for the cohort route, `pytest.skip("target holds no cohorts")`
  when `cohorts` is empty.
- **`contract/test_support.py`** (`pytestmark = pytest.mark.offline`, no network) has these
  tests:
  - `test_refuses_production_base`: the production base URL with `CONTRACT_ALLOW_WRITES=1`
    raises `ValueError`.
  - `test_refuses_production_event_url`: a local base with the production
    `CONTRACT_EVENT_URL` raises.
  - `test_refuses_non_loopback_without_disposable_host`.
  - `test_allows_named_disposable_host`.
  - `test_disposable_host_cannot_name_production`.
  - `test_allows_loopback`, parametrised over `http://127.0.0.1:8000`,
    `http://localhost:8787` and `http://[::1]:8000`.
  - `test_read_only_against_production_is_allowed`.
  - `test_requires_base_url`.
  - `test_run_event_utc_time_is_isoformat`: an `httpx.Client(transport=httpx.MockTransport(handler))`
    captures the multipart body, and the `utc_time` it carries parses with
    `datetime.fromisoformat` and ends with `+00:00`.
- **README:** add a `CONTRACT_DISPOSABLE_HOST` row to the variable table, and the
  `just pg-local` / `just contract http://127.0.0.1:8000 contract-token ""` recipe for the
  Postgres target.

### `justfile`

```just
# Postgres control plane for the contract suite (#373): a throwaway postgres:16 on
# 127.0.0.1:55432, migrated to head, then the API on :8000. Same env as the CI pg-local job.
pg-local:
	docker rm -f nf-pg-local >/dev/null 2>&1 || true
	docker run -d --rm --name nf-pg-local -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55432:5432 postgres:16-alpine
	until docker exec nf-pg-local pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
	d=$(mktemp -d); mkdir -p "$d/events" "$d/blobs"; \
	export SQLALCHEMY_URI=postgresql://postgres:postgres@127.0.0.1:55432/postgres DISPATCH_TOKEN=contract-token \
	  CLAIM_TTL_MINUTES=0.1 LIVENESS_MINUTES=0.1 BLOB_STORE_URL="$d/blobs" EVENTS_ARCHIVE_URL="$d/events" \
	  V2_API_URL=http://127.0.0.1:8000/api; \
	uv run alembic upgrade head && uv run uvicorn nextflow_telemetry.main:app --host 127.0.0.1 --port 8000
```

`pg_isready -h 127.0.0.1` waits past the image's socket-only init server.

`contract` becomes
`contract url="http://127.0.0.1:8787" token="contract-token" fast_clock="20":`, with
`CONTRACT_FAST_CLOCK_SECONDS={{fast_clock}}`. An empty value leaves the timer tier skipped,
because `target_from_env` treats `""` as unset. Add one `help` line for `pg-local`, and
change the `contract` line to "...against a running v2-local or pg-local".

### `.github/workflows/contract.yml`

Add a second job. The `v2-local` job is unchanged.

```yaml
  pg-local:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 2s --health-timeout 5s --health-retries 30
    env:
      SQLALCHEMY_URI: postgresql://postgres:postgres@127.0.0.1:5432/postgres
      DISPATCH_TOKEN: contract-token
      CLAIM_TTL_MINUTES: "0.1"
      LIVENESS_MINUTES: "0.1"
      V2_API_URL: http://127.0.0.1:8000/api
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v10.3.3
      - run: uv sync --group dev --frozen
      - run: uv run alembic upgrade head
      - name: Start the service (env matches `just pg-local`)
        run: |
          mkdir -p "$RUNNER_TEMP/events" "$RUNNER_TEMP/blobs"
          BLOB_STORE_URL="$RUNNER_TEMP/blobs" EVENTS_ARCHIVE_URL="$RUNNER_TEMP/events" \
            uv run uvicorn nextflow_telemetry.main:app --host 127.0.0.1 --port 8000 > "$RUNNER_TEMP/api.log" 2>&1 &
          for i in $(seq 60); do curl -sf http://127.0.0.1:8000/api/health && exit 0; sleep 1; done
          cat "$RUNNER_TEMP/api.log"; exit 1
      - name: Contract suite (timer tier waits for M4)
        env:
          CONTRACT_BASE_URL: http://127.0.0.1:8000
          CONTRACT_TOKEN: contract-token
          CONTRACT_ALLOW_WRITES: "1"
        run: uv run pytest contract -v
      - if: failure()
        run: cat "$RUNNER_TEMP/api.log"
```

M4 adds `CONTRACT_FAST_CLOCK_SECONDS` to this job once its sweep exists.

### Callers

| Caller | Affected? |
|---|---|
| `nf-client` daemons, run wrapper | No. They keep talking to v2 until M8 and M9, and the wire is the same |
| `nf-client add-samples`, `add-cmd` | Yes: they use the bulk route when the server has it, and fall back otherwise |
| `etl/v2.py`, `health_report.py`, the frontend, the add-study bot | No (M9) |
| The Worker (`cf/`) | No. `CATALOG_URL` still proxies the historical tier here, and those routes don't change |

### Notes for operators

To raise priority, run on onclappc02 (higher is claimed first, and retries keep their
priority):

```sql
update jobs set priority = 10
 where workflow_pk = 7
   and sample_id in (select sample_id from collection_samples where collection_id = 'PRJNA123');
```

## Acceptance criteria

- [ ] Against a scratch database at `alembic upgrade head`, the service started as in
      `just pg-local` passes `just contract http://127.0.0.1:8000 contract-token ""`.
      Nothing fails, and the only skips are the three `test_timers.py` tests and any
      empty-target descents.
- [ ] The `pg-local` CI job is green on the PR, and the `v2-local` job is still green with
      the contract changes.
- [ ] `uv run pytest contract/test_support.py` passes with no `CONTRACT_*` variables set.
- [ ] `CONTRACT_ALLOW_WRITES=1 CONTRACT_BASE_URL=http://10.0.0.5:8000 uv run pytest contract`
      exits 2 with the "not loopback" message. With `CONTRACT_DISPOSABLE_HOST=10.0.0.5` it
      gets past collection.
- [ ] Claims: two concurrent `claim_batch` calls on separate connections get disjoint jobs.
      Higher `priority` is claimed first, then older `created_at`, across workflows.
- [ ] `reconcile_jobs` is one statement. Run twice, it creates nothing the second time. It
      respects scope, and one job per readset for readset-keyed registrations.
- [ ] `POST /samples/bulk` loads NDJSON and TSV in one transaction:
  - a bad line returns 422 naming the line, and nothing is written;
  - a run already in another readset returns 409, and nothing is written;
  - an unsupported type returns 415;
  - more than 100,000 rows returns 413.
- [ ] `nf-client add-samples` uses `samples/bulk`, falls back to per-row POSTs on 404, and
      reports the same counts either way.
- [ ] `GET /admin/dead-letter` filters by `workflow_pk`, `collection` and `resolved`.
      `POST /admin/requeue-dead-letter` with `job_ids` or `collection` requeues only those,
      and with no body requeues every unresolved row.
- [ ] A job that is dead-lettered, requeued and dead-lettered again has one
      `dead_letter` row, unresolved, carrying the new reason.
- [ ] Daemons: a heartbeat after more than 2 minutes of silence, or after `stopped`, resets
      `started_at`. `stopped` daemons are not active. `GET /daemons` deletes rows unseen for
      7 days.
- [ ] Transitions set the deadline columns as described. A run never has two deadlines,
      and a closed run has none: M1's checks hold under every test.
- [ ] Every control write returns 401 without the bearer token when `DISPATCH_TOKEN` is
      set. `/telemetry`, `/api/runs/{run}/event` and `POST /api/task-logs` don't.
- [ ] `uv run pytest`, `uv run pytest tests/test_source_catalog.py`,
      `uv run mypy src/nextflow_telemetry` and
      `uv run --project packages/nf_client pytest packages/nf_client/tests` all pass.
- [ ] Measurements recorded in the PR description: 58,699 rows through `POST /samples/bulk`
      in one request, and `reconcile_jobs` over 76,000 samples × 3 active workflows, each
      with its wall time. The targets are under 60 s and under 10 s.

## Test plan

Unit and integration tests use the session testcontainers database (`db_asyncpg_url`;
`create_schema` builds the M1 tables with `create_all`, so the `workflows` trigger isn't
there). Service tests open `create_async_engine(db_asyncpg_url)` and call the functions
inside `engine.begin()`.

- **`tests/test_readset.py`**:
  - `readset_id_for_runs("SRR000001") == "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"`;
  - the ZELLER golden vector from `packages/nf_client/tests/test_readset.py`;
  - `readset_id_for_runs("SRR1;Not_applicable") is None`;
  - `normalize_runs("SRR2, SRR1;SRR1") == "SRR1;SRR2"`.
- **`tests/test_control_transitions.py`**:
  - `test_claim_order_priority_then_age`;
  - `test_claim_skip_locked_disjoint`: two engines, the first holds its transaction open
    while the second claims;
  - `test_claim_filters_and_comma_ids` (comma ids at the router level, in the API test);
  - `test_claim_sets_claim_deadline`;
  - `test_submitted_swaps_to_backstop`;
  - `test_heartbeat_sets_liveness_and_clears_others`;
  - `test_started_on_claimed_run_replaces_claim_deadline`;
  - `test_close_run_retries_within_budget_and_dead_letters_beyond`;
  - `test_close_run_idempotent`;
  - `test_redead_letter_resets_row`;
  - `test_late_mark_complete_never_flips`;
  - `test_expire_claim_burns_no_retry`;
  - `test_weblog_completed_failure_reason` (`errorMessage: None` falls through to
    `errorReport`);
  - `test_record_task_counts_and_caps`, and that it ignores an unknown run;
  - `test_requeue_dead_letter_filters` (by `workflow_pk`, `collection` and `job_ids`; only
    `failed` jobs move);
  - `test_reset_running_resolves_dead_letter`;
  - `test_reconcile_scoped_readset_deduped_idempotent`.
- **`tests/test_control_registry.py`**:
  - `test_register_samples_single_and_bulk_same_path`;
  - `test_registration_writes_readset_runs_and_membership`;
  - `test_collection_source_rule`;
  - `test_readset_conflict_rolls_back`;
  - `test_bulk_last_line_wins_for_duplicate_sample_id`;
  - `test_parse_bulk_ndjson_and_tsv`;
  - `test_parse_bulk_errors_name_lines`;
  - `test_bulk_ten_thousand_rows`;
  - `test_workflow_bundle_conflict_and_reordered_params`;
  - `test_collections_omitted_keeps_scope_null_widens`;
  - `test_retire_purges_pending_jobs_and_their_dead_letters`;
  - `test_job_summary_and_keyset_paging`.
- **`tests/test_control_api.py`**: a `control_client` fixture sets these with
  `monkeypatch.setenv`, then imports `nextflow_telemetry.main` fresh, as
  `integration_client` does:
  - `SQLALCHEMY_URI=db_asyncpg_url`, `TELEMETRY_SKIP_DB_INIT=1`;
  - `DISPATCH_TOKEN=test-token`;
  - `BLOB_STORE_URL=tmp_path/"blobs"`;
  - `EVENTS_ARCHIVE_URL=tmp_path/"events"` (created);
  - `V2_API_URL=http://v2.invalid/api`.

  Tests:
  - `test_writes_need_token_events_do_not`;
  - `test_422_detail_is_a_string`;
  - `test_daemons_list_with_and_without_slash`;
  - `test_bulk_endpoint_content_types_and_limits` (415, 413 on rows, 409, 422);
  - `test_run_event_attachments_and_task_logs_round_trip` (using `LocalStore`);
  - `test_task_log_rejects_and_short_hash`;
  - `test_dead_letter_list_filters`;
  - `test_heartbeat_restart_resets_started_at_and_prunes`;
  - `test_dispatchability_ignores_stopped_daemons`;
  - `test_health_at_both_paths`.
- **`tests/test_historical_metrics.py::test_running_counts_tasks_of_running_runs`**:
  insert a `running` run into the testcontainers database, write `process_submitted`,
  `process_started` and `process_completed` events for it into the archive fixture, and
  check `running()` against `RunningProcessesResponse`.
- **`packages/nf_client/tests/test_ingest.py`** (respx):
  - `test_ingest_rows_uses_bulk_when_supported` checks the NDJSON body and the counts;
  - `test_ingest_rows_falls_back_on_404` checks the per-row POSTs and the same counts.
- **The contract suite**, as in the acceptance criteria. `just v2-local` +
  `just contract` still passes with the contract changes.
- **Measurements (manual, recorded in the PR):**
  - generate a 58,699-row NDJSON file and time one `POST /samples/bulk` against
    `just pg-local`;
  - load 76,000 samples and 3 active workflows (`generate_series`), and time
    `POST /admin/reconcile-jobs`.
- **Commands:**
  - `uv run pytest tests/test_readset.py tests/test_control_transitions.py tests/test_control_registry.py tests/test_control_api.py tests/test_historical_metrics.py`
  - `uv run pytest`
  - `uv run pytest tests/test_source_catalog.py`
  - `uv run mypy src/nextflow_telemetry`
  - `uv run --project packages/nf_client pytest packages/nf_client/tests`
  - `uv run pytest contract/test_support.py`
  - `just pg-local`, then `just contract http://127.0.0.1:8000 contract-token ""`

## Risks

- **Deploy order on onclappc02.** The new routes need M1's migration on `pg_main`. Deploy
  only after `alembic upgrade head` there (M1 Risks), and after `fetch-secrets.sh` has
  written `DISPATCH_TOKEN`, and with `BLOB_STORE_URL=r2://nf-telemetry` in `.env` (the service
  refuses to start without it). Without the token the control writes are open on a public
  host. A deploy replaces the v1 control routes on `nf-telemetry.cancerdatasci.org` with
  the new ones. Nothing calls them before M8 and M9 (checked: client configs, `etl/v2.py`,
  `health_report.py` and the frontend all use `workers.dev`). The catalog routes, and the
  Worker's historical-tier proxy, keep working.
- **M4 and M6 run at the same time.** M3 owns `config.py`'s three deadline settings and
  the `transitions` signatures. If M4 merges first with its own copies, whichever merges
  second reconciles them to this spec's names. M6's import has to fill `readset_runs`, or
  `by-srr` misses imported samples.
- **The origin event path skips the R2 archive.** A reporter pointed at the origin
  instead of the edge would leave no history in R2. The configs and the SLURM templates
  point at the edge, and M5 makes that explicit with `event_url`.
- **`record_task` isn't idempotent.** A replayed NDJSON line counts twice. M5's cursor has
  to prevent replays.
- **Self-HTTP in the historical tier.** With `V2_API_URL` pointing at the service itself,
  `cohorts/{id}/failures` makes blocking HTTP calls to its own process from a threadpool.
  That works with uvicorn's single worker because the event loop stays free. Moving those
  reads onto Postgres is a later cleanup.
- **The obstore wheel.** obstore is a native wheel. 0.12.1 ships `cp311-abi3` manylinux
  x86_64 and aarch64 wheels, which cover the `python:3.11-slim` image and local 3.14.
- **Bulk memory.** A 100,000-row request is parsed in memory, about 50 MB at the limit.
  That is accepted at this scale (58,699 rows was the largest batch so far).
- **Reconcile: a departure from the spec's statement, and a miss on the 10 s target.**
  `reconcile_jobs` finds the earliest sample of each readset with the hashed set
  `s.id in (select min(id) from samples where readset_id is not null group by readset_id)`
  instead of v2's correlated `s.id = (select min(s2.id) … where s2.readset_id = s.readset_id)`.
  The results are the same. With no statistics on `samples`, which is the state right after
  a bulk load and before autovacuum's analyze, the planner turned the correlated form into
  a quadratic index scan on `samples_pkey` that ran past four minutes at 76,000 samples; the
  hashed form took 0.3 s for the same select. Measured on a scratch `postgres:16` through
  the API, 76,000 samples × 3 active workflows (228,000 jobs): 12.6 s straight after the
  bulk load, 10.2 s with fresh statistics, and 3.4–4.0 s when every job already exists.
  The select is 0.3–0.6 s of that; the rest is the insert with its indexes and foreign-key
  checks on `jobs`, so getting under 10 s on a cold first run would need a schema change.
  `POST /samples/bulk` took 7.1 s for 58,699 rows.
- **Shutdown.** The service has no lifespan hook or background task; an idle `uvicorn` exits
  on SIGTERM in 0.2 s. A server stopped while a request is in flight waits for that request,
  so a slow `reconcile-jobs` (above) is what held the earlier dev run past SIGTERM and
  SIGINT. A deploy that restarts the container during a first-time reconcile pays the full
  stop timeout; M4's sweep and M5's ingester must be cancelled in the lifespan.
