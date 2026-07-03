"""collections_type

Revision ID: a5b6c7d8
Revises: f4a5b6c7
Create Date: 2026-07-03

Adds collections.type — the *kind* of collection (meta | study | project),
a separate axis from collections.source (provenance). Flat model: a
meta-collection (e.g. CMD) is still a flat bag of samples; nesting is deferred.

Backfill of existing rows uses source, which at this point uniquely implies
kind: every 'sra_study' row was seeded from a study curation TSV (study), and
'bioproject' rows are accession-derived (project). 'manual' rows can't be
inferred (CMD included) and are left NULL for a human to tag.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("type", sa.String(), nullable=True))
    op.execute("UPDATE collections SET type = 'study' WHERE source = 'sra_study'")
    op.execute("UPDATE collections SET type = 'project' WHERE source = 'bioproject'")


def downgrade() -> None:
    op.drop_column("collections", "type")
