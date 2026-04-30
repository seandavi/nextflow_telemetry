"""initial_schema

Revision ID: 91c2cbd1393e
Revises:
Create Date: 2026-04-29

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "91c2cbd1393e"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Raw weblog events — append-only
    op.create_table(
        "telemetry",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("run_name", sa.String, nullable=False),
        sa.Column("event", sa.String, nullable=False),
        sa.Column("utc_time", sa.DateTime(timezone=True)),
        sa.Column("sample_id", sa.String, nullable=True),
        sa.Column("workflow_id", sa.String, nullable=True),
        sa.Column("workflow_version", sa.String, nullable=True),
        sa.Column("metadata_", JSONB),
        sa.Column("trace", JSONB),
    )
    op.create_index("ix_telemetry_run_id", "telemetry", ["run_id"])
    op.create_index("ix_telemetry_run_name", "telemetry", ["run_name"])
    op.create_index("ix_telemetry_sample_id", "telemetry", ["sample_id"])
    op.create_index("ix_telemetry_workflow_id", "telemetry", ["workflow_id"])

    # One row per Nextflow run (run_name is client-controlled sortable UUID7)
    op.create_table(
        "workflow_runs",
        sa.Column("run_name", sa.String, primary_key=True),
        sa.Column("run_id", sa.String, nullable=True),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("workflow_version", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="claimed"),
        sa.Column("executor_job_id", sa.String, nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_claimed_at", "workflow_runs", ["claimed_at"])

    # One row per (sample, workflow, version, run) — retry history preserved
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sample_id", sa.String, nullable=False),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("workflow_version", sa.String, nullable=False),
        sa.Column("run_name", sa.String, sa.ForeignKey("workflow_runs.run_name"), nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String, nullable=True),
        sa.UniqueConstraint(
            "sample_id", "workflow_id", "workflow_version", "run_name",
            name="uq_execution_composite",
        ),
    )
    op.create_index("ix_executions_sample", "workflow_executions", ["sample_id"])
    op.create_index("ix_executions_status", "workflow_executions", ["status"])
    op.create_index(
        "ix_executions_composite",
        "workflow_executions",
        ["sample_id", "workflow_id", "workflow_version"],
    )

    # Dead letter queue
    op.create_table(
        "dead_letter",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "execution_id",
            sa.Integer,
            sa.ForeignKey("workflow_executions.id"),
            nullable=False,
        ),
        sa.Column("run_name", sa.String, nullable=False),
        sa.Column("sample_id", sa.String, nullable=False),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("workflow_version", sa.String, nullable=False),
        sa.Column("reason", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dlq_resolved", "dead_letter", ["resolved_at"])


def downgrade() -> None:
    op.drop_table("dead_letter")
    op.drop_table("workflow_executions")
    op.drop_table("workflow_runs")
    op.drop_table("telemetry")
