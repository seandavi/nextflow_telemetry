"""SQLAlchemy table definitions and shared metadata.

Kept separate from main.py so Alembic env.py can import metadata
without pulling in the full FastAPI application.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# ---------------------------------------------------------------------------
# Raw weblog events — append-only, one row per Nextflow event POST
# ---------------------------------------------------------------------------
telemetry_tbl = Table(
    "telemetry",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("run_name", String, nullable=False, index=True),
    Column("event", String, nullable=False),
    Column("utc_time", DateTime(timezone=True)),
    Column("sample_id", String, nullable=True, index=True),
    Column("workflow_id", String, nullable=True, index=True),
    Column("workflow_version", String, nullable=True),
    Column("metadata_", JSONB),
    Column("trace", JSONB),
    Index("ix_telemetry_event", "event"),
    Index("ix_telemetry_utc_time", "utc_time"),
    Index("ix_telemetry_event_utc_time", "event", "utc_time"),
)

# ---------------------------------------------------------------------------
# Sample catalog — one row per known sample
# sample_id is the md5 hex of the sorted, deduplicated SRR accession list,
# or a researcher-supplied opaque ID for non-SRA samples.
# ncbi_accession stores the canonical sorted;deduped;semicolon-separated SRRs.
# biosample_id is the NCBI BioSample accession (annotation only, not identity).
# ---------------------------------------------------------------------------
samples_tbl = Table(
    "samples",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, nullable=False, unique=True),
    Column("ncbi_accession", Text, nullable=True),
    Column("biosample_id", String, nullable=True),
    Column("metadata_", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_samples_sample_id", "sample_id"),
    Index("ix_samples_biosample_id", "biosample_id"),
)

# ---------------------------------------------------------------------------
# Collections — named groups of samples (e.g. a BioProject, a cohort)
# ---------------------------------------------------------------------------
collections_tbl = Table(
    "collections",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("collection_id", String, nullable=False, unique=True),
    Column("source", String, nullable=False),   # "bioproject" | "sra_study" | "manual"
    Column("label", String, nullable=True),
    Column("metadata_", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_collections_collection_id", "collection_id"),
)

collection_samples_tbl = Table(
    "collection_samples",
    metadata,
    Column("collection_id", String, ForeignKey("collections.collection_id"), nullable=False),
    Column("sample_id", String, ForeignKey("samples.sample_id"), nullable=False),
    UniqueConstraint("collection_id", "sample_id", name="uq_collection_sample"),
)

# ---------------------------------------------------------------------------
# Workflow registry — one row per (workflow_id, version) pair
# revision is intentionally mutable: the composite job key is
# (workflow_id, version, sample_id), so changing revision does not force
# reruns. workflow_runs captures the revision actually used.
# ---------------------------------------------------------------------------
workflows_tbl = Table(
    "workflows",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("workflow_id", String, nullable=False),
    Column("version", String, nullable=False),
    Column("repository_url", String, nullable=False),
    Column("revision", String, nullable=False),      # git hash / tag / branch — mutable
    Column("manifest_version", String, nullable=True),
    Column("max_retries", Integer, nullable=False, default=3),
    Column("status", String, nullable=False, default="active"),  # active|paused|retired
    Column("description", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("workflow_id", "version", name="uq_workflow_id_version"),
    Index("ix_workflows_status", "status"),
    Index("ix_workflows_workflow_id", "workflow_id"),
)

# ---------------------------------------------------------------------------
# Jobs — one row per (sample_id, workflow_id, workflow_version)
# This is the cross-product of samples × active workflows; reconcile_jobs()
# fills the gaps. A job tracks the *current* status; workflow_runs holds
# the per-execution history.
# ---------------------------------------------------------------------------
jobs_tbl = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, ForeignKey("samples.sample_id"), nullable=False),
    Column("workflow_pk", Integer, ForeignKey("workflows.id"), nullable=False),
    Column("workflow_id", String, nullable=False),       # denormalised for query convenience
    Column("workflow_version", String, nullable=False),  # denormalised
    Column("run_name", String, ForeignKey("workflow_runs.run_name"), nullable=True),
    Column("status", String, nullable=False, default="pending"),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("failed_at", DateTime(timezone=True), nullable=True),
    Column("failure_reason", String, nullable=True),
    UniqueConstraint("sample_id", "workflow_id", "workflow_version", name="uq_job_composite"),
    Index("ix_jobs_status", "status"),
    Index("ix_jobs_sample_id", "sample_id"),
    Index("ix_jobs_composite", "sample_id", "workflow_id", "workflow_version"),
)

# ---------------------------------------------------------------------------
# One row per Nextflow run (identified by run_name, which the client controls)
# ---------------------------------------------------------------------------
workflow_runs_tbl = Table(
    "workflow_runs",
    metadata,
    Column("run_name", String, primary_key=True),
    Column("run_id", String, nullable=True),           # set on 'started' weblog event
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("workflow_pk", Integer, ForeignKey("workflows.id"), nullable=True),
    Column("revision", String, nullable=True),         # revision actually used for this run
    Column("status", String, nullable=False, default="claimed"),
    Column("executor_job_id", String, nullable=True),  # SLURM job id, local PID, etc.
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    # Run-lifecycle observability (issue #63 / meta #62)
    Column("last_heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("last_known_slurm_state", Text, nullable=True),
    Column("slurm_reason", Text, nullable=True),
    Column("wrapper_exit_code", Integer, nullable=True),
    Column("wait_seconds", Integer, nullable=True),
    Column("nextflow_log_uploaded_at", DateTime(timezone=True), nullable=True),
    Index("ix_workflow_runs_status", "status"),
    Index("ix_workflow_runs_claimed_at", "claimed_at"),
)

# ---------------------------------------------------------------------------
# Task logs — .command.sh and .command.err uploaded by nf-client post-run
# ---------------------------------------------------------------------------
task_logs_tbl = Table(
    "task_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_name", String, nullable=False),
    Column("task_hash", String, nullable=False),   # e.g. "ab/1234ef5678..."
    Column("log_type", String, nullable=False),    # "command_sh" or "command_err"
    Column("content", Text, nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("run_name", "task_hash", "log_type", name="uq_task_log"),
    Index("ix_task_logs_run_hash", "run_name", "task_hash"),
)

# ---------------------------------------------------------------------------
# Dead letter queue — populated when a run completes without MARK_COMPLETE
# ---------------------------------------------------------------------------
dead_letter_tbl = Table(
    "dead_letter",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("run_name", String, nullable=False),
    Column("sample_id", String, nullable=False),
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("reason", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("job_id", name="uq_dlq_job_id"),
    Index("ix_dlq_resolved", "resolved_at"),
)

# ---------------------------------------------------------------------------
# Curated studies — one row per imported curatedMetagenomicData study
# ---------------------------------------------------------------------------
curated_studies_tbl = Table(
    "curated_studies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("study_name", Text, nullable=False, unique=True),
    Column("source_file", Text, nullable=True),
    Column("metadata_", JSONB, nullable=True),   # pubmed_id, doi, etc.
    Column("loaded_at", DateTime(timezone=True), nullable=False),
)

# ---------------------------------------------------------------------------
# Curated sample annotations — one row per (sample_id, study_name) pair
# ---------------------------------------------------------------------------
curated_sample_annotations_tbl = Table(
    "curated_sample_annotations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", Text, nullable=False),       # md5 join key
    Column("study_name", Text, ForeignKey("curated_studies.study_name"), nullable=False),
    Column("ncbi_accession", Text, nullable=True),   # raw SRR string from TSV
    Column("metadata_", JSONB, nullable=False),
    Column("loaded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("sample_id", "study_name", name="uq_csa_sample_study"),
    Index("ix_csa_sample_id", "sample_id"),
    Index("ix_csa_study_name", "study_name"),
)

# ---------------------------------------------------------------------------
# Daemon agent registry — upserted by nf-client on every heartbeat
# ---------------------------------------------------------------------------
daemon_agents_tbl = Table(
    "daemon_agents",
    metadata,
    Column("agent_id", String, primary_key=True),   # "{hostname}:{workflow_id}"
    Column("hostname", String, nullable=False),
    Column("workflow_id", String, nullable=True),
    Column("profile", String, nullable=True),
    Column("nf_client_version", String, nullable=True),
    Column("config_yaml", Text, nullable=True),      # sanitized — no credential paths
    Column("mode", String, nullable=False),          # local|slurm|pbs|lsf
    Column("batch_size", Integer, nullable=False),
    Column("max_concurrent_runs", Integer, nullable=True),
    Column("active_runs", Integer, nullable=False, default=0),
    Column("status", String, nullable=False, default="idle"),  # idle|running
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
)
