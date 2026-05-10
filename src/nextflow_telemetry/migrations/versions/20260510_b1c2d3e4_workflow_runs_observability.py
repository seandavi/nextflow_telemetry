"""workflow_runs_observability

Revision ID: b1c2d3e4
Revises: a1b2c3d4
Create Date: 2026-05-10

Adds run-lifecycle observability columns to workflow_runs (#62 / #63):
  - last_heartbeat_at        — wrapper-emitted heartbeat (Phase 3); set to
                               server receipt time, not client utc_time
  - last_known_slurm_state   — most recent sacct state (Phase 4)
  - slurm_reason             — sacct Reason field for the most recent state
                               event (cleared to NULL when the latest state
                               event omits it; not just terminal states)
  - wrapper_exit_code        — exit code from `nextflow run` reported by the wrapper
  - wait_seconds             — queue wait, submit→start
  - nextflow_log_uploaded_at — sentinel: have we received the .nextflow.log?

(A separate `slurm_exit_code` column may be added in Phase 4 once sacct
polling is wired up — sacct's ExitCode is a different value populated by
a different code path, and conflating the two has been confusing.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_runs", sa.Column("last_known_slurm_state", sa.Text(), nullable=True))
    op.add_column("workflow_runs", sa.Column("slurm_reason", sa.Text(), nullable=True))
    op.add_column("workflow_runs", sa.Column("wrapper_exit_code", sa.Integer(), nullable=True))
    op.add_column("workflow_runs", sa.Column("wait_seconds", sa.Integer(), nullable=True))
    op.add_column("workflow_runs", sa.Column("nextflow_log_uploaded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "nextflow_log_uploaded_at")
    op.drop_column("workflow_runs", "wait_seconds")
    op.drop_column("workflow_runs", "wrapper_exit_code")
    op.drop_column("workflow_runs", "slurm_reason")
    op.drop_column("workflow_runs", "last_known_slurm_state")
    op.drop_column("workflow_runs", "last_heartbeat_at")
