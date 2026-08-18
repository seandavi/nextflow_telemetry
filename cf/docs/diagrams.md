# nf_telemetry v2 — data model and object topology

Two views of `cf/`: what ControlDO stores, and how the three Durable Object classes relate.

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
