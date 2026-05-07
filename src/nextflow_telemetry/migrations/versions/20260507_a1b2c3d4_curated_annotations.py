"""curated_annotations

Revision ID: a1b2c3d4
Revises: f3a1b2c4d5e6
Create Date: 2026-05-07

Adds:
  - curated_studies table — one row per imported study (e.g. a curatedMetagenomicData TSV)
  - curated_sample_annotations table — one row per (sample_id, study_name) pair
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "f3a1b2c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "curated_studies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("study_name", sa.Text, nullable=False, unique=True),
        sa.Column("source_file", sa.Text, nullable=True),
        sa.Column("metadata_", JSONB, nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "curated_sample_annotations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sample_id", sa.Text, nullable=False),
        sa.Column(
            "study_name",
            sa.Text,
            sa.ForeignKey("curated_studies.study_name"),
            nullable=False,
        ),
        sa.Column("ncbi_accession", sa.Text, nullable=True),
        sa.Column("metadata_", JSONB, nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sample_id", "study_name", name="uq_csa_sample_study"),
    )
    op.create_index("ix_csa_sample_id", "curated_sample_annotations", ["sample_id"])
    op.create_index("ix_csa_study_name", "curated_sample_annotations", ["study_name"])


def downgrade() -> None:
    op.drop_index("ix_csa_study_name", table_name="curated_sample_annotations")
    op.drop_index("ix_csa_sample_id", table_name="curated_sample_annotations")
    op.drop_table("curated_sample_annotations")
    op.drop_table("curated_studies")
