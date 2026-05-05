"""daemon_agents

Revision ID: 4bc9877b
Revises: e5f2a9b1c8d3
Create Date: 2026-05-05

Adds the daemon_agents table for nf-client heartbeat registration.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4bc9877b"
down_revision: Union[str, None] = "e5f2a9b1c8d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daemon_agents",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("profile", sa.String(), nullable=True),
        sa.Column("nf_client_version", sa.String(), nullable=True),
        sa.Column("config_yaml", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=True),
        sa.Column("active_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="idle"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )


def downgrade() -> None:
    op.drop_table("daemon_agents")
