"""Deadline sweep record (ADR-0013 decision 4, #374).

Revision ID: 0d5e1a7c
Revises: fa0b1c2d
"""
from alembic import op
import sqlalchemy as sa

revision = "0d5e1a7c"
down_revision = "fa0b1c2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deadline_sweep",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("last_swept_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_deadline_sweep_singleton"),
    )


def downgrade() -> None:
    op.drop_table("deadline_sweep")
