"""telemetry_jsonb_indexes

Revision ID: c1d2e3f4
Revises: b1c2d3e4
Create Date: 2026-05-10

Adds functional indexes on telemetry.trace JSONB fields used by the
analytical query path (#71). The two key fields hit by every query in
services/process_metrics.py and services/cohort.py are:

  - trace->>'process'   (the Nextflow process name)
  - trace->>'status'    (COMPLETED / FAILED / ABORTED)

We only need them for `event = 'process_completed'` rows since the other
event types don't carry a useful trace.process. Partial indexes on that
predicate keep the index small and the planner happy.

If/when the telemetry table grows large enough that CREATE INDEX becomes
disruptive, future migrations should use CREATE INDEX CONCURRENTLY (which
requires running outside a transaction; see Alembic's autocommit_block).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telemetry_trace_process
          ON telemetry ((trace->>'process'))
          WHERE event = 'process_completed'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telemetry_trace_status
          ON telemetry ((trace->>'status'))
          WHERE event = 'process_completed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_telemetry_trace_status")
    op.execute("DROP INDEX IF EXISTS ix_telemetry_trace_process")
