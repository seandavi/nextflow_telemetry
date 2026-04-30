"""Integration tests against a real postgres instance (testcontainers).

All tests are synchronous — DB verification uses asyncio.run() with a
short-lived engine to avoid event loop sharing issues between fixtures.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_name() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


def _weblog_payload(
    *,
    run_id: str,
    run_name: str,
    event: str,
    sample_id: str | None = None,
    process_name: str | None = None,
    process_status: str = "COMPLETED",
) -> dict:
    tag = f"{sample_id}:{run_name}" if sample_id else None
    return {
        "runId": run_id,
        "runName": run_name,
        "event": event,
        "utcTime": "2026-01-01T00:00:00",
        "metadata": {
            "params": {
                "workflow_id": "curatedMetagenomics",
                "workflow_version": "1.0.0",
            }
        },
        "trace": {
            "tag": tag,
            "process": process_name or "FETCH_READS",
            "status": process_status,
            "task_id": "1",
            "hash": "ab/cdef12",
            "name": f"{process_name or 'FETCH_READS'} ({sample_id})",
        } if sample_id else None,
    }


def _run(coro):
    """Run an async coroutine from a sync test."""
    return asyncio.run(coro)


async def _query(db_url: str, stmt):
    """Execute a select against the test DB and return all rows as dicts."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            return result.mappings().all()
    finally:
        await engine.dispose()


