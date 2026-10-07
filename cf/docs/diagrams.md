# nf_telemetry v2 — data model, object topology, and lifecycles

Four views of `cf/`: what ControlDO stores, how the three Durable Object classes relate,
what one run looks like end to end, and how the three timers close a run nobody reports on.
Diagrams are Mermaid and render on GitHub. For slides, the same topology as a validated
standalone image, [`architecture.svg`](architecture.svg):

![nf_telemetry v2 architecture](architecture.svg)

---

## ControlDO — SQLite schema

One Durable Object holds the entire relational control plane. This is the v1 Postgres
schema minus the tables that only existed because Postgres was also the analytics store:
`telemetry` and `task_executions` are now NDJSON on R2, and `task_logs` are R2 objects.

```mermaid
erDiagram
    samples {
        integer id PK "autoincrement"
        text sample_id UK "md5 of sorted, deduped SRR set"
        text ncbi_accession "canonical SRRs, semicolon-joined"
        text biosample_id "annotation only, not identity"
        text metadata "JSON"
        text created_at "ISO-8601 UTC"
        text updated_at
    }

    collections {
        text collection_id PK "PRJNA… / SRP… / free text"
        text source "bioproject | sra_study | manual"
        text label
        text created_at
        text updated_at
    }

    collection_samples {
        text collection_id PK "half of composite key"
        text sample_id PK "half of composite key"
        text created_at
    }

    workflows {
        integer id PK "the workflow_pk on the wire"
        text workflow_id "logical name, e.g. cmgd"
        text version "bumping it forces reprocessing"
        text repository_url
        text revision "mutable, no rerun"
        text manifest_version
        integer max_retries "default 3, the DLQ budget"
        text status "active | paused | retired"
        text description
        text created_at
        text updated_at
    }

    jobs {
        integer id PK
        text sample_id FK "one sample"
        integer workflow_pk FK "one workflow version"
        text workflow_id "denormalised for the claim query"
        text workflow_version "denormalised for the claim query"
        text status "pending|claimed|submitted|running|completed|failed"
        text run_name FK "null unless under a run"
        integer retry_count "vs workflows.max_retries"
        text created_at "claim order"
        text completed_at
        text failed_at
        text failure_reason
    }

    job_counts {
        integer workflow_pk PK "half of composite key"
        text status PK "half of composite key"
        integer n "maintained by trigger, never by hand"
    }

    runs {
        text run_name PK "UUIDv7, minted at claim"
        text run_id "Nextflow's own UUID, from weblog"
        text workflow_id
        text workflow_version
        integer workflow_pk FK
        text revision
        text status "claimed|submitted|running|completed|expired|failed"
        text executor_job_id "SLURM job id"
        text claimed_at
        text submitted_at "queue wait starts"
        text started_at "queue wait ends"
        text completed_at
        text last_heartbeat_at "pushed from RunDO, throttled"
        integer wait_seconds "scheduler queue wait"
        integer wrapper_exit_code "non-zero wins over status"
        text last_known_slurm_state
        text slurm_reason "doubles as the close reason"
        text nextflow_log_uploaded_at "absence means ended-no-log"
    }

    dead_letter {
        integer id PK
        integer job_id UK "one DLQ row per job, ever"
        text run_name
        text sample_id
        text workflow_id
        text workflow_version
        text reason
        text created_at
        text resolved_at "set on requeue, row is kept"
    }

    daemons {
        text agent_id PK "hostname:workflow_id"
        text hostname
        text workflow_id "comma-separated claim filter"
        text profile "anvil | alpine | standard"
        text nf_client_version
        text config_yaml "sanitised"
        text mode "local | slurm | pbs | lsf"
        integer batch_size
        integer max_concurrent_runs
        integer active_runs
        text status "idle | running"
        text last_seen_at "active if within 2 min"
        text started_at "never overwritten"
    }

    collections ||--o{ collection_samples : "membership is the truth"
    samples ||--o{ collection_samples : "many-to-many"
    samples ||--o{ jobs : "one job per sample per version"
    workflows ||--o{ jobs : "workflow_pk"
    workflows ||--o{ job_counts : "workflow_pk"
    workflows ||--o{ runs : "workflow_pk"
    runs ||--o{ jobs : "run_name, null until claimed"
    jobs ||--o| dead_letter : "job_id"
```

