"""users_table

Revision ID: d2e3f4a5
Revises: c1d2e3f4
Create Date: 2026-05-13

Adds users table for Google-OAuth identity + role (#95 / meta #94).

The initial admin (seandavi@gmail.com) is seeded here so the very first
login after deploy has a working role. Additional users are added via
admin-only API once available; until then, manual INSERT.
"""
from __future__ import annotations

import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("email", sa.String, primary_key=True),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'contributor')", name="ck_users_role"),
    )
    op.execute(
        sa.text(
            "INSERT INTO users (email, role, created_at) "
            "VALUES (:email, :role, :now) ON CONFLICT (email) DO NOTHING"
        ).bindparams(
            email="seandavi@gmail.com",
            role="admin",
            now=datetime.datetime.now(datetime.timezone.utc),
        )
    )


def downgrade() -> None:
    op.drop_table("users")
