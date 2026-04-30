"""dlq_unique_job_id

Revision ID: c8d1e4f2a9b3
Revises: b3f7a912c041
Create Date: 2026-04-30

Adds a UNIQUE constraint on dead_letter.job_id to prevent duplicate DLQ
entries for the same job (e.g. from duplicate 'completed' weblog events).

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d1e4f2a9b3"
down_revision: Union[str, None] = "b3f7a912c041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint("uq_dlq_job_id", "dead_letter", ["job_id"])


def downgrade() -> None:
    op.drop_constraint("uq_dlq_job_id", "dead_letter", type_="unique")