**Reading notes**

- **No foreign keys are declared.** SQLite would enforce them, but every write already
  goes through one module in one single-threaded object, and FK enforcement would turn
  the retired-version job purge into a cascade problem. The relationships above are real
  and maintained in code; they are not constraints.
- `jobs` carries `workflow_id` and `workflow_version` alongside `workflow_pk`. That
  denormalisation is deliberate: the claim query orders by `(workflow_id,
  workflow_version, created_at)` and must not join to do it.
- `job_counts` is derived state, written **only** by the three triggers on `jobs`. It
  exists so job-summary is a 6-row read instead of a 100k-row scan.
- `daemons` stands alone — it describes the fleet, not the work.
- `jobs` is the only table that grows as `samples × versions`; retired versions are
  archived to R2 and purged, which bounds it at `samples × active versions`.

---

## Object topology

```mermaid
flowchart TB
    subgraph HPC["HPC clusters · Anvil, Alpine — unchanged from v1"]
        DAEMON["nf-client daemon"]
        WRAPPER["SLURM run wrapper"]
        NEXTFLOW["nextflow -with-weblog"]
    end

    CRON["cron trigger<br/>06:00 UTC daily"]

    API["<b>API Worker</b> · Hono<br/>×1 deployment, stateless<br/>parse → route → forward"]

    subgraph DO["Durable Objects"]
        CONTROL["<b>ControlDO</b> · ×1 singleton<br/>SQLite: samples, collections,<br/>workflows, jobs, runs, DLQ, daemons<br/><i>owns every status write</i>"]
        RUN["<b>RunDO</b> · ×1 per live run<br/>one alarm = one deadline<br/>absorbs heartbeats<br/><i>deleted at close</i>"]
        SINK["<b>SinkDO</b> · ×1 singleton<br/>event buffer + process counters<br/>flush at 500 rows or 60s"]
    end

    R2[("<b>R2</b> · nf-telemetry<br/>ledger/ · archive/ · snapshots/<br/>telemetry/events/ · nextflow-logs/ · task-logs/")]

    DAEMON -->|"claim, confirm submitted"| API
    WRAPPER -->|"lifecycle events, heartbeats, .nextflow.log"| API
    NEXTFLOW -->|"weblog events"| API
    CRON --> API

    API -->|"all state reads and writes"| CONTROL
    API -->|"arm deadline · heartbeat"| RUN
    API -->|"every event, both sources"| SINK
    API -->|"log blobs"| R2

    RUN -->|"alarm fires:<br/>expireClaim / closeRun"| CONTROL
    RUN -.->|"last_heartbeat_at,<br/>throttled to 5 min"| CONTROL

    CONTROL -->|"run ledger, job archive,<br/>daily snapshot"| R2
    SINK -->|"gzipped NDJSON,<br/>partitioned by date"| R2
```

| Object | Count | Lifetime | Why it is its own object |
|---|---|---|---|
| **ControlDO** | 1 | forever | Single-threaded + synchronous SQL means claims are atomic with no locking, and everything stays joinable in one query. |
| **RunDO** | 1 per live run | claim → close | A Durable Object has exactly one alarm, so per-run deadlines need per-run objects. It also keeps heartbeats — the highest-frequency write — off the object every claim serializes on. |
| **SinkDO** | 1 | forever | Buffers writes so events land on R2 in batches rather than one object per event, and it is already the one place that sees every process event, so it owns the in-flight counters. |

**Reading notes**

- The dashed edge is the only *optional* write: RunDO pushes `last_heartbeat_at` upstream
  at most once every 5 minutes. A heartbeat that arrives between pushes touches nothing
  but RunDO's own alarm.
- RunDO never writes terminal state. When its alarm fires it calls ControlDO, which no-ops
  if the run already closed — so the weblog, the timer, and an operator all converge on
  one close path.
- Nothing here is a cron sweeper. The daily trigger takes a snapshot and reclaims retired
  jobs; claim expiry and heartbeat death are RunDO alarms, which is what let the v1
  `requeue-expired`, `expire-stale-runs` and `heartbeat-watchdog` endpoints become no-ops.

