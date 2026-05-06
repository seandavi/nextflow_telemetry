"""content_addressed_samples

Revision ID: f3a1b2c4d5e6
Revises: 4bc9877b
Create Date: 2026-05-06

Adds:
  - samples.ncbi_accession  — sorted;deduped;semicolon-separated SRR list
  - samples.biosample_id    — NCBI BioSample accession (nullable, indexed)
  - Migrates existing ncbi_accession from metadata_ JSONB to the new column
  - Recomputes sample_id as md5(canonical SRRs) for rows with an accession
  - Updates denormalized sample_id in jobs, telemetry, dead_letter accordingly
  - collections and collection_samples tables
"""
from __future__ import annotations

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "f3a1b2c4d5e6"
down_revision: Union[str, Sequence[str], None] = "4bc9877b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _srrs_to_sample_id(ncbi_accession: str) -> str:
    srrs = sorted(set(s.strip() for s in ncbi_accession.split(";") if s.strip()))
    canonical = ";".join(srrs)
    return hashlib.md5(canonical.encode()).hexdigest()


def upgrade() -> None:
    # -- new columns on samples -----------------------------------------------
    op.add_column("samples", sa.Column("ncbi_accession", sa.Text, nullable=True))
    op.add_column("samples", sa.Column("biosample_id", sa.String, nullable=True))
    op.create_index("ix_samples_biosample_id", "samples", ["biosample_id"])

    conn = op.get_bind()

    # Populate ncbi_accession from the metadata_ JSONB blob
    conn.execute(sa.text("""
        UPDATE samples
        SET ncbi_accession = metadata_->>'ncbi_accession'
        WHERE metadata_->>'ncbi_accession' IS NOT NULL
          AND metadata_->>'ncbi_accession' != ''
    """))

    # Recompute sample_id for rows that now have ncbi_accession.
    # Must update all FK/denormalized references atomically before applying.
    rows = conn.execute(sa.text(
        "SELECT sample_id, ncbi_accession FROM samples WHERE ncbi_accession IS NOT NULL"
    )).fetchall()

    id_map: dict[str, str] = {}
    for old_id, accession in rows:
        new_id = _srrs_to_sample_id(accession)
        if new_id != old_id:
            id_map[old_id] = new_id

    if id_map:
        # Drop FK so we can update the referenced column freely
        op.drop_constraint("jobs_sample_id_fkey", "jobs", type_="foreignkey")

        for old_id, new_id in id_map.items():
            # Update the canonical column first
            conn.execute(sa.text(
                "UPDATE samples SET sample_id = :new WHERE sample_id = :old"
            ), {"new": new_id, "old": old_id})
            # Propagate to FK reference
            conn.execute(sa.text(
                "UPDATE jobs SET sample_id = :new WHERE sample_id = :old"
            ), {"new": new_id, "old": old_id})
            # Propagate to denormalized columns (no FK constraint)
            conn.execute(sa.text(
                "UPDATE telemetry SET sample_id = :new WHERE sample_id = :old"
            ), {"new": new_id, "old": old_id})
            conn.execute(sa.text(
                "UPDATE dead_letter SET sample_id = :new WHERE sample_id = :old"
            ), {"new": new_id, "old": old_id})

        # Restore FK
        op.create_foreign_key(
            "jobs_sample_id_fkey", "jobs", "samples",
            ["sample_id"], ["sample_id"],
        )

    # -- collections ----------------------------------------------------------
    op.create_table(
        "collections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("collection_id", sa.String, nullable=False, unique=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("metadata_", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_collections_collection_id", "collections", ["collection_id"])

    op.create_table(
        "collection_samples",
        sa.Column("collection_id", sa.String,
                  sa.ForeignKey("collections.collection_id"), nullable=False),
        sa.Column("sample_id", sa.String,
                  sa.ForeignKey("samples.sample_id"), nullable=False),
        sa.UniqueConstraint("collection_id", "sample_id", name="uq_collection_sample"),
    )


def downgrade() -> None:
    op.drop_table("collection_samples")
    op.drop_table("collections")

    op.drop_index("ix_samples_biosample_id", table_name="samples")
    op.drop_column("samples", "biosample_id")
    op.drop_column("samples", "ncbi_accession")
    # Note: sample_id values rewritten by upgrade() are NOT reversed.
    # Downgrade restores schema only, not data.
