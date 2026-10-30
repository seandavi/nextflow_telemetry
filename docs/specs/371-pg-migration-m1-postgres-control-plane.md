# Spec #371: PG migration M1, Postgres control-plane schema

Issue: [#371](https://github.com/seandavi/nextflow_telemetry/issues/371). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381) (migrate the control
plane from Durable Objects to Postgres), step M1. Blocked by M0 (#370, ADR-0013, merged).
Unblocks M3 (#373), M4 (#374), M5 (#375) and M6 (#376).

## Goal

One Alembic migration creates the control-plane schema in the `nf_telemetry` database,
written as real Postgres (`timestamptz`, `jsonb`, foreign keys, check constraints). It
follows v2's model in `cf/src/schema.ts` and `cf/src/control-do.ts`, and adds:

- `jobs.priority`;
- deadline columns on `runs`;
- a `dead_letter.reason` that can't be null;
- the #209 `stopped` daemon status;
- `ingest_cursor`;
- the identity and study structure from the issue's 2026-10-30 section: `readset_runs`,
  `studies`, `study_aliases`, submission → snapshot provenance, and
  `curation_decisions`.

The same migration renames the eleven stale v1 tables to `v1_*`. The v1 Python code keeps
working against the renamed tables, so nothing a caller can see changes.

## Non-goals

- **No endpoints and no service functions.** Claim, reconcile, the transitions, bulk
  registration and the DLQ routes are M3. The deadline sweep, outage grace and its
  sweep-time record are M4, which adds its own migration if it needs a table. The ingester
  is M5. Export and import are M6.
- **No data copy.** The new tables start empty. M6 imports v2 state, including filling
  `readset_runs` from `samples.ncbi_accession`. #384 fills the run structure columns
  from omicidx.
- **v1 tables are not dropped.** That happens in M10. The v1 code is not rewritten or
  deleted.
- **No production migration.** This step does not run `alembic upgrade` on `pg_main`
  (see Risks for the deploy order).
- No config or env change. No change to `cf/`, `nf-client`, the frontend, the ETL or
  `health_report.py`.
- No `job_counts` table and no trigger (see Departures, 3).

## Departures from the issue and the design record

1. **Submission provenance is three columns, not one `source_snapshot_id`.**
   `metadata_source_snapshots` is keyed by `(source_id, asset_id, version)`, so no single
   column can reference it. `submissions` gets `source_snapshot_source_id`,
   `source_snapshot_asset_id` and `source_snapshot_version`, plus a composite foreign
   key, and all three are either null or set. The catalog module (`meta/catalog/`) gets
   no surrogate id, so a snapshot keeps exactly one identity.
2. **`samples.ncbi_accession` stays.** The issue says `readset_runs` "replaces" the
   `;`-joined string. Here `readset_runs` replaces it as the structure that run-level
   lookups use: by-srr, run → readset uniqueness, and run structure. The column itself
   stays, because the v2 wire contract returns it in sample and claim payloads, and
   ADR-0007 computes `readset_id` from it. M6's verification also recomputes ids from it.
   M3's registration writes both in one transaction.
3. **Status counts come from a covering index plus `count(*)`; there's no `job_counts`
   table or trigger.** The issue leaves the choice open ("measure before adding the
   trigger"). v2 kept counters to stay under Durable Object row-read billing, and that
   cost doesn't exist on Postgres. `ix_jobs_workflow_pk_status` makes
   `select workflow_pk, status, count(*) … group by 1, 2` an index-only scan. The
   measurement in the Test plan backs this choice. M6's import skips the exported
   `job_counts` rows.
4. **"Terminal job states" are enforced as consistency checks, not "never flips".**
   Operator routes legitimately move terminal jobs back to `pending`: v2's
   `requeueDeadLetter` moves `failed` to `pending`, and `resetJobsToPending` moves
   `completed` or `failed` to `pending`. So the database can't forbid leaving a terminal
   state. Instead it enforces what a terminal state must carry: `completed` ⇒
   `completed_at`, `failed` ⇒ `failed_at`, and `pending` ⇔ no `run_name`. "Terminal
   never flips from an event" (M5) belongs to the transition functions in M3.
5. **No "one active version per workflow" index.** v1 had
   `uq_one_active_version_per_workflow`, but v2 doesn't, and the multi-registration health
   report runs several active registrations at once (ADR-0010).
6. **New tables live in a new module, `src/nextflow_telemetry/control_db.py`, not in
   `db.py`.** The v1 Python names (`samples_tbl`, `jobs_tbl`, …) stay bound to the v1
   tables that about 570 call sites use until M10. `control_db.py` registers its tables
   on the same `db.metadata`.
7. **CONTEXT.md, "Study".** The glossary says a study is "not a first-class concept".
   ADR-0013 decision 9 and this issue make `studies` a table that collections reference.
   This spec updates that entry (see Files).
8. **ADR-0005 holds.** Membership is only `collection_samples`. There is no
   `metadata.cohort`, and nothing on `samples` names a collection.

## Design

### Files

| File | Change |
|---|---|
| `src/nextflow_telemetry/migrations/versions/20261030_fa0b1c2d_control_plane_schema.py` | **new.** The migration: `revision = "fa0b1c2d"`, `down_revision = "e9fa0b1c"` |
| `src/nextflow_telemetry/control_db.py` | **new.** SQLAlchemy Core `Table`s for the new tables on `db.metadata`. Names, types, nullability, constraint and index names exactly as in the migration |
| `src/nextflow_telemetry/db.py` | v1 tables renamed (see "Repointing v1 code"); three columns, an FK and a check on `submissions_tbl`; section comment |
| `src/nextflow_telemetry/migrations/env.py` | `import nextflow_telemetry.control_db  # noqa: F401` after the `db` import, so `target_metadata` is complete |
| `src/nextflow_telemetry/services/cohort.py`, `services/process_metrics.py`, `services/reconcile.py`, `services/collection.py`, `services/lifecycle.py`, `services/workflow.py`, `routers/runs.py`, `routers/task_logs.py`, `scripts/seed_test_jobs.py` | raw SQL table names and `ON CONFLICT` constraint names → `v1_*` (see below) |
| `tests/conftest.py` | `create_schema` and `clean_db` run `import nextflow_telemetry.control_db  # noqa: F401` before `from nextflow_telemetry.db import metadata` |
| `tests/test_cohorts_router.py`, `tests/test_integration.py` | raw SQL table names → `v1_*` |
| `tests/test_control_plane_migration.py` | **new.** Migration round trip, parity and constraint tests |
| `CONTEXT.md` | "Study" entry: a study is a row in `studies` with aliases in `study_aliases` (schemes listed below). A collection may reference one. Collection stays the unit of grouping and progress |

### Migration `fa0b1c2d`

Module docstring: "Control-plane schema (ADR-0013, #371); v1 tables renamed to v1_*."
The migration uses `op` and `sa` only. It doesn't import application modules, as the
existing migrations don't. It runs online only, because it reads `pg_catalog`; say so in
the docstring.

#### Step 1: rename v1

```python
V1_TABLES = ("telemetry", "task_executions", "samples", "collections",
             "collection_samples", "workflows", "jobs", "workflow_runs",
             "task_logs", "dead_letter", "daemon_agents")

def _owned_relations(table: str) -> list[tuple[str, str]]:
    """(kind, name) for every index and owned sequence of `table` in the current schema.
    kind is "INDEX" or "SEQUENCE", ready for ALTER <kind> … RENAME."""
```

`_owned_relations` runs one query through `op.get_bind().execute(sa.text(...), {"t": table})`:

```sql
select 'INDEX', indexname from pg_indexes
 where schemaname = current_schema() and tablename = :t
union all
select 'SEQUENCE', s.relname from pg_class s
  join pg_depend d on d.objid = s.oid
 where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass
   and d.refobjid = to_regclass(:t) and s.relkind = 'S' and d.deptype in ('a', 'i')
```

For each `t` in `V1_TABLES`:

1. `op.rename_table(t, f"v1_{t}")`;
2. for each `(kind, name)` in `_owned_relations(f"v1_{t}")`, run
   `op.execute(f'ALTER {kind} "{name}" RENAME TO "v1_{name}"')`.

Indexes and sequences are relations, so their names are unique across the schema, and a
new `samples` table couldn't otherwise create `samples_pkey` or `samples_id_seq`.
Renaming the index behind a PK or UNIQUE constraint renames the constraint too, and serial
defaults follow the sequence's oid. Both were checked on Postgres 16 while writing this
spec. FK and CHECK names are per table, so they don't collide and aren't renamed. The
tables that stay keep their names: `submissions`, `users`, `curated_studies`,
`curated_sample_annotations`, `etl_ingested` and `metadata_source*`.

#### Step 2: create tables

Conventions:
- Every timestamp is `sa.DateTime(timezone=True)`.
- `created_at` and `updated_at` are `nullable=False, server_default=sa.text("now()")`.
- Text columns are `sa.String()`, or `sa.Text()` where the column holds prose or YAML.
- Surrogate keys are `sa.Integer` with `primary_key=True` (serial, as in the rest of the
  schema), so M6 can insert explicit ids and then `setval`.
- Constraint and index names are exactly as given. PKs keep Postgres's default
  `<table>_pkey`.

The tables are created in this order:

**`studies`**
- `study_id` Integer PK
- `name` String not null; `uq_studies_name` unique
- `created_at`, `updated_at`

**`study_aliases`**
- `study_id` Integer not null FK `studies.study_id` `ondelete="CASCADE"`
- `scheme` String not null
- `value` String not null
- PK `(scheme, value, study_id)`
- `ix_study_aliases_study_id (study_id)`
- `ck_study_aliases_scheme`: `scheme in ('cmd_study','bioproject','insdc_study','asap_code','tracker_name','doi','pmid')`

The PK isn't unique on `(scheme, value)`, because one DOI or BioProject can back several
studies.

**`samples`** (v2 `samples`, ADR-0007)
- `id` Integer PK
- `sample_id` String not null (the md5 alias); `uq_samples_sample_id` unique
- `readset_id` String null
- `ncbi_accession` Text null (the canonical sorted `;` list)
- `biosample_id` String null
- `metadata` JSONB not null `server_default=sa.text("'{}'::jsonb")`
- `created_at`, `updated_at`
- `ix_samples_readset_id (readset_id)`. It's not unique: two md5 ids can name the same
  run set, as in v2.
- `ix_samples_biosample_id (biosample_id)`

**`readset_runs`**
- `run_accession` String PK (this is the issue's UNIQUE: a run belongs to exactly one
  readset)
- `readset_id` String not null
- `biosample`, `sra_sample`, `experiment`, `bioproject`: String null
- `created_at`, `updated_at`
- `ix_readset_runs_readset_id (readset_id)`
- `ck_readset_runs_run_accession`: `run_accession ~ '^[SED]RR[0-9]+$'` (ADR-0007 allows
  only run accessions as units)

There's no FK from `readset_runs.readset_id` to `samples.readset_id`: that column isn't
unique, and there's no `readsets` table.

**`collections`** (ADR-0005)
- `collection_id` String PK (the natural key, as in v2)
- `source` String not null `server_default="manual"`
- `label` String null
- `study_id` Integer null FK `studies.study_id`
- `created_at`, `updated_at`
- `ix_collections_study_id (study_id)`

**`collection_samples`**
- `collection_id` String not null FK `collections.collection_id`
- `sample_id` String not null FK `samples.sample_id`
- `created_at`
- PK `(collection_id, sample_id)` (one membership row per pair)
- `ix_collection_samples_sample_id (sample_id)`

**`workflows`** (ADR-0004, ADR-0010)
- `id` Integer PK
- `workflow_id`, `version`, `repository_url`, `revision`: String not null
- `manifest_version` String null
- `max_retries` Integer not null `server_default="3"`
- `status` String not null `server_default="active"`
- `description` Text null
- `params` JSONB not null `server_default=sa.text("'{}'::jsonb")`
- `collections` `postgresql.ARRAY(sa.String())` null (null means every sample)
- `sample_key` String not null `server_default="readset_id"`
- `created_at`, `updated_at`

Constraints:
- `uq_workflows_workflow_id_version` unique `(workflow_id, version)`
- `ck_workflows_status`: `status in ('active','paused','retired')`
- `ck_workflows_sample_key`: `sample_key in ('sample_id','readset_id')`
- `ck_workflows_collections`: `collections is null or cardinality(collections) > 0`. v2
  stores `[]` as null, and M3 does the same.
- `ck_workflows_max_retries`: `max_retries >= 0`

The default `sample_key` is `readset_id`, the value v2 inserts for new registrations.
M6 imports carry their stored value explicitly.

Trigger, created with `op.execute`. It enforces "params fixed per version": an update
that changes `params` or `sample_key` fails as a check violation, so callers see
`IntegrityError`.

```sql
CREATE FUNCTION workflows_bundle_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.params IS DISTINCT FROM OLD.params OR NEW.sample_key IS DISTINCT FROM OLD.sample_key THEN
    RAISE EXCEPTION 'workflow % %: params and sample_key are fixed per version (ADR-0010)',
      OLD.workflow_id, OLD.version USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_workflows_bundle_immutable BEFORE UPDATE OF params, sample_key ON workflows
  FOR EACH ROW EXECUTE FUNCTION workflows_bundle_immutable();
```

`control_db.py` doesn't attach the trigger. `metadata.create_all` (the test fixture)
therefore builds `workflows` without it. Tests that need the trigger use the migrated
database.

**`runs`** (v2 `runs`, plus deadlines in place of RunDO alarms)
- `run_name` String PK
- `run_id` String null
- `workflow_id` String null
- `workflow_version` String null
- `workflow_pk` Integer null FK `workflows.id`
- `revision` String null
- `status` String not null
- `executor_job_id` String null
- `claimed_at`, `submitted_at`, `started_at`, `completed_at`, `last_heartbeat_at`: timestamptz null
- `wait_seconds` Integer null
- `wrapper_exit_code` Integer null
- `last_known_slurm_state` String null
- `slurm_reason` Text null
- `nextflow_log_uploaded_at` timestamptz null
- `claim_expires_at`, `submit_deadline`, `liveness_deadline`: timestamptz null

Constraints:
- `ck_runs_status`: `status in ('claimed','submitted','running','completed','failed','expired')`
- `ck_runs_one_deadline`: `num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) <= 1`.
  A run waits on one deadline at a time, like RunDO's single alarm.
- `ck_runs_terminal_closed`: `status not in ('completed','failed','expired') or (completed_at is not null and num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) = 0)`

Indexes:
- `ix_runs_status_claimed_at (status, claimed_at)`
- `ix_runs_claim_expires_at (claim_expires_at) WHERE claim_expires_at IS NOT NULL`
- `ix_runs_submit_deadline (submit_deadline) WHERE submit_deadline IS NOT NULL`
- `ix_runs_liveness_deadline (liveness_deadline) WHERE liveness_deadline IS NOT NULL`

**`jobs`**
- `id` Integer PK
- `sample_id` String not null FK `samples.sample_id`
- `workflow_pk` Integer not null FK `workflows.id`
- `workflow_id`, `workflow_version`: String not null (denormalized, as in v2)
- `status` String not null `server_default="pending"`
- `run_name` String null FK `runs.run_name`
- `retry_count` Integer not null `server_default="0"`
- `priority` Integer not null `server_default="0"` (higher is claimed first)
- `created_at`
- `completed_at`, `failed_at`: timestamptz null
- `failure_reason` Text null

Constraints:
- `uq_jobs_sample_id_workflow_pk` unique `(sample_id, workflow_pk)`
- `ck_jobs_status`: `status in ('pending','claimed','submitted','running','completed','failed')`
- `ck_jobs_completed_at`: `status <> 'completed' or completed_at is not null`
- `ck_jobs_failed_at`: `status <> 'failed' or failed_at is not null`
- `ck_jobs_run_name`: `(status = 'pending') = (run_name is null)`
- `ck_jobs_retry_count`: `retry_count >= 0`

Indexes:
- `ix_jobs_pending_claim (workflow_pk, priority DESC, created_at, id) WHERE status = 'pending'`.
  This is the claim order M3 uses: priority desc, then created_at, then id.
- `ix_jobs_workflow_pk_status (workflow_pk, status)`. Status counts and per-registration
  listing use it.
- `ix_jobs_run_name (run_name)`

In the migration, write the DESC column as
`op.create_index(..., ["workflow_pk", sa.text("priority DESC"), "created_at", "id"], postgresql_where=sa.text("status = 'pending'"))`.
In `control_db.py`, define the index after the `Table`, with
`jobs_tbl.c.priority.desc()` and `postgresql_where=jobs_tbl.c.status == "pending"`.

**`dead_letter`**
- `id` Integer PK
- `job_id` Integer not null FK `jobs.id`; `uq_dead_letter_job_id` unique
- `run_name` String null FK `runs.run_name`
- `sample_id`, `workflow_id`, `workflow_version`: String not null
- `reason` Text not null (#299 asks for a reason that can be queried and filtered; v2
  always writes one)
- `created_at`
- `resolved_at` timestamptz null

There are no further indexes, because the table holds tens of rows.

**`daemons`** (#209)
- `agent_id` String PK
- `hostname` String not null
- `workflow_id` String null
- `profile` String null
- `nf_client_version` String null
- `config_yaml` Text null
- `mode` String null
- `batch_size` Integer null
- `max_concurrent_runs` Integer null
- `active_runs` Integer not null `server_default="0"`
- `status` String not null `server_default="idle"`
- `last_seen_at` timestamptz not null
- `started_at` timestamptz not null
- `ck_daemons_status`: `status in ('idle','running','stopped')`. `stale` is computed when
  read and never stored.
- `ix_daemons_last_seen_at (last_seen_at)`, used for pruning

**`run_task_counts`**
- `run_name` String not null FK `runs.run_name`
- `status` String not null
- `n` Integer not null
- PK `(run_name, status)`
- `ck_run_task_counts_n`: `n >= 0`

**`run_failed_tasks`**
- `id` Integer PK
- `run_name` String not null FK `runs.run_name`
- `process` String not null
- `sample_id` String null
- `exit_code` String null
- `task_hash` String null
- `attempt` Integer null
- `error_action` String null
- `utc_time` timestamptz not null
- `ix_run_failed_tasks_run_name (run_name)`

**`ingest_cursor`** (M5)
- `stream` String PK, for example `telemetry/events`
- `object_key` String not null: the last R2 key applied. SinkDO keys
  (`telemetry/events/dt=YYYY-MM-DD/HHMMSS-xxxxxxxx.ndjson.gz`) sort by time.
- `line_offset` BigInteger not null `server_default="0"`: NDJSON lines of `object_key`
  already applied. The column isn't called `offset` because that's a reserved word.
- `updated_at`
- `ck_ingest_cursor_line_offset`: `line_offset >= 0`

**`curation_decisions`** (append-only; the latest `at` per subject wins)
- `id` Integer PK
- `subject_type` String not null
- `subject_id` String not null
- `decision` String not null
- `reason` Text not null
- `actor` String not null
- `at` timestamptz not null `server_default=now()`
- `ck_curation_decisions_subject_type`: `subject_type in ('study','readset','run')`
- `ck_curation_decisions_decision`: `decision in ('include','exclude')`
- `ix_curation_decisions_subject (subject_type, subject_id, at)`

There's no FK on `subject_id`, because the subject is polymorphic.

#### Step 3: submission provenance

Add these to `submissions`, all nullable:
- `source_snapshot_source_id` `sa.String(80)`
- `source_snapshot_asset_id` `sa.String(80)`
- `source_snapshot_version` `sa.String(300)`

Then create:
- FK `fk_submissions_source_snapshot`, from those three columns to
  `metadata_source_snapshots (source_id, asset_id, version)`;
- check `ck_submissions_source_snapshot`:
  `num_nulls(source_snapshot_source_id, source_snapshot_asset_id, source_snapshot_version) in (0, 3)`.

#### Downgrade

Run the reverse, in this order:

1. Drop the submissions check, FK and three columns.
2. Drop the trigger, then the function.
3. Drop the new tables in reverse dependency order: `curation_decisions`,
   `ingest_cursor`, `run_failed_tasks`, `run_task_counts`, `daemons`, `dead_letter`,
   `jobs`, `runs`, `workflows`, `collection_samples`, `collections`, `readset_runs`,
   `samples`, `study_aliases`, `studies`.
4. For each `t` in `V1_TABLES`: for each `(kind, name)` in
   `_owned_relations(f"v1_{t}")` where `name.startswith("v1_")`, rename it to
   `name[3:]`. Then `op.rename_table(f"v1_{t}", t)`.

### `control_db.py`

```python
"""Control-plane tables (ADR-0013, #371). Defined on db.metadata; the v1 tables
they replace are the v1_* tables in db.py until M10."""
from .db import metadata
```

It defines one `Table` per new table, named `<table>_tbl`: `studies_tbl`,
`study_aliases_tbl`, `samples_tbl`, `readset_runs_tbl`, `collections_tbl`,
`collection_samples_tbl`, `workflows_tbl`, `runs_tbl`, `jobs_tbl`, `dead_letter_tbl`,
`daemons_tbl`, `run_task_counts_tbl`, `run_failed_tasks_tbl`, `ingest_cursor_tbl` and
`curation_decisions_tbl`. It also defines `Index("ix_jobs_pending_claim", …)`. Use the
same SQLAlchemy idioms as `db.py`:
- `Column`;
- `CheckConstraint(…, name=…)`, `UniqueConstraint`, `ForeignKey`, `Index(…, postgresql_where=…)`;
- `JSONB` and `ARRAY` from `sqlalchemy.dialects.postgresql`;
- `server_default=text(...)`.

Callers in M3 and later import from `nextflow_telemetry.control_db`.

### Repointing v1 code

In `db.py`, each of the 11 v1 `Table(...)` calls gets the table name `"v1_<name>"`. Python
variable names stay as they are. Also:
- `ForeignKey` targets get the `v1_` prefix: `"v1_samples.sample_id"`, `"v1_workflows.id"`,
  `"v1_workflow_runs.run_name"`, `"v1_jobs.id"`, `"v1_telemetry.id"`,
  `"v1_collections.collection_id"`.
- Every explicit `Index` and `UniqueConstraint` name on those tables gets the `v1_`
  prefix, so `db.py` matches what the migration leaves in production, for example
  `v1_uq_job_composite` and `v1_ix_jobs_status`.
- A comment heads the section: "v1 tables, renamed by #371, dropped in M10. Live
  control-plane tables are in control_db.py."

`submissions_tbl` gets the three nullable columns, plus
`ForeignKeyConstraint([...], ["metadata_source_snapshots.source_id", "metadata_source_snapshots.asset_id", "metadata_source_snapshots.version"], name="fk_submissions_source_snapshot")`
and the check.

Raw SQL and constraint names that move to `v1_*`. These are every hit found by grepping
for SQL table references and `ON CONFLICT … CONSTRAINT`:

| File | Change |
|---|---|
| `services/cohort.py` | `collections`, `collection_samples`, `jobs`, `workflows`, `telemetry` in `text()` SQL |
| `services/process_metrics.py` | `workflow_runs` and `telemetry` in `running()` only. That's the one method that queries Postgres (`self.engine`). Every other query goes through `self.archive.fetch`, where `telemetry` and `task_executions` are DuckDB views (`event_archive.py`), and must not change |
| `services/reconcile.py` | `jobs`, `samples`, `workflows`; `ON CONSTRAINT v1_uq_job_composite` |
| `routers/runs.py` | `INSERT INTO v1_task_logs … ON CONSTRAINT v1_uq_task_log` (×2) |
| `routers/task_logs.py` | `v1_task_logs` (insert and select), `v1_uq_task_log` |
| `services/collection.py` | `constraint="v1_uq_collection_sample"` |
| `services/lifecycle.py` | `constraint="v1_uq_dlq_job_id"` |
| `services/workflow.py` | `constraint="v1_uq_workflow_id_version"` |
| `scripts/seed_test_jobs.py` | `samples`, `jobs` SQL; `constraint="v1_uq_job_composite"` |
| `tests/test_cohorts_router.py`, `tests/test_integration.py` | `DELETE FROM` / `SELECT … FROM` table names |

The implementer re-runs the grep and covers any new hit:
`grep -rnE "\b(FROM|JOIN|INTO|UPDATE|from|join|into|update)\s+(telemetry|task_executions|samples|collections|collection_samples|workflows|jobs|workflow_runs|task_logs|dead_letter|daemon_agents)\b"`,
plus `grep -rn "uq_\|ix_"` outside `db.py` and `migrations/`. A hit only changes if the
query runs on the Postgres engine. SQL run through `EventArchive` (DuckDB over R2:
`services/event_archive.py` and `ProcessMetricsService` via `self.archive.fetch`) keeps
its names.

### Endpoints, config, callers

- **Endpoints:** none added or changed.
- **Config and env:** none. The M4 deadline values are M4's.
- **Callers:** `nf-client`, `etl/v2.py`, the frontend and `health_report.py` talk to the v2
  Worker over HTTP, so none of them is affected. The FastAPI catalog code (submissions,
  samples, cohorts, curated, process metrics and the v1 routes) behaves as before,
  against the `v1_*` tables.

### Notes for M3 and M4 (column semantics, not built here)

- **Deadlines.**
  - Claim sets `claim_expires_at` to claimed_at + `CLAIM_TTL_MINUTES`.
  - `submitted` clears it and sets `submit_deadline` to submitted_at + `SUBMIT_BACKSTOP_HOURS`.
  - `wrapper_started` and `heartbeat` clear the others and set `liveness_deadline` to
    now + `LIVENESS_MINUTES`.
  - Closing a run clears all three. That is the order of RunDO's arm, heartbeat and
    finalize calls, and `ck_runs_one_deadline` and `ck_runs_terminal_closed` hold M3 and
    M4 to it.
- **Daemons (#209).**
  - A heartbeat for a row whose `status = 'stopped'`, or whose `last_seen_at` is older
    than the 2-minute active window, sets `started_at = now()`.
  - `status = 'stopped'` is sent on shutdown.
  - `GET /daemons` deletes rows whose `last_seen_at` is more than N days old.
- **Priority:** M3 and #298 decide how priority is set (at reconcile, or by an operator).
  The column defaults to 0.

## Acceptance criteria

- [ ] `alembic upgrade head` on an empty scratch database succeeds and leaves the 15 new
      tables, the 11 `v1_*` tables and the 3 new `submissions` columns.
- [ ] `alembic downgrade e9fa0b1c` from head succeeds. Afterwards the 11 original table
      names and their index names are exactly as before the upgrade, and no new table or
      `workflows_bundle_immutable` function remains. A later `alembic upgrade head`
      succeeds again.
- [ ] Rows in v1 tables survive the round trip. Inserts into `v1_samples` after the
      upgrade still get ids from the renamed sequence.
- [ ] After the upgrade, no index or sequence on a `v1_*` table has a name without the
      `v1_` prefix.
- [ ] For every new table and for `submissions`, the migrated database's columns (name
      and nullability) match the `control_db.py` / `db.py` `Table`.
- [ ] Each of these is rejected with `sqlalchemy.exc.IntegrityError`:
  - a duplicate `(workflow_id, version)`;
  - a duplicate `(collection_id, sample_id)` membership;
  - a duplicate `readset_runs.run_accession`, and a malformed run accession;
  - a job status outside the six;
  - `completed` without `completed_at`, and `failed` without `failed_at`;
  - `pending` with a `run_name`, and `claimed` without one;
  - a run status outside the six;
  - a terminal run with a deadline set, and a terminal run without `completed_at`;
  - a run with two deadlines;
  - an empty `workflows.collections` array;
  - a daemon status `stale`;
  - an unknown `study_aliases.scheme`;
  - a `curation_decisions.decision` other than include or exclude;
  - a submission with one or two of the three snapshot columns set;
  - a submission whose snapshot reference doesn't exist;
  - an update that changes `workflows.params` or `workflows.sample_key`.
- [ ] Each of these is accepted:
  - an update of `workflows.revision`;
  - a daemon status `stopped`;
  - a submission with all three snapshot columns referencing an existing snapshot.
- [ ] `uv run pytest` passes, including the existing suite against the `v1_*` tables.
- [ ] `uv run pytest tests/test_source_catalog.py` passes.
- [ ] `uv run mypy src/nextflow_telemetry` passes.
- [ ] The job status-count measurement (Test plan) is recorded in the PR description.

## Test plan

New file `tests/test_control_plane_migration.py`. It uses the session `db_asyncpg_url`
fixture (testcontainers Postgres 16) and the existing style: `asyncio.run` around
`create_async_engine`.

- **Helpers.**
  - `_scratch_db(name) -> str`: connects with `create_async_engine(db_asyncpg_url, isolation_level="AUTOCOMMIT")`,
    runs `DROP DATABASE IF EXISTS {name} WITH (FORCE)` then `CREATE DATABASE {name}`, and
    returns `make_url(db_asyncpg_url).set(database=name).render_as_string(hide_password=False)`.
  - `_alembic(url, *args)`: runs
    `subprocess.run([sys.executable, "-m", "alembic", *args], cwd=<repo root>, env={**os.environ, "SQLALCHEMY_URI": url}, check=True)`.
    The repo root is `Path(__file__).resolve().parents[1]`. `env.py` reads
    `SQLALCHEMY_URI` through `config.settings`.
- **`test_upgrade_downgrade_round_trip`**, on database `nf_m1_roundtrip`:
  1. `_alembic(url, "upgrade", "e9fa0b1c")`.
  2. Insert one v1 `samples` row, one `workflows` row and one `jobs` row with raw SQL.
  3. Record the index names of the 11 tables from `pg_indexes`.
  4. `upgrade head`, then assert the acceptance items for upgrade, prefixes, row survival
     and sequence.
  5. `downgrade e9fa0b1c`, then assert the index names equal the recorded set, the new
     tables and the function are gone, and the rows are present.
  6. `upgrade head` again.
- **Module fixture `migrated_url`:** database `nf_m1_constraints`, upgraded to head once.
- **`test_columns_match_table_definitions`:** for each new table name and `submissions`,
  `{c["name"]: c["nullable"] for c in inspect(con).get_columns(t)}` equals
  `{c.name: c.nullable for c in metadata.tables[t].columns}`.
- **Constraint tests:** one test per group (workflows, membership, readset_runs, jobs,
  runs, daemons and aliases, curation, submissions). Each opens one connection on
  `migrated_url`, seeds the minimum parent rows, and checks each rejected statement with
  `async with con.begin_nested(): await con.execute(stmt)` inside
  `pytest.raises(IntegrityError)`. It then rolls the outer transaction back. Statements
  use the `control_db` / `db` `Table` objects (`insert(...)`, `update(...)`). A
  submissions test seeds a `metadata_sources` row, an asset and a snapshot.
- **Existing suite:** `uv run pytest`. Its fixtures build the test database with
  `metadata.create_all`, which now creates the `v1_*` tables and the new tables, so the
  v1 tests run against `v1_*`.
- **Measurement (manual, recorded in the PR, not committed):**
  1. On the `nf_m1_constraints` database (or any head database), insert 76,000 samples,
     10 workflows and 760,000 jobs (10× production) with `generate_series`, statuses
     spread over the six values.
  2. Run `VACUUM ANALYZE jobs`, then
     `EXPLAIN (ANALYZE, BUFFERS) SELECT workflow_pk, status, count(*) FROM jobs GROUP BY 1, 2`.
  3. Record the plan type and execution time. Under 100 ms confirms Departure 3. Over
     100 ms means reporting it to the supervisor before M3, not adding a trigger in M1.
- **Commands:**
  - `uv run pytest tests/test_control_plane_migration.py`
  - `uv run pytest`
  - `uv run pytest tests/test_source_catalog.py`
  - `uv run mypy src/nextflow_telemetry`

## Risks

- **Deploy order.** Once this merges, the code expects `v1_*` names.
  `just deploy-onclappc02` doesn't run migrations. A deploy without the migration breaks
  the catalog service, because its tables are missing. A migration without the deploy
  breaks it too, because the old code writes `metadata_` into the new `samples` table.
  Production order: `docker compose stop nf_telemetry_api`, `alembic upgrade head` (from
  the host, with the `@127.0.0.1` override in `deploy/onclappc02/README.md`), then deploy.
  Put this in the PR description. Running it is outside M1.
- **M6 import meets real data.** `ck_jobs_run_name`, `ck_jobs_completed_at`,
  `readset_runs`'s unique run accession and `ck_runs_terminal_closed` assume v2's
  invariants held. If production violates one of them, for example a run in two
  readsets, which is checked by hand today, the import fails loudly. That's the intent,
  but it may need data fixes in M6.
- **v1 index-name drift in tests.** `db.py` v1 columns declared with `index=True` get
  `ix_v1_<table>_<col>` names under `create_all`, but `v1_ix_<table>_<col>` in
  production. Nothing references those names. They go away in M10.
- **The migration runs online only** (`--sql` isn't supported), because the rename reads
  `pg_catalog`.
- **Two `samples_tbl` symbols** (`db` for v1, `control_db` for the new table) exist
  until M10. Import paths keep them apart. M10 deletes the v1 one.
