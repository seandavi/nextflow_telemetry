# Temporal-backed nextflow_telemetry — design seed

> Status: **seed for a future session** — uncommitted draft. Refine then decide.

## Why Temporal

The current `nextflow_telemetry` server reinvents what Temporal already solves:

- A job lifecycle state machine (`pending → claimed → submitted → running → completed/failed → dead_letter`) implemented as Postgres rows + ad-hoc reconcilers.
- A `nf-client daemon` that polls and claims jobs (`SELECT … FOR UPDATE SKIP LOCKED`).
- A `MARK_COMPLETE` sentinel process to detect completion via the unauthenticated `/telemetry` weblog endpoint.
- `sweep_run_incomplete()` for retries with a `max_retries` budget.
- A dead-letter table.

Every one of these is a thing Temporal does, durably, with replay, with first-class retries, with a worker model that scales horizontally. The current `daemon` is essentially a hand-rolled Temporal worker without the durability guarantees.

## What Temporal would replace

| Current concept | Temporal concept |
|---|---|
| `jobs_tbl.status` lifecycle | Workflow state (durable, replayable) |
| `nf-client daemon` polling loop | Temporal worker on the HPC node |
| `SELECT … FOR UPDATE SKIP LOCKED` claim | Activity polling on a task queue |
| `MARK_COMPLETE` sentinel | Signal to the running workflow |
| `/telemetry` weblog catch-all | Signal handler in the workflow |
| `sweep_run_incomplete()` | Activity retry policy + workflow timer |
| `dead_letter_tbl` | Workflow failure → DLQ namespace or status field |
| Manual reconcile (`reconcile_jobs`) | Per-sample workflow started by an admin signal/API |

## Proposed workflow shape

```python
@workflow.defn
class SampleProcessingWorkflow:
    """One workflow per (sample × workflow_def) pair."""

    @workflow.run
    async def run(self, params: SampleJobParams) -> JobResult:
        # 1. Wait for a worker to claim
        await workflow.wait_condition(lambda: self._claimed)

        # 2. Run the activity (submit to local/slurm/pbs/lsf)
        run_id = await workflow.execute_activity(
            submit_to_executor,
            params,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # 3. Wait for either MARK_COMPLETE signal or a heartbeat-based timeout
        try:
            await workflow.wait_condition(
                lambda: self._completed,
                timeout=timedelta(hours=24),
            )
        except asyncio.TimeoutError:
            raise ApplicationError("nextflow run did not signal completion within SLA")

        return JobResult(run_id=run_id, status=self._final_status)

    @workflow.signal
    def claim(self, claimer: str) -> None:
        self._claimed = True
        self._claimer = claimer

    @workflow.signal
    def mark_complete(self, status: str) -> None:
        self._completed = True
        self._final_status = status
```

Replaces the entire job-table state machine. The workflow IS the state.

## Architecture

```
┌──────────────────┐       ┌──────────────────┐
│  Admin / API     │       │  Nextflow weblog │
│  (FastAPI thin)  │       │  → /signal       │
└────────┬─────────┘       └────────┬─────────┘
         │                          │
         │  start_workflow          │  signal
         │                          │
         ▼                          ▼
   ┌────────────────────────────────────┐
   │      Temporal Server (OSS)         │
   │  (or self-hosted; very lightweight)│
   └────────────────┬───────────────────┘
                    │ task queue
                    ▼
   ┌────────────────────────────────────┐
   │    Workers (HPC nodes, anvil, etc) │
   │    activities: submit_to_executor, │
   │       check_status, cleanup        │
   └────────────────────────────────────┘
```

- **FastAPI shrinks to a thin facade**: `POST /jobs` → `start_workflow`; `POST /telemetry` → look up workflow by run_id and `signal`. No more `dispatch`, `reconcile`, `sweep` services.
- **Workers run on HPC nodes**, just like `nf-client daemon` does today, but with durable execution semantics: a worker dying doesn't lose state, the workflow just resumes elsewhere.
- **Postgres** still holds telemetry events and process metrics (the observability data, not the orchestration state). Temporal manages its own state in its own DB.

## Open questions for the next session

1. **Self-hosted Temporal server vs. Temporal Cloud?** Self-hosted on the existing big server is probably right (consistent with monode's "ops on one box" pattern). Temporal Cloud is fine but a recurring cost.
2. **Migration strategy.** Run the two systems side-by-side during cutover, or hard switch? Probably side-by-side: new jobs go through Temporal, existing pending jobs drain through the legacy state machine.
3. **What to do with telemetry events.** Keep the existing `telemetry_tbl`, `process_metrics`, `task_logs` tables — those are observational data, not orchestration state. Wire from the workflow's signal handler.
4. **`MARK_COMPLETE` sentinel: keep or drop?** Could replace with a Nextflow-native completion mechanism (workflow `onComplete` hook calling our signal endpoint directly). Cleaner.
5. **Workflow id schema.** Probably `{workflow_def_name}:{sample_id}:{attempt}` — content-addressed where possible.
6. **What about `dispatch/batch`?** With Temporal, the worker pulls from the task queue. The "claim a batch" semantics get replaced by worker-side concurrency tuning.
7. **Authentication for signal endpoint.** Nextflow weblog can't send auth headers. Options: (a) keep `/telemetry` unauthenticated and have it map weblog → signal; (b) use a per-workflow URL token in the workflow id.

## Migration phases (rough)

1. **Phase 0** — stand up Temporal server on the big box (`temporalio/server` + Postgres + UI). Keep current FastAPI alongside.
2. **Phase 1** — write the `SampleProcessingWorkflow` + activities. Run a single test job through it end-to-end.
3. **Phase 2** — port one workflow profile (e.g. `anvil`) to Temporal-backed. Keep others on legacy.
4. **Phase 3** — port all profiles. Legacy services (`dispatch`, `reconcile`, `sweep`) marked deprecated.
5. **Phase 4** — drain legacy queue, drop tables, remove `nf-client daemon`.

## Concrete first step

Stand up Temporal server + UI in a `compose/temporal/` directory in `monode/infrastructure/`, on a new Docker network or the existing `pg_and_duckdb_default`. Verify a hello-world workflow runs from a Python worker on the same box. That's the smoke test before any real design work.