---

## One run, happy path

Every arrow into the Worker is a v1 path. The daemon and the weblog are the only callers
that need to know the server exists; the wrapper is best-effort and can never fail a run.

```mermaid
sequenceDiagram
    autonumber
    participant D as nf-client daemon
    participant W as SLURM wrapper
    participant N as nextflow -with-weblog
    participant API as API Worker
    participant C as ControlDO
    participant R as RunDO (run_name)
    participant S as SinkDO
    participant R2

    D->>API: POST /dispatch/batch
    API->>C: claimBatch(limit, workflow filter)
    C-->>API: run_name + jobs (status: claimed)
    API->>R: arm("claim", CLAIM_TTL 5 min)
    API-->>D: 200 ClaimedBatch

    D->>D: sbatch wrapper job
    D->>API: POST /dispatch/submitted
    API->>C: markSubmitted(run_name, slurm job id)
    API->>R: arm("backstop", SUBMIT_BACKSTOP 48 h)

    Note over W: SLURM starts the job
    W->>API: POST /runs/{run}/event wrapper_started
    API->>C: applyRunEvent
    API->>R: heartbeat(LIVENESS 10 min)
    W->>API: pre_nextflow (wait_seconds)
    loop every 60 s
        W->>API: heartbeat
        API->>R: heartbeat → re-arm alarm
        R--)C: last_heartbeat_at (at most every 5 min)
    end

    W->>N: nextflow run ... -name run_name
    N->>API: POST /telemetry started
    API->>C: markRunning(run_name, run_id)
    API->>S: write(event)
    loop per task
        N->>API: process_submitted / started / completed
        API->>S: write(event) + in-flight counters
    end
    N->>API: process_completed MARK_COMPLETE (sample)
    API->>C: completeSample(run_name, sample_id) → job completed
    N->>API: POST /telemetry completed
    API->>C: closeRun("completed") → sweep incomplete jobs, ledger
    C->>R2: ledger/YYYY/MM/{run_name}.jsonl
    API->>R: finalize() → alarm cancelled, storage deleted

    W->>API: wrapper_exited (exit_code) + .nextflow.log multipart
    API->>R2: nextflow-logs/{run}/nextflow.log, wrapper_output.log
    API->>C: closeRun(...) → already_closed, no-op
    S->>R2: telemetry/events/dt=…/*.ndjson.gz (500 rows or 60 s)
```

**Reading notes**

- Steps 3, 8 and 13 are the three phases a RunDO can be in. Each `arm`/`heartbeat`
  replaces the previous alarm; a RunDO has exactly one.
- The run is closed at step 25 by the weblog, not at step 29 by the wrapper. `closeRun`
  is idempotent, so whichever terminal signal lands first wins and the rest are no-ops.
  This is why `wrapper_exited` with a non-zero code cannot un-complete a run that
  Nextflow already reported as completed.
- `MARK_COMPLETE` (step 22) completes the job the moment it lands. Jobs still not
  completed when the run closes are swept: `retry_count < max_retries` → back to
  `pending` with `retry_count + 1`; otherwise `failed` plus a `dead_letter` row.
- Only step 1, 6 and the operator routes need the bearer token. `/telemetry` and
  `/runs/{run}/event` are open because neither the weblog reporter nor the wrapper can
  carry one (`authExempt` in `index.ts`).

---

## The three timers

What happens when the happy path stops. Each is a RunDO alarm; none is a sweeper.
Verified on the deployed worker 2026-09-21 with a real clock (see `cf/README.md`).

