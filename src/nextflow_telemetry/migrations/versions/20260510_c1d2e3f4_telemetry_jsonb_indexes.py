"""telemetry_jsonb_indexes

Revision ID: c1d2e3f4
Revises: b1c2d3e4
Create Date: 2026-05-10

Adds functional indexes on telemetry.trace JSONB fields used by the
analytical query path (#71). The two key fields hit by every query in
services/process_metrics.py and services/cohort.py are:

  - trace->>'process'   (the Nextflow process name)
  - trace->>'status'    (COMPLETED / FAILED / ABORTED)

We only need them for `event = 'process_completed'` rows since the
other event types don't carry a useful trace.process. Partial indexes
on that predicate alone keep the index small (this filter accounts for
~all the trace volume) and the predicate is one Postgres can prove
holds for any analytical query that has `event = 'process_completed'`
in its WHERE — no need for the queries to repeat any extra condition.

A tighter `AND trace IS NOT NULL` partial was considered but rejected:
not every cohort/analytics query repeats that check explicitly, and
when the partial predicate has more conjuncts than the query, the
planner has to *prove* the implication, which it doesn't always do.
Dropping the extra conjunct keeps the index broadly usable.

Created CONCURRENTLY so the migration doesn't take a long lock on the
table when applied to a production database with ongoing writes. Alembic
runs each migration inside a transaction by default, but
CREATE INDEX CONCURRENTLY can't run in a transaction — autocommit_block
takes us out of the surrounding transaction for the duration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_telemetry_trace_process",
            "telemetry",
            [sa.text("(trace->>'process')")],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text("event = 'process_completed'"),
            if_not_exists=True,
        )
        op.create_index(
            "ix_telemetry_trace_status",
            "telemetry",
            [sa.text("(trace->>'status')")],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text("event = 'process_completed'"),
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_telemetry_trace_status",
            table_name="telemetry",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_telemetry_trace_process",
            table_name="telemetry",
            postgresql_concurrently=True,
            if_exists=True,
        )
