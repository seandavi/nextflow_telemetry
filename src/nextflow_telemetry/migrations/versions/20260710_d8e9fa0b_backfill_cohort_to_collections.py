"""backfill_cohort_to_collections

Revision ID: d8e9fa0b
Revises: c7d8e9fa
Create Date: 2026-07-10

Retire `metadata.cohort` as a collection-membership encoding (ADR-0005). The
Samples page used to read a scalar `samples.metadata_->>'cohort'`, while the
Cohorts dashboard read `collections`/`collection_samples`, so the two disagreed.

This migration folds the legacy scalar into the single source of truth:

  1. Each distinct `metadata_->>'cohort'` value becomes a collection
     (source='legacy'; its exact string is the collection_id — see CONTEXT.md).
     ON CONFLICT DO NOTHING so a value that already names a real collection
     (e.g. an accession) merges into it rather than duplicating.
  2. A membership row is created for every sample carrying that cohort value.
  3. The now-dead `cohort` key is stripped from `samples.metadata_`. Other keys
     (phenotype, source, provenance) are untouched — the sample row keeps its
     metadata column, only this key goes.

Irreversible: the pre-strip cohort strings can't be reconstructed, so downgrade
leaves the derived collections/membership in place and is otherwise a no-op.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "d8e9fa0b"
down_revision: Union[str, Sequence[str], None] = "c7d8e9fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO collections (collection_id, source, type, label, metadata_, created_at, updated_at)
        SELECT DISTINCT metadata_->>'cohort', 'legacy', NULL, metadata_->>'cohort', NULL::jsonb, now(), now()
        FROM samples
        WHERE metadata_->>'cohort' IS NOT NULL AND metadata_->>'cohort' <> ''
        ON CONFLICT (collection_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO collection_samples (collection_id, sample_id)
        SELECT metadata_->>'cohort', sample_id
        FROM samples
        WHERE metadata_->>'cohort' IS NOT NULL AND metadata_->>'cohort' <> ''
        ON CONFLICT ON CONSTRAINT uq_collection_sample DO NOTHING
        """
    )
    op.execute("UPDATE samples SET metadata_ = metadata_ - 'cohort' WHERE metadata_ ? 'cohort'")


def downgrade() -> None:
    # Irreversible: the stripped cohort strings are gone. The derived collections
    # and membership rows are harmless, so we leave them and no-op.
    pass
