/**
 * The v2 wire contract, as Zod. One place for request-body shapes and the
 * response shapes a client actually binds to (dashboard, nf-client, the
 * TypeScript types #173 will generate). Response objects are `looseObject`:
 * the fields named here are the contract, extra columns are not a break.
 *
 * Field sets mirror v1's Pydantic models in src/nextflow_telemetry/models.py;
 * where v2 differs on purpose it is recorded in cf/README.md (#174).
 */
import { z } from "zod";

const iso = z.string().describe("ISO-8601 UTC timestamp");
const isoNull = iso.nullable();

// ---------------------------------------------------------------- requests

export const ClaimRequest = z.object({
  limit: z.number().int().min(1).max(500).default(50),
  workflow_id: z.union([z.string(), z.array(z.string())]).nullish().describe("Restrict the claim to these workflow ids"),
  workflow_version: z.string().nullish(),
  agent_id: z.string().optional(),
  hostname: z.string().optional(),
});

export const SubmittedRequest = z.object({
  run_name: z.string(),
  executor_job_id: z.string().nullish().describe("SLURM job id, or anything the executor returned"),
});

export const RegisterSampleRequest = z.object({
  sample_id: z.string().describe("md5 of the sorted, deduplicated SRR set"),
  ncbi_accession: z.string().describe("SRR accessions, separator-tolerant"),
  biosample_id: z.string().nullish(),
  metadata: z.record(z.string(), z.unknown()).nullish(),
  collection: z.string().nullish().describe("Collection to attach to; membership rows are the truth (ADR-0005)"),
});

export const RegisterWorkflowRequest = z.object({
  workflow_id: z.string(),
  version: z.string(),
  repository_url: z.string(),
  revision: z.string(),
  manifest_version: z.string().nullish(),
  max_retries: z.number().int().min(0).optional(),
  status: z.enum(["active", "paused", "retired"]).optional(),
  description: z.string().nullish(),
});

export const WorkflowStatusRequest = z.object({ status: z.enum(["active", "paused", "retired"]) });
export const WorkflowRevisionRequest = z.object({ revision: z.string() });

export const RunEventType = z.enum(["wrapper_started", "pre_nextflow", "heartbeat", "wrapper_exited", "slurm_state"]);
export const RunEvent = z.looseObject({
  type: RunEventType,
  utc_time: iso.optional(),
  wait_seconds: z.number().optional(),
  exit_code: z.number().int().optional(),
  slurm_state: z.string().optional(),
  slurm_reason: z.string().optional(),
});

export const DaemonHeartbeatRequest = z.looseObject({
  agent_id: z.string(),
  hostname: z.string().optional(),
  workflow_id: z.string().nullish(),
  profile: z.string().nullish(),
  nf_client_version: z.string().nullish(),
  config_yaml: z.string().nullish().describe("Sanitised: never contains the token"),
  mode: z.string().nullish(),
  batch_size: z.number().int().nullish(),
  max_concurrent_runs: z.number().int().nullish(),
  active_runs: z.number().int().nullish(),
  status: z.string().nullish(),
});

export const CloseRunRequest = z.object({ run_name: z.string() });

// --------------------------------------------------------------- responses

export const Detail = z.object({ detail: z.string() });

export const Sample = z.looseObject({
  id: z.number().int(),
  sample_id: z.string(),
  ncbi_accession: z.string().nullable(),
  biosample_id: z.string().nullable(),
  metadata: z.record(z.string(), z.unknown()),
  collections: z.array(z.string()),
  created_at: iso,
  updated_at: iso,
});

export const SampleList = z.object({
  items: z.array(Sample),
  total: z.number().int(),
  limit: z.number().int(),
  offset: z.number().int(),
});

export const CollectionFacets = z.object({
  total: z.number().int(),
  collections: z.array(z.object({ collection: z.string(), count: z.number().int() })),
});

export const Workflow = z.looseObject({
  id: z.number().int().describe("workflow_pk on the wire"),
  workflow_id: z.string(),
  version: z.string(),
  repository_url: z.string(),
  revision: z.string(),
  manifest_version: z.string().nullable(),
  max_retries: z.number().int(),
  status: z.enum(["active", "paused", "retired"]),
  description: z.string().nullable(),
  created_at: iso,
  updated_at: iso,
});

