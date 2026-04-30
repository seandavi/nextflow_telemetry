"""phase2_samples_workflows_jobs

Revision ID: b3f7a912c041
Revises: 91c2cbd1393e
Create Date: 2026-04-29

Adds:
  - samples catalog table
  - workflows registry table (revision intentionally mutable)
  - jobs table (replaces workflow_executions as primary execution tracker)
  - revision + workflow_pk columns on workflow_runs
  - dead_letter migrated to FK jobs instead of workflow_executions
  - drops workflow_executions

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b3f7a912c041"
down_revision: Union[str, Sequence[str], None] = "91c2cbd1393e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- samples --------------------------------------------------------------
    op.create_table(
        "samples",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sample_id", sa.String, nullable=False, unique=True),
        sa.Column("metadata_", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_samples_sample_id", "samples", ["sample_id"])

    # -- workflows ------------------------------------------------------------
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("version", sa.String, nullable=False),
        sa.Column("repository_url", sa.String, nullable=False),
        sa.Column("revision", sa.String, nullable=False),
        sa.Column("profile", sa.String, nullable=False, server_default="standard"),
        sa.Column("manifest_version", sa.String, nullable=True),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("status", sa.String, nullable=False, server_default="active"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_id_version"),
    )
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index("ix_workflows_workflow_id", "workflows", ["workflow_id"])

    # -- extend workflow_runs -------------------------------------------------
    op.add_column("workflow_runs", sa.Column("workflow_pk", sa.Integer,
                  sa.ForeignKey("workflows.id"), nullable=True))
    op.add_column("workflow_runs", sa.Column("revision", sa.String, nullable=True))

    # -- jobs -----------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sample_id", sa.String,
                  sa.ForeignKey("samples.sample_id"), nullable=False),
        sa.Column("workflow_pk", sa.Integer,
                  sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("workflow_version", sa.String, nullable=False),
        sa.Column("run_name", sa.String,
                  sa.ForeignKey("workflow_runs.run_name"), nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String, nullable=True),
        sa.UniqueConstraint("sample_id", "workflow_id", "workflow_version",
                            name="uq_job_composite"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_sample_id", "jobs", ["sample_id"])
    op.create_index("ix_jobs_composite", "jobs",
                    ["sample_id", "workflow_id", "workflow_version"])

    # -- migrate dead_letter to reference jobs --------------------------------
    # Drop old FK constraint on dead_letter.execution_id and replace with job_id
    op.drop_constraint("dead_letter_execution_id_fkey", "dead_letter",
                       type_="foreignkey")
    op.drop_column("dead_letter", "execution_id")
    op.add_column("dead_letter", sa.Column(
        "job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False,
        server_default="0",   # temporary default for migration; removed after
    ))
    op.alter_column("dead_letter", "job_id", server_default=None)

    # -- drop workflow_executions ---------------------------------------------
    op.drop_table("workflow_executions")


def downgrade() -> None:
    # Recreate workflow_executions
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sample_id", sa.String, nullable=False),
        sa.Column("workflow_id", sa.String, nullable=False),
        sa.Column("workflow_version", sa.String, nullable=False),
        sa.Column("run_name", sa.String,
                  sa.ForeignKey("workflow_runs.run_name"), nullable=True),
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
    # Revert dead_letter FK
    op.drop_column("dead_letter", "job_id")
    op.add_column("dead_letter", sa.Column(
        "execution_id", sa.Integer,
        sa.ForeignKey("workflow_executions.id"), nullable=False,
        server_default="0",
    ))
    op.alter_column("dead_letter", "execution_id", server_default=None)

    op.drop_table("jobs")
    op.drop_column("workflow_runs", "revision")
    op.drop_column("workflow_runs", "workflow_pk")
    op.drop_table("workflows")
    op.drop_table("samples")