```mermaid
sequenceDiagram
    participant D as daemon / wrapper
    participant API as API Worker
    participant R as RunDO
    participant C as ControlDO
    participant R2

    rect rgb(245, 245, 235)
    Note over D,R2: 1 · Claim expiry — daemon claimed but never confirmed submitted
    D->>API: POST /dispatch/batch
    API->>R: arm("claim", 5 min)
    Note over D: daemon dies before sbatch
    R->>R: alarm fires at claimed_at + 5:00
    R->>C: expireClaim(run_name)
    C->>C: run → expired · jobs → pending, retry_count unchanged
    Note right of C: nothing was attempted, so no retry is burned
    end

    rect rgb(235, 245, 245)
    Note over D,R2: 2 · Submit backstop — sbatch accepted, wrapper never started
    D->>API: POST /dispatch/submitted
    API->>R: arm("backstop", 48 h)
    Note over D: SLURM holds the job past the backstop
    R->>R: alarm fires
    R->>C: closeRun("failed", "no wrapper activity within submit backstop")
    C->>C: sweep: requeue or dead-letter per retry budget
    C->>R2: ledger record
    end

    rect rgb(245, 235, 235)
    Note over D,R2: 3 · Liveness — wrapper started, then heartbeats stopped
    D->>API: heartbeat
    API->>R: heartbeat(10 min) → re-arm
    Note over D: SIGKILL / node failure / walltime
    R->>R: alarm fires at last_heartbeat + 10:00
    R->>C: closeRun("failed", "presumed dead: heartbeats stopped")
    C->>C: sweep: requeue or dead-letter per retry budget
    C->>R2: ledger record
    end

    Note over R: after any alarm: storage.deleteAll() — the object is gone
```

| Timer | Armed by | Fires after | Terminal state | Retry burned? |
|---|---|---|---|---|
| claim | `POST /dispatch/batch` | `CLAIM_TTL_MINUTES` (5) | run `expired`, jobs `pending` | no |
| backstop | `POST /dispatch/submitted` | `SUBMIT_BACKSTOP_HOURS` (48) | run `failed`, jobs swept | yes |
| liveness | `wrapper_started`, every `heartbeat` | `LIVENESS_MINUTES` (10) | run `failed`, jobs swept | yes |

All three are cancelled by `finalize()`, which every terminal path calls: weblog
`completed`, `wrapper_exited`, `POST /admin/close-run`, and `POST /admin/reset`.

---

## State machines

Every transition below is one ControlDO method, and the `job_counts` triggers fire on
each. There are no other writers. Both machines are the v1 ones; only the owner changed.

### Job

```mermaid
stateDiagram-v2
    [*] --> pending : reconcileJobs
    pending --> claimed : claimBatch
    claimed --> pending : expireClaim (claim TTL, no retry burned)
    claimed --> submitted : markSubmitted
    submitted --> running : markRunning (weblog started)
    submitted --> completed : completeSample (MARK_COMPLETE)
    running --> completed : completeSample (MARK_COMPLETE)
    submitted --> pending : closeRun sweep, retry_count < max_retries
    running --> pending : closeRun sweep, retry_count < max_retries
    submitted --> failed : closeRun sweep, budget exhausted
    running --> failed : closeRun sweep, budget exhausted
    failed --> pending : requeueDeadLetter (operator)
    completed --> [*]
    note right of failed : also writes a dead_letter row
    note right of completed : terminal. A late MARK_COMPLETE or a failing wrapper never flips it back
```

### Run

```mermaid
stateDiagram-v2
    [*] --> claimed : claimBatch (mints run_name, arms claim timer)
    claimed --> expired : expireClaim (claim alarm)
    claimed --> submitted : markSubmitted (arms backstop)
    submitted --> running : markRunning (weblog started)
    submitted --> completed : closeRun "completed"
    running --> completed : closeRun "completed"
    submitted --> failed : closeRun "failed"
    running --> failed : closeRun "failed"
    expired --> [*]
    completed --> [*]
    failed --> [*]
    note right of completed : callers of closeRun: weblog completed, wrapper_exited 0, admin close-run
    note right of failed : callers of closeRun: wrapper_exited non-zero, backstop alarm, liveness alarm, admin close-run
```

**Reading notes**

- A run's `status` and a job's `status` are independent columns. A run can be
  `completed` while its job is `failed`: Nextflow finished, but `MARK_COMPLETE` never
  fired for that sample, so the sweep failed it. `fail-mark` in `nf_testing` produces
  exactly this.
- `closeRun` on an already-terminal run returns `already_closed: true` and changes
  nothing. That guard is what makes the four callers of each terminal state safe to
  race.
- `expired` is the only run state that costs the jobs nothing. Everything else that
  ends a run without `MARK_COMPLETE` spends one unit of the retry budget.
