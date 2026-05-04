"""task_logs

Revision ID: e5f2a9b1c8d3
Revises: d4e1f8c2b7a5
Create Date: 2026-05-04

Adds the task_logs table for storing .command.sh and .command.err content
uploaded by nf-client after each Nextflow task completes.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f2a9b1c8d3"
down_revision: Union[str, None] = "d4e1f8c2b7a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_name", sa.String(), nullable=False),
        sa.Column("task_hash", sa.String(), nullable=False),
        sa.Column("log_type", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_name", "task_hash", "log_type", name="uq_task_log"),
    )
    op.create_index("ix_task_logs_run_hash", "task_logs", ["run_name", "task_hash"])


def downgrade() -> None:
    op.drop_index("ix_task_logs_run_hash", table_name="task_logs")
    op.drop_table("task_logs")
