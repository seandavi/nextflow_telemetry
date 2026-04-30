"""SQLAlchemy table definitions and shared metadata.

Kept separate from main.py so Alembic env.py can import metadata
without pulling in the full FastAPI application.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
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
    Column("status", String, nullable=False, default="claimed"),
    Column("executor_job_id", String, nullable=True),  # SLURM job id, local PID, etc.
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Index("ix_workflow_runs_status", "status"),
    Index("ix_workflow_runs_claimed_at", "claimed_at"),
)

# ---------------------------------------------------------------------------
# One row per (sample, workflow, version) logical execution
# Multiple rows possible across different runs (retry history)
# ---------------------------------------------------------------------------
workflow_executions_tbl = Table(
    "workflow_executions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, nullable=False),
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("run_name", String, ForeignKey("workflow_runs.run_name"), nullable=True),
    Column(
        "status",
        String,
        nullable=False,
        default="pending",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("failed_at", DateTime(timezone=True), nullable=True),
    Column("failure_reason", String, nullable=True),
    UniqueConstraint("sample_id", "workflow_id", "workflow_version", "run_name",
                     name="uq_execution_composite"),
    Index("ix_executions_sample", "sample_id"),
    Index("ix_executions_status", "status"),
    Index("ix_executions_composite", "sample_id", "workflow_id", "workflow_version"),
)

# ---------------------------------------------------------------------------
# Dead letter queue — populated when a run completes without MARK_COMPLETE
# ---------------------------------------------------------------------------
dead_letter_tbl = Table(
    "dead_letter",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("execution_id", Integer, ForeignKey("workflow_executions.id"), nullable=False),
    Column("run_name", String, nullable=False),
    Column("sample_id", String, nullable=False),
    Column("workflow_id", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("reason", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Index("ix_dlq_resolved", "resolved_at"),
)
