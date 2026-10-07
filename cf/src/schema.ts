/**
 * ControlDO SQLite schema — the v1 Postgres tables, minus everything that was
 * only there because Postgres was also the analytics store:
 *
 *   telemetry / task_executions  -> NDJSON on R2 (SinkDO), queried later
 *   task_logs                    -> objects on R2 (blobs never belonged in a DB)
 *
 * Timestamps are ISO-8601 UTC strings: lexicographically ordered, JSON-native,
 * and comparable in SQL without a date type.
 */
export const SCHEMA = `
create table if not exists samples (
  id             integer primary key autoincrement,
  sample_id      text not null unique,
  ncbi_accession text,
  biosample_id   text,
  metadata       text,
  created_at     text not null,
  updated_at     text not null
);
create index if not exists samples_biosample on samples (biosample_id);

create table if not exists collections (
  collection_id text primary key,
  source        text not null default 'manual',
  label         text,
  created_at    text not null,
  updated_at    text not null
);

create table if not exists collection_samples (
  collection_id text not null,
  sample_id     text not null,
  created_at    text not null,
  primary key (collection_id, sample_id)
);
create index if not exists collection_samples_sample on collection_samples (sample_id);

create table if not exists workflows (
  id               integer primary key autoincrement,
  workflow_id      text not null,
  version          text not null,
  repository_url   text not null,
  revision         text not null,
  manifest_version text,
  max_retries      integer not null default 3,
  status           text not null default 'active',
  description      text,
  created_at       text not null,
  updated_at       text not null,
  unique (workflow_id, version)
);

create table if not exists jobs (
  id               integer primary key autoincrement,
  sample_id        text not null,
  workflow_pk      integer not null,
  workflow_id      text not null,
  workflow_version text not null,
  status           text not null default 'pending',
  run_name         text,
  retry_count      integer not null default 0,
  created_at       text not null,
  completed_at     text,
  failed_at        text,
  failure_reason   text,
  unique (sample_id, workflow_pk)
);
create index if not exists jobs_dispatch on jobs (status, workflow_id, workflow_version, created_at);
create index if not exists jobs_run on jobs (run_name);
create index if not exists jobs_wf on jobs (workflow_pk, status);

-- Per-(workflow, status) job tallies, maintained by trigger.
--
-- Without this, every job-summary / stats call is a GROUP BY over the whole
-- jobs table: ~100k rows read per call, and a dashboard polling on a timer
-- runs through the 25-billion-row monthly allowance on its own. The counters
-- turn that into a 6-row read.
--
-- Triggers rather than bookkeeping at the call sites: job status is written by
-- half a dozen set-based statements (claim, submit, run, sweep, requeue,
-- reset), and a counter that any one of them can forget to update is a counter
-- that will eventually be wrong. SQLite cannot forget.
create table if not exists job_counts (
  workflow_pk integer not null,
  status      text not null,
  n           integer not null default 0,
  primary key (workflow_pk, status)
);

create trigger if not exists job_counts_insert after insert on jobs
begin
  insert into job_counts (workflow_pk, status, n) values (new.workflow_pk, new.status, 1)
    on conflict (workflow_pk, status) do update set n = n + 1;
end;

create trigger if not exists job_counts_update after update of status on jobs
when old.status <> new.status
begin
  update job_counts set n = n - 1 where workflow_pk = old.workflow_pk and status = old.status;
  insert into job_counts (workflow_pk, status, n) values (new.workflow_pk, new.status, 1)
    on conflict (workflow_pk, status) do update set n = n + 1;
end;

create trigger if not exists job_counts_delete after delete on jobs
begin
  update job_counts set n = n - 1 where workflow_pk = old.workflow_pk and status = old.status;
end;

create table if not exists runs (
  run_name                 text primary key,
  run_id                   text,
  workflow_id              text,
  workflow_version         text,
  workflow_pk              integer,
  revision                 text,
  status                   text not null,
  executor_job_id          text,
  claimed_at               text,
  submitted_at             text,
  started_at               text,
  completed_at             text,
  last_heartbeat_at        text,
  wait_seconds             integer,
  wrapper_exit_code        integer,
  last_known_slurm_state   text,
  slurm_reason             text,
  nextflow_log_uploaded_at text
);
create index if not exists runs_status on runs (status, claimed_at);

create table if not exists dead_letter (
  id               integer primary key autoincrement,
  job_id           integer not null unique,
  run_name         text,
  sample_id        text,
  workflow_id      text,
  workflow_version text,
  reason           text,
  created_at       text,
  resolved_at      text
);

create table if not exists daemons (
  agent_id            text primary key,
  hostname            text,
  workflow_id         text,
  profile             text,
  nf_client_version   text,
  config_yaml         text,
  mode                text,
  batch_size          integer,
  max_concurrent_runs integer,
  active_runs         integer,
  status              text,
  last_seen_at        text,
  started_at          text
);
`;
