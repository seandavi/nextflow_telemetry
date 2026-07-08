"""etl_ingested watermark

Revision ID: c7d8e9fa
Revises: b6c7d8e9
Create Date: 2026-07-07

Adds the output-catalog ETL watermark: one row per (sample, workflow, version)
whose published outputs have been ingested into the DuckLake. The ETL reads
"pending" as completed jobs anti-joined against this table, so it's restart- and
re-run-safe. See src/nextflow_telemetry/etl/.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "c7d8e9fa"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etl_ingested",
        sa.Column("sample_id", sa.String(), primary_key=True),
        sa.Column("workflow_id", sa.String(), primary_key=True),
        sa.Column("workflow_version", sa.String(), primary_key=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("row_counts", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("etl_ingested")