export const JobStatus = z.enum(["pending", "claimed", "submitted", "running", "completed", "failed"]);
export const RunStatus = z.enum(["claimed", "submitted", "running", "completed", "expired", "failed"]);

export const JobSummary = z.object({
  workflow_pk: z.number().int(),
  workflow_id: z.string(),
  version: z.string(),
  total: z.number().int(),
  pending: z.number().int(),
  claimed: z.number().int(),
  submitted: z.number().int(),
  running: z.number().int(),
  completed: z.number().int(),
  failed: z.number().int(),
  dead_letter: z.number().int(),
  completion_pct: z.number(),
});

export const ClaimedBatch = z.looseObject({
  run_name: z.string().describe("UUIDv7, minted at claim; also the Nextflow -name"),
  workflow_pk: z.number().int(),
  workflow_id: z.string(),
  workflow_version: z.string(),
  repository_url: z.string(),
  revision: z.string(),
  jobs: z.array(
    z.looseObject({
      id: z.number().int(),
      sample_id: z.string(),
      ncbi_accession: z.string().nullable(),
      metadata: z.record(z.string(), z.unknown()).nullable(),
    }),
  ),
});

export const Run = z.looseObject({
  run_name: z.string(),
  run_id: z.string().nullable().describe("Nextflow session id, from the weblog started event"),
  workflow_id: z.string().nullable(),
  workflow_version: z.string().nullable(),
  workflow_pk: z.number().int().nullable(),
  revision: z.string().nullable(),
  status: RunStatus,
  executor_job_id: z.string().nullable(),
  claimed_at: isoNull,
  submitted_at: isoNull,
  started_at: isoNull,
  completed_at: isoNull,
  last_heartbeat_at: isoNull,
  wait_seconds: z.number().nullable(),
  wrapper_exit_code: z.number().int().nullable(),
  last_known_slurm_state: z.string().nullable(),
  slurm_reason: z.string().nullable(),
  nextflow_log_uploaded_at: isoNull,
  classification: z.string().describe("ADR-0002: active | stalled | wrapper-failed | ended-no-log | completed | …"),
});

export const RunList = z.object({
  total: z.number().int(),
  runs: z.array(Run),
  limit: z.number().int(),
  offset: z.number().int(),
});

export const RunDetail = Run.extend({
  job_status_counts: z.record(z.string(), z.number().int()),
  nextflow_log_available: z.boolean(),
  wrapper_output_log_available: z.boolean(),
});

export const RunEventAccepted = z.object({
  run_name: z.string(),
  type: RunEventType,
  nextflow_log_uploaded: z.boolean(),
  wrapper_output_log_uploaded: z.boolean(),
});

export const Cohort = z.looseObject({
  collection_id: z.string(),
  source: z.string(),
  label: z.string().nullable(),
  sample_count: z.number().int(),
});

export const CohortSummary = z.looseObject({
  collection_id: z.string(),
  sample_count: z.number().int(),
  samples_completed: z.number().int(),
  total_jobs: z.number().int(),
  job_status_counts: z.record(z.string(), z.number().int()),
  completion_pct: z.number(),
  generated_at_utc: iso,
});

export const Daemon = z.looseObject({
  agent_id: z.string(),
  hostname: z.string().nullable(),
  workflow_id: z.string().nullable(),
  last_seen_at: isoNull,
  is_active: z.boolean(),
});

export const Stats = z.object({
  samples: z.number().int(),
  workflows: z.number().int(),
  jobs_by_status: z.record(z.string(), z.number().int()).describe("From job_counts (trigger-maintained)"),
  jobs_by_status_active: z.record(z.string(), z.number().int()),
  runs_by_status: z.record(z.string(), z.number().int()),
  dead_letter_unresolved: z.number().int(),
  storage: z.object({ bytes: z.number(), pct_of_limit: z.number() }),
});

export const RunningProcesses = z.looseObject({
  generated_at_utc: iso,
  active_nf_runs: z.number().int(),
  total_running: z.number().int(),
  total_queued: z.number().int(),
  by_process: z.array(z.looseObject({ process: z.string() })),
});

export const TaskLogs = z.object({
  run_name: z.string(),
  task_hash: z.string(),
  logs: z.array(
    z.object({
      id: z.number().int(),
      run_name: z.string(),
      task_hash: z.string(),
      log_type: z.string(),
      content: z.string(),
      uploaded_at: iso,
    }),
  ),
});
