"""submissions_table

Revision ID: b6c7d8e9
Revises: a5b6c7d8
Create Date: 2026-07-03

Adds the submissions table — the append-only event log of "register these
samples" actions (accession or, later, curation TSV). Provenance/audit source
of truth; collections + samples remain the current-state projections. A no-op
re-submission is still a row (samples_added = 0). Receipt-only for now; a
submission_samples join (per-attempt sample sets / rollback) is deferred.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("submission_id", sa.String(), primary_key=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("accession", sa.String(), nullable=True),
        sa.Column("collection_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("samples_found", sa.Integer(), nullable=True),
        sa.Column("samples_added", sa.Integer(), nullable=True),
        sa.Column("samples_existing", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata_", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_submissions_collection_id", "submissions", ["collection_id"])
    op.create_index("ix_submissions_created_at", "submissions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_submissions_created_at", table_name="submissions")
    op.drop_index("ix_submissions_collection_id", table_name="submissions")
    op.drop_table("submissions")
