"""create task_executions table

Revision ID: e3f4a5b6
Revises: d2e3f4a5
Create Date: 2026-07-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telemetry_id", sa.Integer(), sa.ForeignKey("telemetry.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("run_name", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("sample_id", sa.String(), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("workflow_version", sa.String(), nullable=True),
        sa.Column("utc_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("task_hash", sa.String(), nullable=True),
        sa.Column("process", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("exit_code", sa.String(), nullable=True),
        sa.Column("error_action", sa.String(), nullable=True),
        sa.Column("realtime_ms", sa.Float(), nullable=True),
        sa.Column("requested_cpus", sa.Float(), nullable=True),
        sa.Column("requested_memory_bytes", sa.Float(), nullable=True),
        sa.Column("requested_time_ms", sa.Float(), nullable=True),
        sa.Column("pct_cpu", sa.Float(), nullable=True),
        sa.Column("pct_mem", sa.Float(), nullable=True),
        sa.Column("peak_rss", sa.Float(), nullable=True),
        sa.Column("read_bytes", sa.Float(), nullable=True),
        sa.Column("write_bytes", sa.Float(), nullable=True),
        sa.Column("rchar", sa.Float(), nullable=True),
        sa.Column("wchar", sa.Float(), nullable=True),
    )
    op.create_index("ix_task_executions_run_name", "task_executions", ["run_name"])
    op.create_index("ix_task_executions_sample_id", "task_executions", ["sample_id"])
    op.create_index("ix_task_executions_workflow_id", "task_executions", ["workflow_id"])
    op.create_index("ix_task_executions_process", "task_executions", ["process"])
    op.create_index("ix_task_executions_status", "task_executions", ["status"])
    op.create_index("ix_task_executions_process_status", "task_executions", ["process", "status"])
    op.create_index("ix_task_executions_utc_time", "task_executions", ["utc_time"])
    op.create_index("ix_task_executions_composite_metrics", "task_executions", ["workflow_id", "workflow_version", "status"])

    # Backfill task_executions from existing telemetry rows
    op.execute(
        """
        INSERT INTO task_executions (
            telemetry_id, run_name, run_id, sample_id, workflow_id, workflow_version,
            utc_time, task_id, task_hash, process, name, status, attempt,
            exit_code, error_action, realtime_ms, requested_cpus, requested_memory_bytes,
            requested_time_ms, pct_cpu, pct_mem, peak_rss, read_bytes, write_bytes,
            rchar, wchar
        )
        SELECT
            id as telemetry_id,
            run_name,
            run_id,
            sample_id,
            workflow_id,
            workflow_version,
            utc_time,
            coalesce(trace->>'task_id', '') as task_id,
            trace->>'hash' as task_hash,
            coalesce(trace->>'process', '') as process,
            trace->>'name' as name,
            coalesce(trace->>'status', '') as status,
            coalesce(nullif(trace->>'attempt', ''), '1')::int as attempt,
            trace->>'exit' as exit_code,
            trace->>'error_action' as error_action,
            nullif(trace->>'realtime', '')::double precision as realtime_ms,
            nullif(trace->>'cpus', '')::double precision as requested_cpus,
            nullif(trace->>'memory', '')::double precision as requested_memory_bytes,
            nullif(trace->>'time', '')::double precision as requested_time_ms,
            nullif(trace->>'%cpu', '')::double precision as pct_cpu,
            nullif(trace->>'%mem', '')::double precision as pct_mem,
            nullif(trace->>'peak_rss', '')::double precision as peak_rss,
            nullif(trace->>'read_bytes', '')::double precision as read_bytes,
            nullif(trace->>'write_bytes', '')::double precision as write_bytes,
            nullif(trace->>'rchar', '')::double precision as rchar,
            nullif(trace->>'wchar', '')::double precision as wchar
        FROM telemetry
        WHERE event = 'process_completed'
          AND trace IS NOT NULL
        ON CONFLICT (telemetry_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("task_executions")
