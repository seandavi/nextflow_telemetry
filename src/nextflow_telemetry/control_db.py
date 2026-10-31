"""Control-plane tables (ADR-0013, #371). Defined on db.metadata; the v1 tables
they replace are the v1_* tables in db.py until M10."""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from .db import metadata


def _created_at() -> Column:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated_at() -> Column:
    return Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))


_NO_DEADLINE = "num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) = 0"

studies_tbl = Table(
    "studies", metadata,
    Column("study_id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    _created_at(),
    _updated_at(),
    UniqueConstraint("name", name="uq_studies_name"),
)

study_aliases_tbl = Table(
    "study_aliases", metadata,
    Column("study_id", Integer, ForeignKey("studies.study_id", ondelete="CASCADE"), nullable=False),
    Column("scheme", String, nullable=False),
    Column("value", String, nullable=False),
    # Not unique on (scheme, value): one DOI or BioProject can back several studies.
    PrimaryKeyConstraint("scheme", "value", "study_id"),
    CheckConstraint(
        "scheme in ('cmd_study','bioproject','insdc_study','asap_code','tracker_name','doi','pmid')",
        name="ck_study_aliases_scheme",
    ),
    Index("ix_study_aliases_study_id", "study_id"),
)

samples_tbl = Table(
    "samples", metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, nullable=False),
    Column("readset_id", String, nullable=True),
    Column("ncbi_accession", Text, nullable=True),
    Column("biosample_id", String, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _created_at(),
    _updated_at(),
    UniqueConstraint("sample_id", name="uq_samples_sample_id"),
    Index("ix_samples_readset_id", "readset_id"),
    Index("ix_samples_biosample_id", "biosample_id"),
)

readset_runs_tbl = Table(
    "readset_runs", metadata,
    Column("run_accession", String, primary_key=True),
    Column("readset_id", String, nullable=False),
    Column("biosample", String, nullable=True),
    Column("sra_sample", String, nullable=True),
    Column("experiment", String, nullable=True),
    Column("bioproject", String, nullable=True),
    _created_at(),
    _updated_at(),
    CheckConstraint("run_accession ~ '^[SED]RR[0-9]+$'", name="ck_readset_runs_run_accession"),
    Index("ix_readset_runs_readset_id", "readset_id"),
)

collections_tbl = Table(
    "collections", metadata,
    Column("collection_id", String, primary_key=True),
    Column("source", String, nullable=False, server_default="manual"),
    Column("label", String, nullable=True),
    Column("study_id", Integer, ForeignKey("studies.study_id"), nullable=True),
    _created_at(),
    _updated_at(),
    Index("ix_collections_study_id", "study_id"),
)

collection_samples_tbl = Table(
    "collection_samples", metadata,
    Column("collection_id", String, ForeignKey("collections.collection_id"), primary_key=True),
    Column("sample_id", String, ForeignKey("samples.sample_id"), primary_key=True),
    _created_at(),
    Index("ix_collection_samples_sample_id", "sample_id"),
)

workflows_tbl = Table(
    "workflows", metadata,
    Column("id", Integer, primary_key=True),
    Column("workflow_id", String, nullable=False),
    Column("version", String, nullable=False),
    Column("repository_url", String, nullable=False),
    Column("revision", String, nullable=False),
    Column("manifest_version", String, nullable=True),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("status", String, nullable=False, server_default="active"),
    Column("description", Text, nullable=True),
    Column("params", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("collections", ARRAY(String), nullable=True),
    Column("sample_key", String, nullable=False, server_default="readset_id"),
    _created_at(),
    _updated_at(),
    UniqueConstraint("workflow_id", "version", name="uq_workflows_workflow_id_version"),
    CheckConstraint("status in ('active','paused','retired')", name="ck_workflows_status"),
    CheckConstraint("sample_key in ('sample_id','readset_id')", name="ck_workflows_sample_key"),
    CheckConstraint("collections is null or cardinality(collections) > 0", name="ck_workflows_collections"),
    CheckConstraint("max_retries >= 0", name="ck_workflows_max_retries"),
)

runs_tbl = Table(
    "runs", metadata,
    Column("run_name", String, primary_key=True),
    Column("run_id", String, nullable=True),
    Column("workflow_id", String, nullable=True),
    Column("workflow_version", String, nullable=True),
    Column("workflow_pk", Integer, ForeignKey("workflows.id"), nullable=True),
    Column("revision", String, nullable=True),
    Column("status", String, nullable=False),
    Column("executor_job_id", String, nullable=True),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("last_heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("wait_seconds", Integer, nullable=True),
    Column("wrapper_exit_code", Integer, nullable=True),
    Column("last_known_slurm_state", String, nullable=True),
    Column("slurm_reason", Text, nullable=True),
    Column("nextflow_log_uploaded_at", DateTime(timezone=True), nullable=True),
    Column("claim_expires_at", DateTime(timezone=True), nullable=True),
    Column("submit_deadline", DateTime(timezone=True), nullable=True),
    Column("liveness_deadline", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status in ('claimed','submitted','running','completed','failed','expired')",
        name="ck_runs_status",
    ),
    CheckConstraint(
        "num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) <= 1",
        name="ck_runs_one_deadline",
    ),
    CheckConstraint(
        f"status not in ('completed','failed','expired') or (completed_at is not null and {_NO_DEADLINE})",
        name="ck_runs_terminal_closed",
    ),
    Index("ix_runs_status_claimed_at", "status", "claimed_at"),
    Index("ix_runs_claim_expires_at", "claim_expires_at", postgresql_where=text("claim_expires_at IS NOT NULL")),
    Index("ix_runs_submit_deadline", "submit_deadline", postgresql_where=text("submit_deadline IS NOT NULL")),
    Index("ix_runs_liveness_deadline", "liveness_deadline", postgresql_where=text("liveness_deadline IS NOT NULL")),
)

jobs_tbl = Table(
    "jobs", metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, ForeignKey("samples.sample_id"), nullable=False),
    Column("workflow_pk", Integer, ForeignKey("workflows.id"), nullable=False),
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("run_name", String, ForeignKey("runs.run_name"), nullable=True),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("priority", Integer, nullable=False, server_default="0"),
    _created_at(),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("failed_at", DateTime(timezone=True), nullable=True),
    Column("failure_reason", Text, nullable=True),
    UniqueConstraint("sample_id", "workflow_pk", name="uq_jobs_sample_id_workflow_pk"),
    CheckConstraint(
        "status in ('pending','claimed','submitted','running','completed','failed')",
        name="ck_jobs_status",
    ),
    CheckConstraint("status <> 'completed' or completed_at is not null", name="ck_jobs_completed_at"),
    CheckConstraint("status <> 'failed' or failed_at is not null", name="ck_jobs_failed_at"),
    CheckConstraint("(status = 'pending') = (run_name is null)", name="ck_jobs_run_name"),
    CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count"),
    Index("ix_jobs_workflow_pk_status", "workflow_pk", "status"),
    Index("ix_jobs_run_name", "run_name"),
)
Index(
    "ix_jobs_pending_claim",
    jobs_tbl.c.workflow_pk,
    jobs_tbl.c.priority.desc(),
    jobs_tbl.c.created_at,
    jobs_tbl.c.id,
    postgresql_where=jobs_tbl.c.status == "pending",
)

dead_letter_tbl = Table(
    "dead_letter", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("run_name", String, ForeignKey("runs.run_name"), nullable=True),
    Column("sample_id", String, nullable=False),
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("reason", Text, nullable=False),
    _created_at(),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("job_id", name="uq_dead_letter_job_id"),
)

daemons_tbl = Table(
    "daemons", metadata,
    Column("agent_id", String, primary_key=True),
    Column("hostname", String, nullable=False),
    Column("workflow_id", String, nullable=True),
    Column("profile", String, nullable=True),
    Column("nf_client_version", String, nullable=True),
    Column("config_yaml", Text, nullable=True),
    Column("mode", String, nullable=True),
    Column("batch_size", Integer, nullable=True),
    Column("max_concurrent_runs", Integer, nullable=True),
    Column("active_runs", Integer, nullable=False, server_default="0"),
    Column("status", String, nullable=False, server_default="idle"),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    # "stale" is computed when read and never stored.
    CheckConstraint("status in ('idle','running','stopped')", name="ck_daemons_status"),
    Index("ix_daemons_last_seen_at", "last_seen_at"),
)

run_task_counts_tbl = Table(
    "run_task_counts", metadata,
    Column("run_name", String, ForeignKey("runs.run_name"), primary_key=True),
    Column("status", String, primary_key=True),
    Column("n", Integer, nullable=False),
    CheckConstraint("n >= 0", name="ck_run_task_counts_n"),
)

run_failed_tasks_tbl = Table(
    "run_failed_tasks", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_name", String, ForeignKey("runs.run_name"), nullable=False),
    Column("process", String, nullable=False),
    Column("sample_id", String, nullable=True),
    Column("exit_code", String, nullable=True),
    Column("task_hash", String, nullable=True),
    Column("attempt", Integer, nullable=True),
    Column("error_action", String, nullable=True),
    Column("utc_time", DateTime(timezone=True), nullable=False),
    Index("ix_run_failed_tasks_run_name", "run_name"),
)

ingest_cursor_tbl = Table(
    "ingest_cursor", metadata,
    Column("stream", String, primary_key=True),
    Column("object_key", String, nullable=False),
    # Not "offset": reserved word.
    Column("line_offset", BigInteger, nullable=False, server_default="0"),
    _updated_at(),
    CheckConstraint("line_offset >= 0", name="ck_ingest_cursor_line_offset"),
)

# Append-only; the latest `at` per subject wins. No FK on subject_id (polymorphic).
curation_decisions_tbl = Table(
    "curation_decisions", metadata,
    Column("id", Integer, primary_key=True),
    Column("subject_type", String, nullable=False),
    Column("subject_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor", String, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("subject_type in ('study','readset','run')", name="ck_curation_decisions_subject_type"),
    CheckConstraint("decision in ('include','exclude')", name="ck_curation_decisions_decision"),
    Index("ix_curation_decisions_subject", "subject_type", "subject_id", "at"),
)

# One row (id = 1): when the last deadline sweep committed. The outage grace
# period measures the gap from it (#374).
deadline_sweep_tbl = Table(
    "deadline_sweep", metadata,
    Column("id", SmallInteger, primary_key=True),
    Column("last_swept_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("id = 1", name="ck_deadline_sweep_singleton"),
)
