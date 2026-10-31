# Spec #372: PG migration M2, an HTTP contract suite for v2 and the new service

Issue: [#372](https://github.com/seandavi/nextflow_telemetry/issues/372). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), the move of the
control plane to Postgres ([ADR-0013](../adr/0013-postgres-control-plane-edge-event-sink.md)).
Blocked by M0 (#370, merged). It can run in parallel with M1 (#371), and M3 (#373) is
done when this suite passes against the new service.

## Goal

A black-box pytest + httpx suite, `contract/`, that drives the control-plane API over
HTTP using only a base URL and a token. It passes today against a local v2 (the
Worker in `cf/` under `wrangler dev`), and M3 makes it pass against the Python service
on Postgres. It replaces the v1↔v2 parity idea in #173/#174 with a v2→Postgres
comparison. It covers:

- the lifecycle scenarios ported from `cf/test/lifecycle.test.ts`;
- every path the callers use: `nf-client`, the run wrapper, `etl/v2.py`, the
  frontend API client and `health_report.py`;
- response shapes, checked against Pydantic mirrors of the Zod schemas in
  `cf/src/schemas.ts`, including `total/limit/offset` (#118).

Scenarios that need time to pass use a fast-clock fixture and are skipped unless the
target has been configured with short deadlines. A GitHub Actions job runs the whole
suite against a local v2 on every PR, which partly covers #265.

## Non-goals

- **Routes v2 does not serve:** `/submissions*`, `/curated*`, `/auth/*`. They are v1
  routes (the catalog service) and can't pass against v2. Whichever step moves them
  adds them to the suite.
- **New M3 endpoints:** bulk registration (#373), `GET /admin/dead-letter` and
  filtered requeue (#299). M3 adds their tests when it builds them.
- **v2-only housekeeping:** `POST /admin/reset` (`ALLOW_RESET`), `POST /admin/archive-retired`,
  and the readset backfill and column-migration tests. These test Durable Object
  storage, not the wire contract, and they stay in `cf/test/`. The suite never calls
  reset (see Design: isolation).
- **Historical tier shapes:** `/metrics/processes/{summary,retries,resources-by-attempt,failures,failure-signatures,tasks,timeline}`
  and `/cohorts/{id}/failures`. The suite only checks that they answer 200 or 501 with
  a JSON body. v2 proxies them to the catalog or returns 501, and their shapes are the
  v1 Pydantic models, not Zod.
- **No change to:** `nf-client`, the run wrapper, `etl/`, the frontend,
  `health_report.py`, Alembic, `src/nextflow_telemetry/`, `tests/`, or
  `just v2-e2e`. No new Python or npm dependency.
- **The rest of #265:** the watchdog test failures and a CI job for `uv run pytest`.

## Departures from the issue and the design record

1. **The suite lives in `contract/`, not `tests/`.** AGENTS.md says new tests go
   under `tests/`. But `tests/conftest.py` has a session-scoped autouse fixture that
   starts a Postgres testcontainer, and `testpaths = ["tests"]` means plain
   `uv run pytest` would collect the suite without a target. A top-level `contract/`
   picks up neither conftest nor testpaths. Run it with `uv run pytest contract`.
2. **Writes are opt-in, and refused against production.** The issue only says to skip
   time-dependent cases against production v2. The suite does more than that: it
   registers samples and calls `POST /admin/reconcile-jobs` and
   `POST /admin/requeue-dead-letter`, which act on the whole catalog. On production
   that would give real active registrations jobs for fake SRRs, which the cluster
   daemons would then claim. So the write tier only runs when
   `CONTRACT_ALLOW_WRITES=1`, and the suite exits if that is set while the base URL's
   host is a production host. Against production the suite is read-only: shapes over
   live data.
3. **"Local v2 (`just v2-e2e` setup)"** means a new recipe, `just v2-local`
   (`wrangler dev` on miniflare, no Cloudflare credentials, short deadlines).
   `just v2-e2e` resets and seeds the deployed Worker, which `ALLOW_RESET=false` now
   forbids (cf/STATUS.md), so it can't serve as a CI target. `v2-e2e` stays unchanged.
4. **Three fixes to `cf/src/schemas.ts`, each where it is wrong about live v2:**
   - `ClaimedBatch.jobs[].id` is required in Zod, but v2 never sends it. Checked on
     2026-10-30 by parsing a live local claim through `S.ClaimedBatch`; the only
     error was `jobs.0.id: expected number, received undefined`. `nf-client`'s
     `DispatchedJob` doesn't read it either. Remove it.
   - `RunDetail` doesn't declare `task_status_counts` or `failed_tasks`. v2 always
     sends them (`control-do.ts` `getRun`), and the dashboard reads them
     (`frontend/src/types.ts` `RunDetail`, `pages/RunsPage.tsx`). Add them.

   - `ClaimedBatch.jobs[].metadata` is `.nullable()` in Zod, but no server sends
     null: v2's `safeJson` returns `{}` and v1's `DispatchedJob` defaults to `{}`.
     `nf-client` parses every claim with `DispatchBatchResponse`, whose
     `DispatchedJob.metadata: dict[str, Any] = {}` rejects null, so a service that
     returned null would break the cluster daemons. Make it a required object in Zod,
     and in the mirror too (stricter than the old Zod, on purpose).

   The other 16 GET schemas parsed cleanly against live local v2 on 2026-10-30.
   These are documentation-only changes to the OpenAPI document; handler behaviour
   is unchanged.
5. **`Stats.storage` is not required by the mirror.** It reports Durable Object bytes
   against the 10 GB per-object cap. No caller reads it (checked: frontend,
   `nf-client stats`, `health_report.py`), and the Postgres service has no such cap.
   The Python `Stats` mirror accepts the field but does not require it. Zod is
   unchanged.

## Design

### Files

| File | Change |
|---|---|
| `contract/__init__.py` | empty; makes `contract.support` and `contract.schemas` importable |
| `contract/conftest.py` | env → `Target`, the clients, `ns`, the `writes` marker, `fast_clock` |
| `contract/support.py` | `Target`, `target_from_env`, `eventually`, `validate`, scenario helpers |
| `contract/schemas.py` | Pydantic mirrors of `cf/src/schemas.ts` responses, plus small result models |
| `contract/test_read.py` | read-only tier: shapes over whatever the target holds |
| `contract/test_registry.py` | samples, workflows, registrations as bundles, readset keying |
| `contract/test_lifecycle.py` | claim → submitted → started → MARK_COMPLETE → completed; retry; DLQ |
| `contract/test_dispatch_admin.py` | 204, workflow filters, submitted 404, close-run, reset-running, deprecated no-ops |
| `contract/test_wrapper.py` | `runs/{run}/event`, log attachments, nextflow_log sentinel |
| `contract/test_task_logs.py` | `POST/GET /task-logs` |
| `contract/test_fleet.py` | daemons, dispatchability |
| `contract/test_auth.py` | bearer required on writes, the event routes exempt |
| `contract/test_timers.py` | fast clock: claim expiry, liveness expiry → DLQ |
| `cf/src/schemas.ts` | departure 4 |
| `justfile` | `v2-local`, `contract` recipes; one line each in `help` |
| `.github/workflows/contract.yml` | CI job: local v2 + suite |
| `README.md` | "HTTP contract suite" subsection under Development |

No Alembic migration, no endpoint, and no change to `config.py` or `env`.

### Configuration (environment variables, read only by the suite)

| Variable | Default | Meaning |
|---|---|---|
| `CONTRACT_BASE_URL` | required; the session errors if unset | Origin root, no `/api`, no trailing slash. Example: `http://127.0.0.1:8787` |
| `CONTRACT_EVENT_URL` | `CONTRACT_BASE_URL` | Root that receives `/telemetry`, `/api/runs/{run}/event`, `POST /api/task-logs`. After M5 this is the edge Worker (ADR-0013 §2) |
| `CONTRACT_TOKEN` | unset | Bearer token for origin writes. When it is set, `test_auth.py` runs |
| `CONTRACT_ALLOW_WRITES` | unset | `1` enables tests marked `writes` |
| `CONTRACT_FAST_CLOCK_SECONDS` | unset | Longest time, in seconds, the target takes to fire any deadline (window plus sweep interval). When set, `test_timers.py` runs |
| `CONTRACT_EVENT_LAG_SECONDS` | `5` | How long the suite polls for state driven by an event. Covers the ingester lag after M5 (about a minute; set to `90` then) |

The URLs are normalised with `rstrip("/")`. Production hosts, used for the write
refusal: `nf-telemetry.seandavi.workers.dev`, `nf-telemetry.cancerdatasci.org`.
The stable origin hostname from #368 gets added to that set when it exists.

### `contract/support.py`

```python
PRODUCTION_HOSTS: frozenset[str] = frozenset({"nf-telemetry.seandavi.workers.dev", "nf-telemetry.cancerdatasci.org"})

@dataclass(frozen=True)
class Target:
    base_url: str
    event_url: str
    token: str | None
    allow_writes: bool
    fast_clock_seconds: float | None
    event_lag_seconds: float

def target_from_env(environ: Mapping[str, str]) -> Target
    # Raises ValueError when CONTRACT_BASE_URL is missing, or when allow_writes is
    # set and urllib.parse.urlsplit(base_url).hostname (or event_url's) is in PRODUCTION_HOSTS.

def eventually(check: Callable[[], T], timeout: float, interval: float = 0.25) -> T
    # Calls check() until it returns without AssertionError; re-raises the last
    # AssertionError once `timeout` has passed (time.monotonic). check() always runs at least once.

def validate(model: type[BaseModel] | TypeAdapter[Any], resp: httpx.Response, status: int = 200) -> Any
    # assert resp.status_code == status, f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text[:500]}"
    # then model.model_validate(resp.json()) or adapter.validate_python(resp.json()); returns the validated value.

def new_srr() -> str                      # f"SRR{secrets.randbelow(9 * 10**8) + 10**8}", always 9 digits
def md5_sample_id(runs: str) -> str       # hashlib.md5(";".join(sorted(runs.split(";"))).encode()).hexdigest()

def register_workflow(api: httpx.Client, workflow_id: str, **fields: Any) -> dict[str, Any]
    # POST workflows {workflow_id, version="1.0.0", repository_url="https://example.invalid/contract",
    # revision="main", max_retries=1} | fields; asserts 201; returns body validated as schemas.Workflow, as dict.
def register_sample(api: httpx.Client, collection: str | None, runs: str | None = None, **fields: Any) -> dict[str, Any]
    # runs defaults to new_srr(); sample_id = md5_sample_id(runs); asserts 201; returns schemas.Sample as dict.
def reconcile(api: httpx.Client) -> int                   # POST admin/reconcile-jobs → jobs_created
def claim(api: httpx.Client, workflow_id: str | list[str], limit: int = 10) -> dict[str, Any] | None
    # POST dispatch/batch {limit, workflow_id}; 204 → None; 200 → schemas.ClaimedBatch as dict.
def submitted(api: httpx.Client, run_name: str, sample_ids: list[str]) -> None
    # POST dispatch/submitted {run_name, executor_job_id: "contract-1", sample_ids}, the nf-client body; asserts 200.
def weblog(edge: httpx.Client, run_name: str, event: str, **extra: Any) -> None
    # POST telemetry {runId: "contract-nf-session", runName, event, utcTime: now ISO-8601 Z} | extra; asserts 200.
def mark_complete(edge: httpx.Client, run_name: str, tag: str) -> None
    # weblog process_completed with trace {tag, process: "cmgd:MARK_COMPLETE", status: "COMPLETED", hash: "aa/bbbbbb"}.
def run_event(edge: httpx.Client, run_name: str, event: dict[str, Any],
              files: dict[str, tuple[str, bytes, str]] | None = None) -> httpx.Response
    # POST api/runs/{run_name}/event, data={"event": json.dumps({"utc_time": now} | event)}, files=files.
    # Multipart, exactly as run_wrapper._post_event sends it. Returns the response unasserted.
def job_summary(api: httpx.Client, pk: int) -> dict[str, Any]   # GET workflows/{pk}/job-summary → schemas.JobSummary
def run_detail(api: httpx.Client, run_name: str) -> dict[str, Any]  # GET runs/{run} → schemas.RunDetail
```

Paths are relative and have no leading slash, the `nf-client` convention: the `api`
client's `base_url` ends in `/api/`, and the `edge` client's in `/`. httpx 0.28.1
(the locked version) is used for `Client(base_url=, headers=, timeout=)` and
`post(data=, files=, json=, params=)`; these were checked against the installed package.

### `contract/conftest.py`

- `pytest_configure(config)`: registers the markers
  `writes: mutates the target; needs CONTRACT_ALLOW_WRITES=1` and
  `fast_clock: needs CONTRACT_FAST_CLOCK_SECONDS`.
- `pytest_collection_modifyitems(config, items)`: builds the `Target` with
  `target_from_env(os.environ)`. If that raises `ValueError`, it calls
  `pytest.exit(str(err), returncode=2)`. Otherwise it adds `pytest.mark.skip` to
  `writes` items when writes are off, and to `fast_clock` items when
  `fast_clock_seconds` is None. The skip reason names the variable.
- Session fixtures:
  - `target() -> Target`.
  - `api(target) -> httpx.Client`: `base_url=f"{base_url}/api/"`, a bearer header
    when there is a token, `timeout=30`.
  - `anon(target) -> httpx.Client`: the same with no auth header.
  - `edge(target) -> httpx.Client`: `base_url=f"{event_url}/"`, no auth (the
    reporters can't send headers), `timeout=30`.
  - `ns() -> str`: `f"ct{secrets.token_hex(4)}"`, the per-session prefix for every
    workflow id, collection and run-unrelated name the suite creates.

  Each client fixture closes the client at teardown.
- `lag(target) -> float`: `target.event_lag_seconds`.
- `fast_clock(target) -> float`: `target.fast_clock_seconds + 10`, the timeout for
  `eventually`.

**Isolation, without reset.** A session never resets the target and never asserts
global counts. Each test works on its own registration: workflow id
`f"{ns}-{name}"`, scoped (`collections=[f"{ns}-{name}"]`) unless the test is about
unscoped registrations. Each test also uses its own collection and fresh SRRs, and
claims with `workflow_id=<its own id>`. Job counts are read from the test's own
`job-summary`, `runs/{run}` and `cohorts/{collection}/summary`. A test that registers
an unscoped workflow retires it (`PATCH status retired`) in a `finally`, so other
tests' reconciles don't keep feeding it jobs. With this, a local target can run the
suite many times without a restart, and a scratch Postgres database needs no
fixtures beyond `alembic upgrade head`.

`POST /admin/requeue-dead-letter` and `/admin/reconcile-jobs` still act on every
registration. That is why writes need a disposable target (departure 2).

**Events are eventually consistent.** Any assertion on state that an event drives
(`started`, `MARK_COMPLETE`, `completed`, wrapper events) is wrapped in
`eventually(..., lag)`. Assertions after origin calls (claim, submitted, close-run)
are not.

### `contract/schemas.py`

Pydantic v2 (2.13.3, locked). There is one model per Zod response schema, with the
same name and the same fields. Each class has a one-line docstring naming its Zod
counterpart; there are no other comments. The mapping:

| Zod | Pydantic |
|---|---|
| `z.looseObject` | `model_config = ConfigDict(extra="allow")` |
| `z.object` (the envelopes `SampleList`, `CollectionFacets`, `JobList`, `JobSummary`, `RunList`, `RunEventAccepted`, `TaskLogs`, `Detail`) | `ConfigDict(extra="forbid")` |
| `Stats` (`z.object`) | `extra="allow"`, `storage: dict[str, float] \| None = None` (departure 5) |
| `.nullable()` (key required) | `X \| None` with **no default**. In Pydantic v2 that field is required but may be null, matching Zod |
| `.optional()` / `.nullish()` | `X \| None = None` |
| `z.number().int()` | `int` |
| `z.number()` | `float` |
| `iso` | `str` |
| `z.enum` | `Literal[...]` (`WorkflowStatus`, `JobStatus`, `RunStatus`, `RunEventType`, `SampleKey`) |
| `z.record(z.string(), X)` | `dict[str, X]` |
| `WorkflowParams` | `dict[str, str \| int \| float \| bool]` |

Models: `Detail`, `Sample`, `SampleList`, `CollectionFacet`, `CollectionFacets`,
`Workflow`, `JobListItem`, `JobList`, `JobSummary`, `ClaimedJob`, `ClaimedBatch`,
`Run`, `RunList`, `FailedTask`, `RunDetail(Run)`, `RunEventAccepted`, `Cohort`,
`CohortSummary`, `Daemon`, `Stats`, `RunningProcess`, `RunningProcesses`, `TaskLog`,
`TaskLogs`.

- `ClaimedJob` has `sample_id: str`, `ncbi_accession: str | None`, and
  `metadata: dict[str, Any]` (required, not nullable). It has no `id` (departure 4).
- `RunDetail` adds `job_status_counts: dict[str, int]`,
  `nextflow_log_available: bool`, `wrapper_output_log_available: bool`,
  `task_status_counts: dict[str, int]` and `failed_tasks: list[FailedTask]`.
- `FailedTask` (extra allow) has `process: str`, `sample_id: str | None`,
  `exit_code: str | None`, `task_hash: str | None`, `attempt: int | None` and
  `error_action: str | None`.

There are also result models for responses that have no Zod schema. Their fields
come from `cf/src/index.ts` and the callers' types, and they all use `extra="allow"`:

| Model | Fields | Route |
|---|---|---|
| `Health` | `message: str`, `status: str`, `database: str` | `GET /api/health` (frontend `HealthResponse`) |
| `Reconciled` | `jobs_created: int` | `POST admin/reconcile-jobs` |
| `RequeuedDeadLetter` | `requeued: int` | `POST admin/requeue-dead-letter` |
| `Submitted` | `run_name: str`, `status: Literal["submitted"]` | `POST dispatch/submitted` |
| `RequeuedExpired` | `requeued_runs: int` | `POST dispatch/requeue-expired` |
| `HeartbeatWatchdog` | `stale_runs_failed: int`, `jobs_swept: int` | `POST admin/heartbeat-watchdog` (frontend) |
| `ClosedRun` | `run_name: str`, `already_closed: bool`, `swept: int` | `POST admin/close-run` |
| `ResetRunning` | `reset: int` | `POST admin/reset-running` |
| `Dispatchability` | `checked_at: str`, `active_daemons: int`, `stuck: list[dict[str, Any]]`, `stuck_pending_total: int` | `GET admin/dispatchability` |
| `DeletedDaemon` | `deleted: str` | `DELETE daemons/{agent_id}` |
| `TaskLogUploaded` | `TaskLog`'s fields | `POST task-logs` |

### Tests

Unless a test says otherwise it is marked `writes`. Each line below is one test
function; the bullets name the assertions. "Own summary" means
`job_summary(api, own_pk)`.

**`test_read.py`** (no marker; safe against production). It lists, then descends
into the first entity, and skips the descent when the list is empty:

- `test_health`: `GET health` → `Health`.
- `test_stats`: `admin/stats` → `Stats`, the fields `nf-client stats` and
  `health_report.py` read.
- `test_workflows`:
  - `workflows`, `workflows?status=active` → `list[Workflow]`;
  - for the first: `workflows/{pk}` → `Workflow`, `workflows/{pk}/job-summary` →
    `JobSummary`, `workflows/{pk}/jobs?limit=5` → `JobList`, with `limit == 5` and
    `after == 0`;
  - `workflows/999999999` → 404 `Detail`.
- `test_samples`:
  - `samples?limit=5&offset=0` → `SampleList`, with `limit == 5`, `offset == 0`,
    `total >= len(items)`;
  - `samples/facets/collections` → `CollectionFacets`;
  - for the first item: `samples/{sample_id}` and `samples/by-srr/{first run}` →
    `Sample`, and `samples?collection={first collection}&limit=1000&offset=0`
    (`etl/v2.py`'s call) → `SampleList`.
- `test_cohorts`:
  - `cohorts`, `cohorts/leaderboard` → `list[Cohort]`;
  - for the first: `cohorts/{id}/summary` → `CohortSummary`;
  - `cohorts/{ns}-nosuch/summary` → 404.
- `test_runs`:
  - `runs` (no params, `health_report.py`), `runs?limit=5&offset=0` → `RunList`,
    with `limit`/`offset` echoed;
  - for the first: `runs/{run}` → `RunDetail`;
  - `runs/{ns}-nosuch` → 404.
- `test_daemons`: `daemons` and `daemons/` (the frontend and `health_report.py` use
  the slash) → `list[Daemon]`; `daemons/?active_only=true` → `list[Daemon]`, every
  item `is_active`.
- `test_dispatchability`: `admin/dispatchability` → `Dispatchability`.
- `test_running_processes`: `metrics/processes/running` → `RunningProcesses`.
- `test_historical_tier` (parametrised over the 7 metrics routes and
  `cohorts/{first or ns}/failures?process=x`): status in {200, 501}, and the body
  parses as JSON.

**`test_registry.py`**:

- `test_sample_upsert_is_idempotent`:
  - registering the same body twice returns the same `id` and `sample_id`;
  - registering again with a second collection gives `collections` containing both;
  - `samples?collection={c}` total is 1;
  - `samples/facets/collections` has `{collection: c, count: 1}`.
- `test_sample_lookups`:
  - `samples/{md5}`, `samples/{readset_id}` and `samples/by-srr/{srr}` all return
    the same `sample_id`;
  - `samples/by-biosample/{ns}-SAMN1` → `list[Sample]` of length 1, after
    registering with `biosample_id`;
  - `samples?search={readset_id[3:12]}` contains it;
  - `samples/by-srr/SRR000000000` → 404.
- `test_sample_requires_ids`: POST without `ncbi_accession` → 422.
- `test_workflow_registration_is_a_bundle` (ADR-0010, `cf/test` "round-trips params
  and collections"):
  - register with `params={"humann_bundle": "x", "skip_humann": False}` and
    `collections=[c]` → 201, and the round trip is equal;
  - the same params with keys reordered → 201, same `id`;
  - re-register without `collections` → `collections == [c]`;
  - different params → 409 `Detail`, and `GET workflows/{pk}` params are unchanged;
  - each of `{"params": {"nested": {"a": 1}}}`, `{"params": ["x"]}`,
    `{"collections": "c"}` and `{"collections": []}` → 422.
- `test_workflow_status_and_revision`:
  - `PATCH workflows/{pk}/status` `paused` / `active` → `Workflow` with that status;
  - `{"status": "bogus"}` → 422;
  - `PATCH workflows/{pk}/revision` `{"revision": "abc123"}` → `revision == "abc123"`;
  - `workflows?status=paused` includes the pk while paused;
  - `PATCH workflows/999999999/status` → 404.
- `test_scoped_reconcile`:
  - a workflow scoped to `c1`, one sample in `c1`, one in `c2`;
  - `reconcile` returns an int ≥ 1, and the own summary has `total == 1` and
    `pending == 1`;
  - reconciling again leaves the own summary unchanged.
- `test_readset_keyed_claim` (ADR-0007): a new registration has
  `sample_key == "readset_id"`. Two samples with run lists `"A;B"` and `"B,A"` and
  different md5 ids → own `total == 1`. The claim's `jobs[0].sample_id` equals
  `GET samples/{md5}`'s `readset_id`, and it starts `RS.`.
- `test_jobs_keyset_paging` (`etl/v2.py`, `health_report.py`):
  - 3 samples, so 3 jobs; `workflows/{pk}/jobs?limit=2` → 2 items, ids ascending;
  - `?after={items[-1].job_id}&limit=2` → the third;
  - after a MARK_COMPLETE (eventually, `lag`): `?status=completed` → 1 item with
    `sample_key` starting `RS.`, `readset_id == sample_key`, the md5 `sample_id`,
    `collections == [c]` and a non-null `completed_at`;
  - `workflows/999999999/jobs` → 404.

**`test_lifecycle.py`**:

- `test_happy_path` (two samples, `max_retries=1`):
  - `claim(limit=2)` → `ClaimedBatch` with 2 jobs, `workflow_id`, `repository_url`
    and `params` echoed; own `claimed == 2`;
  - `submitted` → own `submitted == 2`; `runs/{run}.status == "submitted"`;
  - `weblog started` → eventually own `running == 2` and run `running`;
  - `mark_complete(jobs[0].sample_id)` → eventually own `completed == 1`;
  - `weblog completed` → eventually run `completed`, own `completed == 1`,
    `running == 0`, `pending == 1` (within budget, so it is retried);
  - `cohorts/{c}/summary` → `samples_completed == 1`;
  - `cohorts/leaderboard` row for `c` has `sample_count == 2`.
- `test_late_mark_complete_never_flips`: after the happy-path shape closes a run,
  `mark_complete` for the swept sample on the closed run leaves own `completed`
  unchanged. Check it after `lag` seconds with a plain `time.sleep(lag)`, because
  this asserts that nothing happens.
- `test_retry_budget_dead_letters` (`max_retries=0`):
  - claim, submitted, started, then `weblog completed` with no MARK_COMPLETE →
    eventually own `failed == 1`, `dead_letter == 1`, and run
    `job_status_counts == {"failed": 1}`;
  - `POST admin/requeue-dead-letter` → `RequeuedDeadLetter` with `requeued >= 1`;
    own `failed == 0` and `pending == 1`.
- `test_nextflow_failure_reason`: `weblog completed` with
  `metadata={"workflow": {"success": False, "errorMessage": None, "errorReport": "Failed to pull singularity image"}}`
  → eventually run `failed` and `slurm_reason == "Failed to pull singularity image"`.
- `test_task_counts_and_failed_tasks`: two `process_completed` traces for one
  sample (`FAILED` exit 137 attempt 1 `error_action RETRY`, then `COMPLETED` exit 0
  attempt 2) → eventually `task_status_counts == {"FAILED": 1, "COMPLETED": 1}` and
  `failed_tasks == [{"process": "cmgd:kneaddata", "sample_id": <tag>, "exit_code": "137", "task_hash": "ab/cdef12", "attempt": 1, "error_action": "RETRY"}]`.
- `test_runs_filter_and_paging`: `runs?workflow_id={own}&limit=1` → `RunList` with
  `total >= 1`, one run whose `workflow_id` is the own id, and `limit == 1`.

**`test_dispatch_admin.py`**:

- `test_claim_204_when_paused`: pause the own workflow → claim returns `None` (204);
  resume.
- `test_claim_comma_joined_workflow_ids` (#250): `workflow_id=[f"{ns}-nosuch,{own}"]`
  → 200, and the batch's `workflow_id` is the own id.
- `test_claim_limit`: 3 pending; `claim(limit=1)` → 1 job; own `pending == 2`,
  `claimed == 1`.
- `test_submitted_unknown_run`: `dispatch/submitted` with `run_name=f"{ns}-nosuch"`
  → 404 `Detail`.
- `test_close_run`:
  - claim, submitted, started → `POST admin/close-run {"run_name": run}` →
    `ClosedRun`, `already_closed is False`; run `completed`;
  - again → `already_closed is True`;
  - unknown run → 404.
- `test_reset_running`:
  - `POST admin/reset-running?workflow_pk={pk}` after a run is started →
    `ResetRunning`; own `running == 0` and `pending >= 1`;
  - without `workflow_pk` → 422.
- `test_deprecated_noops`: `dispatch/requeue-expired` → `RequeuedExpired`;
  `admin/heartbeat-watchdog` → `HeartbeatWatchdog`. Both are callers' paths
  (frontend); the suite checks shapes, not the deprecation header.

**`test_wrapper.py`** (the run wrapper's multipart events; no token):

- `test_wrapper_lifecycle`:
  - claim, submitted;
  - `wrapper_started`, then `heartbeat` → 201 `RunEventAccepted`, `type` echoed,
    both `*_uploaded` false;
  - `wrapper_exited` with `exit_code: 0` and files `nextflow_log=("nextflow.log", b"nf says hi", "text/plain")`
    and `wrapper_output_log=("wrapper_output.log", b"OOM", "text/plain")` → 201 with
    `nextflow_log_uploaded` and `wrapper_output_log_uploaded` true;
  - eventually run `completed`, `nextflow_log_available` and
    `wrapper_output_log_available` true;
  - `GET task-logs/{run}/nextflow_log` → `TaskLogs`, whose
    `[(log_type, content)] == [("nextflow_log", "nf says hi"), ("wrapper_output_log", "OOM")]`.
- `test_wrapper_exit_nonzero`: `wrapper_exited` `exit_code: 1` → eventually run
  `failed`, `slurm_reason == "wrapper exited 1"`.
- `test_wrapper_rejects`:
  - a non-JSON `event` field → 422;
  - `{}` with no `type` → 422;
  - a `heartbeat` with a `nextflow_log` file → 422;
  - `wrapper_exited` with a file for run `f"{ns}-nosuch"` → 404.

**`test_task_logs.py`** (`nf-client upload-logs`, the dashboard; posted on `edge`,
read on `api`, no token):

- `test_upload_and_read`: POST `run_name={ns}-r`, `task_hash="ab/cdef1234567890"`,
  `log_type="command_err"`, `content` file `b"boom"` → 201 `TaskLogUploaded` with
  `task_hash == "ab/cdef12"`. `GET task-logs/{ns}-r/ab/cdef12` → `TaskLogs` with one
  log, `content == "boom"` and `log_type == "command_err"`.
- `test_rejects`: no `run_name`, `""` and `"null"` → 400;
  `log_type="bogus"` → 422.
- `test_strips_nul`: content `b"he\x00llo\x00"` → the read back gives `"hello"`.

**`test_fleet.py`**:

- `test_daemon_heartbeat_list_delete`:
  - `PUT daemons/heartbeat` with the `nf-client` daemon payload (`agent_id=f"{ns}-host:wf"`,
    `hostname`, `workflow_id`, `mode="slurm"`, `batch_size=1`, `max_concurrent_runs=1`,
    `active_runs=0`, `status="idle"`, `nf_client_version="contract"`) → `Daemon`,
    `is_active`;
  - `daemons/` contains it, and so does `daemons/?active_only=true`;
  - `DELETE daemons/{agent_id}` → `DeletedDaemon`; deleting again → 404.
- `test_dispatchability_counts_active_daemon`: after a heartbeat,
  `admin/dispatchability.active_daemons >= 1`.

**`test_auth.py`** (marked `writes`; it also skips unless `target.token`):

- `test_writes_need_bearer`: through `anon`, each of `POST admin/reconcile-jobs`,
  `POST samples`, `POST workflows`, `PATCH workflows/1/status`,
  `PUT daemons/heartbeat` and `POST dispatch/batch` → 401.
- `test_event_routes_are_open`: through `edge` with no header, `POST telemetry`
  (`runName=f"{ns}-auth"`, `event="started"`), a `heartbeat` on
  `api/runs/{ns}-auth/event`, and `POST api/task-logs` all return non-401.
- `test_reads_are_open`: `anon` `GET admin/stats` → 200.

**`test_timers.py`** (marked `writes` and `fast_clock`; each test takes the
`fast_clock` timeout):

- `test_claim_expiry`: claim, never submit → eventually run `expired`, own
  `claimed == 0`, `pending == 1`, run `job_status_counts == {}` (an expired claim
  burns no retry).
- `test_liveness_expiry_dead_letters` (`max_retries=0`): claim, submitted,
  `weblog started`, one `heartbeat` run event, then silence → eventually run
  `failed`, `"presumed dead"` in `slurm_reason`, own `dead_letter == 1`.
- `test_liveness_expiry_retries` (`max_retries=1`): the same with a fresh
  registration → eventually run `failed` and own `pending == 1` (retried, not dead).

A deadline on the target has to fire within `CONTRACT_FAST_CLOCK_SECONDS`. v2 does
this with fractional minute vars, since `index.ts` `mins()` uses `Number()`. M4
(#374) has to make the claim TTL, liveness window and sweep interval configurable
down to seconds for the same fixture.

### `cf/src/schemas.ts`

- `ClaimedBatch.jobs` item: delete `id: z.number().int(),`.
- `RunDetail`: add
  `task_status_counts: z.record(z.string(), z.number().int()).describe("Per-run task outcomes from run_task_counts")`
  and
  `failed_tasks: z.array(z.looseObject({ process: z.string(), sample_id: z.string().nullable(), exit_code: z.string().nullable(), task_hash: z.string().nullable(), attempt: z.number().int().nullable(), error_action: z.string().nullable() }))`.

`npm test` and `npm run typecheck` in `cf/` must stay green. The lifecycle suite
parses `RunDetail` through Zod, so `cf/test` checks the new fields too.

### `justfile`

Add these after `v2-e2e`:

```just
# Local v2 for the contract suite (#372): miniflare, no Cloudflare credentials,
# 6 s claim TTL and liveness, no catalog proxy. Same flags as .github/workflows/contract.yml.
v2-local:
	cd cf && npx wrangler dev --ip 127.0.0.1 --port 8787 \
	  --var CLAIM_TTL_MINUTES:0.1 --var LIVENESS_MINUTES:0.1 --var CATALOG_URL: --var API_TOKEN:contract-token

# HTTP contract suite against a running target; defaults match `just v2-local`.
contract url="http://127.0.0.1:8787" token="contract-token":
	CONTRACT_BASE_URL={{url}} CONTRACT_TOKEN={{token}} CONTRACT_ALLOW_WRITES=1 CONTRACT_FAST_CLOCK_SECONDS=20 \
	  uv run pytest contract -v
```

Add one `help` line for each, under "Data and dispatch". Checked on 2026-10-30:
`wrangler dev` (wrangler 3.100.0 from `cf/package-lock.json`) starts with an empty
`HOME` and no Cloudflare token. With `CLAIM_TTL_MINUTES:0.05` an unconfirmed claim
went `expired` within 6 s, and with `LIVENESS_MINUTES:0.1` a run went `failed`
("presumed dead: heartbeats stopped") within 9 s. `CATALOG_URL:` (empty) makes the
historical routes return 501.

### `.github/workflows/contract.yml`

```yaml
name: contract
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  v2-local:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      WRANGLER_SEND_METRICS: "false"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: cf/package-lock.json
      - run: npm ci
        working-directory: cf
      - uses: astral-sh/setup-uv@v10.3.3
      - run: uv sync --group dev --frozen
      - name: Start local v2 (flags match `just v2-local`)
        working-directory: cf
        run: |
          npx wrangler dev --ip 127.0.0.1 --port 8787 \
            --var CLAIM_TTL_MINUTES:0.1 --var LIVENESS_MINUTES:0.1 --var CATALOG_URL: --var API_TOKEN:contract-token \
            > "$RUNNER_TEMP/wrangler.log" 2>&1 &
          for i in $(seq 60); do curl -sf http://127.0.0.1:8787/api/health && exit 0; sleep 1; done
          cat "$RUNNER_TEMP/wrangler.log"; exit 1
      - name: Contract suite
        env:
          CONTRACT_BASE_URL: http://127.0.0.1:8787
          CONTRACT_TOKEN: contract-token
          CONTRACT_ALLOW_WRITES: "1"
          CONTRACT_FAST_CLOCK_SECONDS: "20"
        run: uv run pytest contract -v
      - if: failure()
        run: cat "$RUNNER_TEMP/wrangler.log"
```

The action refs were checked on 2026-10-30. `checkout@v4` and `setup-node@v4` match
`add-study.yml`. `astral-sh/setup-uv` no longer publishes a moving major tag, so it is
pinned to the release tag. `API_TOKEN` is a plain `--var` here, so CI exercises the
auth paths with a throwaway value; it is not a secret.

### `README.md`

Add a `### HTTP contract suite` subsection under Development, after "Common
commands". It covers:

- what the suite is for (#372, ADR-0013 M2), and that it lives in `contract/`;
- the two-terminal recipe: `just v2-local`, then `just contract`;
- the variable table above;
- the read-only production run, `CONTRACT_BASE_URL=https://nf-telemetry.seandavi.workers.dev uv run pytest contract`;
- that writes need a disposable target;
- that the CI job runs it.

### Callers affected

None. The suite reads the callers to choose its paths and bodies but doesn't change
them. The `schemas.ts` edits change only the published OpenAPI document.

## Acceptance criteria

- [ ] `contract/` exists with the files above. `uv run pytest` (no args) collects
      nothing from it, and `uv run pytest contract` without `CONTRACT_BASE_URL`
      exits 2 with a message naming the variable.
- [ ] With `just v2-local` running, `just contract` passes with 0 failures, and the
      only skips are tests that depend on the target (empty-list descents in
      `test_read.py`).
- [ ] `uv run pytest contract` against local v2 *without* `CONTRACT_ALLOW_WRITES`
      runs only `test_read.py` and skips every `writes` test with a reason naming
      the variable.
- [ ] Without `CONTRACT_FAST_CLOCK_SECONDS`, `test_timers.py` skips with a reason
      naming the variable.
- [ ] `CONTRACT_ALLOW_WRITES=1 CONTRACT_BASE_URL=https://nf-telemetry.seandavi.workers.dev uv run pytest contract`
      exits 2 before any request ("refusing writes against production host ...").
- [ ] Running `just contract` twice against the same `v2-local` without a restart
      passes both times (isolation without reset).
- [ ] Every path in the coverage table below is requested at least once by the suite.
- [ ] `cf/src/schemas.ts` has the departure 4 changes, and `cd cf && npm test && npm run typecheck` pass.
- [ ] `.github/workflows/contract.yml` exists and is green on the PR.
- [ ] README documents the suite. `uv run mypy src/nextflow_telemetry` still passes.

Coverage table (caller → paths):

| Caller | Paths |
|---|---|
| `nf-client` dispatch/daemon | `POST dispatch/batch`, `POST dispatch/submitted`, `PUT daemons/heartbeat`, `GET admin/stats` |
| `nf-client` operator | `POST samples`, `POST workflows`, `POST admin/reconcile-jobs`, `POST admin/requeue-dead-letter`, `POST task-logs` |
| run wrapper | `POST runs/{run}/event` (multipart, attachments) |
| Nextflow weblog | `POST /telemetry` |
| `etl/v2.py` | `GET workflows`, `GET workflows/{pk}/jobs?after&limit&status`, `GET samples?collection&limit&offset` |
| frontend `api.ts` | `health`, `workflows` (+ `PATCH status`, `job-summary`), `samples` (+ facets), `metrics/processes/*`, `task-logs/{run}/{hash}`, `dispatch/*`, `admin/{reconcile-jobs,requeue-dead-letter,heartbeat-watchdog,dispatchability}`, `daemons/`, `runs`, `runs/{run}`, `cohorts`, `cohorts/leaderboard`, `cohorts/{id}/summary`, `cohorts/{id}/failures` |
| `health_report.py` | `workflows?status=active`, `workflows/{pk}/job-summary`, `admin/stats`, `admin/dispatchability`, `metrics/processes/{failure-signatures,timeline,tasks}`, `daemons/`, `workflows/{pk}/jobs?status=completed&limit=500&after=`, `runs`, `runs/{run}` |
| operator (README/v2) | `PATCH workflows/{pk}/revision`, `GET workflows/{pk}`, `samples/{id}`, `by-srr`, `by-biosample`, `DELETE daemons/{id}`, `POST admin/{close-run,reset-running}`, `POST dispatch/requeue-expired` |

## Test plan

1. `cd cf && npm ci && npm test && npm run typecheck` after the `schemas.ts` edit.
2. Terminal A: `just v2-local`. Terminal B: `just contract`. All tests pass.
3. Run `just contract` again without restarting A; it passes again.
4. `CONTRACT_BASE_URL=http://127.0.0.1:8787 uv run pytest contract -v`: only reads
   run, and they pass.
5. Point a read-only run at production v2
   (`CONTRACT_BASE_URL=https://nf-telemetry.seandavi.workers.dev uv run pytest contract`).
   Record the result in the PR. A failure here is a v2-vs-Zod finding to report, not
   a reason to loosen a schema.
6. Check the write refusal against production (acceptance item 5).
7. `uv run pytest` (default testpaths) and `uv run mypy src/nextflow_telemetry`, unchanged.
8. Push, and see `contract` green on the PR.

## Risks

- **Mirror drift.** `contract/schemas.py` duplicates `cf/src/schemas.ts` by hand. A
  Zod change that also changes v2's responses fails the CI job. A Zod-only edit
  doesn't fail anything. The two files name each other: the `cf/src/schemas.ts` header comment and the
  `contract/schemas.py` module docstring.
  After M10 the Zod file goes away, and the Python mirror becomes the contract.
- **Global side effects.** Reconcile and requeue-dead-letter act on every
  registration. That is acceptable on a disposable target and is why writes are
  refused on production hosts. The production host list has to gain the #368 origin
  hostname when it exists.
- **Timing flake.** With 6 s deadlines on CI, a non-timer test that pauses more than
  6 s between claim and `submitted` would see its claim expire. Each such step is a
  single local HTTP call. If CI flakes, raise the windows to `0.2` and
  `CONTRACT_FAST_CLOCK_SECONDS` to `30`.
- **Event lag after M5.** Event-driven state shows up about a minute late. The
  `eventually` wrapper and `CONTRACT_EVENT_LAG_SECONDS` absorb that.
  `test_late_mark_complete_never_flips` sleeps for the full lag, so it gets slower
  as the lag grows.
- **v2 semantics that M3 may revisit.** The suite pins today's observable behaviour,
  for example that `requeue-dead-letter` takes no filters and that `dead_letter` in
  `job-summary` counts resolved rows (not asserted after requeue, on purpose). M3's
  #299 work changes the DLQ admin surface. It adds tests and must keep these passing.
- **CI cost.** `uv sync --group dev` installs the whole dev environment (duckdb,
  linkml, cdsci-lake from git) for a suite that needs only httpx, pydantic and
  pytest. That's accepted to keep one lockfile path. If the job gets slow, switch
  to `uv run --frozen --only-group` with a dedicated group.
