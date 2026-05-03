"""drop_workflow_profile

Revision ID: d4e1f8c2b7a5
Revises: c8d1e4f2a9b3
Create Date: 2026-05-03

Removes the `profile` column from the `workflows` table. Profile is now
execution-environment-specific and lives in the nf_client config (ClientConfig.profile),
allowing the same workflow definition to run on different HPC systems (e.g. anvil, alpine).

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e1f8c2b7a5"
down_revision: Union[str, None] = "c8d1e4f2a9b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("workflows", "profile")


def downgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("profile", sa.String(), nullable=False, server_default="standard"),
    )