async def _exec(db_url: str, *stmts):
    """Execute one or more DML statements against the test DB."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(stmt)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Basic connectivity
# ---------------------------------------------------------------------------

def test_health_endpoint(integration_client):
    client, _ = integration_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "Healthy"


# ---------------------------------------------------------------------------
# Telemetry ingest
# ---------------------------------------------------------------------------

def test_telemetry_ingest_stores_raw_event(integration_client, db_url):
    from nextflow_telemetry.db import telemetry_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    run_id = str(uuid.uuid4())

    resp = client.post(
        "/telemetry",
        json=_weblog_payload(
            run_id=run_id, run_name=run_name, event="process_completed",
            sample_id="SRR000001", process_name="FETCH_READS",
        ),
    )
    assert resp.status_code == 200

    rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name
    )))
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id
    assert rows[0]["event"] == "process_completed"
    assert rows[0]["sample_id"] == "SRR000001"


def test_telemetry_started_event_updates_run_record(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    run_id = str(uuid.uuid4())

    # Seed a submitted run record
    _run(_exec(
        db_url,
        insert(workflow_runs_tbl).values(
            run_name=run_name,
            workflow_id="curatedMetagenomics",
            workflow_version="1.0.0",
            status="submitted",
        ),
    ))

    resp = client.post(
        "/telemetry",
        json=_weblog_payload(run_id=run_id, run_name=run_name, event="started"),
    )
    assert resp.status_code == 200

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["status"] == "running"
    assert rows[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Dispatch protocol
# ---------------------------------------------------------------------------

def test_dispatch_batch_no_pending_returns_204(integration_client):
    client, _ = integration_client
    resp = client.post(
        "/dispatch/batch",
        json={"workflow_id": "no-such-wf", "workflow_version": "99.0", "limit": 5},
    )
    assert resp.status_code == 204


def test_dispatch_batch_claims_pending_executions(integration_client, db_url):
    from nextflow_telemetry.db import workflow_executions_tbl

    client, _ = integration_client
    run_name = _make_run_name()

    async def seed():
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(
                insert(workflow_executions_tbl),
                [
                    {
                        "sample_id": f"SRR{i:06d}",
                        "workflow_id": "curatedMetagenomics",
                        "workflow_version": "2.0.0",
                        "run_name": None,
                        "status": "pending",
                        "created_at": datetime.now(timezone.utc),
                    }
                    for i in range(1, 4)
                ],
            )
        await engine.dispose()

    _run(seed())

    resp = client.post(
        "/dispatch/batch",
        json={"workflow_id": "curatedMetagenomics", "workflow_version": "2.0.0", "limit": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "run_name" in data
    assert len(data["jobs"]) == 2
    assert all(j["workflow_version"] == "2.0.0" for j in data["jobs"])

    # Verify run record created
    from nextflow_telemetry.db import workflow_runs_tbl
    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == data["run_name"]
    )))
    assert len(rows) == 1
    assert rows[0]["status"] == "claimed"


def test_dispatch_submitted_transitions_to_submitted(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl, workflow_executions_tbl

    client, _ = integration_client
    run_name = _make_run_name()

    async def seed():
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(
                insert(workflow_runs_tbl).values(
                    run_name=run_name,
                    workflow_id="curatedMetagenomics",
                    workflow_version="1.0.0",
                    status="claimed",
                    claimed_at=datetime.now(timezone.utc),
                )
            )
            await conn.execute(
                insert(workflow_executions_tbl).values(
                    sample_id="SRR001",
                    workflow_id="curatedMetagenomics",
                    workflow_version="1.0.0",
                    run_name=run_name,
                    status="claimed",
                    created_at=datetime.now(timezone.utc),
                )
            )
        await engine.dispose()

    _run(seed())

    resp = client.post(
        "/dispatch/submitted",
        json={"run_name": run_name, "executor_job_id": "SLURM_999", "sample_ids": ["SRR001"]},
    )
    assert resp.status_code == 200

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    exec_rows = _run(_query(db_url, select(workflow_executions_tbl).where(
        workflow_executions_tbl.c.run_name == run_name
    )))

    assert run_rows[0]["status"] == "submitted"
    assert run_rows[0]["executor_job_id"] == "SLURM_999"
    assert exec_rows[0]["status"] == "running"


# ---------------------------------------------------------------------------
# MARK_COMPLETE semaphore
# ---------------------------------------------------------------------------

def test_mark_complete_event_completes_execution(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl, workflow_executions_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    run_id = str(uuid.uuid4())

    async def seed():
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(insert(workflow_runs_tbl).values(
                run_name=run_name, run_id=run_id,
                workflow_id="curatedMetagenomics", workflow_version="1.0.0",
                status="running",
            ))
            await conn.execute(insert(workflow_executions_tbl).values(
                sample_id="SRR001",
                workflow_id="curatedMetagenomics", workflow_version="1.0.0",
                run_name=run_name, status="running",
                created_at=datetime.now(timezone.utc),
            ))
        await engine.dispose()

    _run(seed())

    resp = client.post(
        "/telemetry",
        json=_weblog_payload(
            run_id=run_id, run_name=run_name,
            event="process_completed",
            sample_id="SRR001", process_name="MARK_COMPLETE",
            process_status="COMPLETED",
        ),
    )
    assert resp.status_code == 200

    rows = _run(_query(db_url, select(workflow_executions_tbl).where(
        workflow_executions_tbl.c.run_name == run_name,
        workflow_executions_tbl.c.sample_id == "SRR001",
    )))
    assert rows[0]["status"] == "completed"
    assert rows[0]["completed_at"] is not None


# ---------------------------------------------------------------------------
# DLQ sweep on workflow completion
# ---------------------------------------------------------------------------

def test_workflow_completed_sweeps_incomplete_to_dlq(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl, workflow_executions_tbl, dead_letter_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    run_id = str(uuid.uuid4())

    async def seed():
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(insert(workflow_runs_tbl).values(
                run_name=run_name, run_id=run_id,
                workflow_id="curatedMetagenomics", workflow_version="1.0.0",
                status="running",
            ))
            await conn.execute(
                insert(workflow_executions_tbl),
                [
                    {
                        "sample_id": sid,
                        "workflow_id": "curatedMetagenomics",
                        "workflow_version": "1.0.0",
                        "run_name": run_name,
                        "status": "running",
                        "created_at": datetime.now(timezone.utc),
                    }
                    for sid in ["SRR001", "SRR002", "SRR003"]
                ],
            )
        await engine.dispose()

    _run(seed())

    # SRR001 completes via MARK_COMPLETE
    client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="process_completed",
        sample_id="SRR001", process_name="MARK_COMPLETE", process_status="COMPLETED",
    ))

    # Pipeline-level completion fires
    resp = client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="completed",
    ))
    assert resp.status_code == 200

    exec_rows = _run(_query(db_url, select(workflow_executions_tbl).where(
        workflow_executions_tbl.c.run_name == run_name
    )))
    statuses = {r["sample_id"]: r["status"] for r in exec_rows}
    assert statuses["SRR001"] == "completed"
    assert statuses["SRR002"] == "failed"
    assert statuses["SRR003"] == "failed"

    dlq_rows = _run(_query(db_url, select(dead_letter_tbl).where(
        dead_letter_tbl.c.run_name == run_name
    )))
    assert len(dlq_rows) == 2
    dlq_samples = {r["sample_id"] for r in dlq_rows}
    assert dlq_samples == {"SRR002", "SRR003"}
