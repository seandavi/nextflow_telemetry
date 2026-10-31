"""Event ingester: cursor caught_up_at and ingest_rejects (ADR-0013, #375).

Revision ID: 6b3e9d21
Revises: 0d5e1a7c
"""
from alembic import op
import sqlalchemy as sa

revision = "6b3e9d21"
down_revision = "0d5e1a7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_cursor", sa.Column("caught_up_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "ingest_rejects",
        sa.Column("stream", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("stream", "object_key", "line_no"),
        sa.CheckConstraint("line_no >= 0", name="ck_ingest_rejects_line_no"),
    )


def downgrade() -> None:
    op.drop_table("ingest_rejects")
    op.drop_column("ingest_cursor", "caught_up_at")
