"""Integration tests for the run-lifecycle event router (POST /api/runs/{run_name}/event).

Spec: issue #63. Tests use the shared testcontainers postgres fixture from
conftest.py (no mocks).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine


def _make_run_name() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(coro):
    return asyncio.run(coro)


async def _query(db_url: str, stmt):
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(stmt)).mappings().all()
    finally:
        await engine.dispose()


async def _exec(db_url: str, *stmts):
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(stmt)
    finally:
        await engine.dispose()


def _seed_run(db_url: str, run_name: str, *, status: str = "claimed") -> None:
    from nextflow_telemetry.db import workflow_runs_tbl
    _run(_exec(
        db_url,
        insert(workflow_runs_tbl).values(
            run_name=run_name,
            workflow_id="curatedMetagenomics",
            workflow_version="1.0.0",
            status=status,
        ),
    ))


def _post_event(client, run_name: str, body: dict, *, file: tuple | None = None):
    payload: dict = {"data": {"event": json.dumps(body)}}
    if file is not None:
        payload["files"] = {"nextflow_log": file}
    return client.post(f"/api/runs/{run_name}/event", **payload)


# ---------------------------------------------------------------------------
# Each event type round-trips into telemetry_tbl + the right summary update
# ---------------------------------------------------------------------------

def test_wrapper_started_promotes_claimed_to_submitted(integration_client, db_url):
    from nextflow_telemetry.db import telemetry_tbl, workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name, status="claimed")

    resp = _post_event(client, run_name, {
        "type": "wrapper_started",
        "utc_time": _ts(),
        "hostname": "compute-01",
        "slurm_job_id": "12345",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "wrapper_started"
    assert body["nextflow_log_uploaded"] is False

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["status"] == "submitted"
    assert rows[0]["submitted_at"] is not None

    tel_rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name
    )))
    assert len(tel_rows) == 1
    assert tel_rows[0]["event"] == "run_wrapper_started"
    assert tel_rows[0]["metadata_"]["hostname"] == "compute-01"


def test_wrapper_started_does_not_roll_back_running_state(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name, status="running")

    resp = _post_event(client, run_name, {
        "type": "wrapper_started",
        "utc_time": _ts(),
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["status"] == "running"  # unchanged


def test_pre_nextflow_records_wait_seconds(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "pre_nextflow",
        "utc_time": _ts(),
        "wait_seconds": 312,
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["wait_seconds"] == 312


def test_heartbeat_updates_last_heartbeat_at(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "heartbeat",
        "utc_time": _ts(),
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["last_heartbeat_at"] is not None


def test_slurm_state_records_state_and_reason(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "slurm_state",
        "utc_time": _ts(),
        "state": "TIMEOUT",
        "reason": "WallTimeLimit",
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["last_known_slurm_state"] == "TIMEOUT"
    assert rows[0]["slurm_reason"] == "WallTimeLimit"


def test_wrapper_exited_records_exit_code(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "wrapper_exited",
        "utc_time": _ts(),
        "exit_code": 137,
        "duration_seconds": 1842,
    })
    assert resp.status_code == 201
    assert resp.json()["nextflow_log_uploaded"] is False

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["wrapper_exit_code"] == 137


def test_wrapper_exited_with_attached_log_stores_in_task_logs(integration_client, db_url):
    from nextflow_telemetry.db import task_logs_tbl, workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    log_content = "Sep 09 12:34:56 - DEBUG - Nextflow starting\n... rest of log ...\n"

    resp = _post_event(
        client,
        run_name,
        {"type": "wrapper_exited", "utc_time": _ts(), "exit_code": 0},
        file=("nextflow.log", log_content.encode(), "text/plain"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["nextflow_log_uploaded"] is True

    log_rows = _run(_query(db_url, select(task_logs_tbl).where(
        task_logs_tbl.c.run_name == run_name
    )))
    assert len(log_rows) == 1
    assert log_rows[0]["task_hash"] == "nextflow_log"
    assert log_rows[0]["log_type"] == "nextflow_log"
    assert log_rows[0]["content"] == log_content

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert run_rows[0]["nextflow_log_uploaded_at"] is not None


def test_re_uploading_nextflow_log_is_idempotent(integration_client, db_url):
    from nextflow_telemetry.db import task_logs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    for content in ("first\n", "second\n"):
        resp = _post_event(
            client,
            run_name,
            {"type": "wrapper_exited", "utc_time": _ts(), "exit_code": 0},
            file=("nextflow.log", content.encode(), "text/plain"),
        )
        assert resp.status_code == 201

    rows = _run(_query(db_url, select(task_logs_tbl).where(
        task_logs_tbl.c.run_name == run_name
    )))
    assert len(rows) == 1  # UNIQUE constraint upserts
    assert rows[0]["content"] == "second\n"


def test_workflow_oncomplete_event_persists_to_telemetry(integration_client, db_url):
    from nextflow_telemetry.db import telemetry_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "workflow_oncomplete",
        "utc_time": _ts(),
        "success": False,
        "exit_status": 1,
        "duration_ms": 12345,
        "error_message": "Something blew up",
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name,
        telemetry_tbl.c.event == "run_workflow_oncomplete",
    )))
    assert len(rows) == 1
    assert rows[0]["metadata_"]["error_message"] == "Something blew up"


def test_workflow_onerror_event_persists(integration_client, db_url):
    from nextflow_telemetry.db import telemetry_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "workflow_onerror",
        "utc_time": _ts(),
        "error_message": "boom",
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name,
        telemetry_tbl.c.event == "run_workflow_onerror",
    )))
    assert len(rows) == 1


def test_wrapper_log_event_persists(integration_client, db_url):
    from nextflow_telemetry.db import telemetry_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "wrapper_log",
        "utc_time": _ts(),
        "stream": "stderr",
        "text": "sbatch: error: foo",
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name,
        telemetry_tbl.c.event == "run_wrapper_log",
    )))
    assert len(rows) == 1
    assert rows[0]["metadata_"]["stream"] == "stderr"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_unknown_event_type_returns_422(integration_client, db_url):
    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = _post_event(client, run_name, {
        "type": "not_a_real_event",
        "utc_time": _ts(),
    })
    assert resp.status_code == 422


def test_malformed_event_json_returns_422(integration_client, db_url):
    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    resp = client.post(
        f"/api/runs/{run_name}/event",
        data={"event": "not-json"},
    )
    assert resp.status_code == 422


def test_event_for_unknown_run_still_records_raw(integration_client, db_url):
    """Wrapper-races-claim case — raw event must be captured even with no workflow_runs row."""
    from nextflow_telemetry.db import telemetry_tbl, workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()  # no _seed_run

    resp = _post_event(client, run_name, {
        "type": "heartbeat",
        "utc_time": _ts(),
    })
    assert resp.status_code == 201

    tel_rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name
    )))
    assert len(tel_rows) == 1

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert len(run_rows) == 0  # we don't pollute workflow_runs with orphan events


def test_log_attachment_on_unknown_run_returns_404(integration_client, db_url):
    """A .nextflow.log without a known run is rejected — no orphan task_logs row."""
    from nextflow_telemetry.db import task_logs_tbl, telemetry_tbl

    client, _ = integration_client
    run_name = _make_run_name()  # no _seed_run

    resp = _post_event(
        client, run_name,
        {"type": "wrapper_exited", "utc_time": _ts(), "exit_code": 0},
        file=("nextflow.log", b"would-be-orphan", "text/plain"),
    )
    assert resp.status_code == 404

    # No orphan log row was created.
    log_rows = _run(_query(db_url, select(task_logs_tbl).where(
        task_logs_tbl.c.run_name == run_name
    )))
    assert log_rows == []

    # Transaction was rolled back: telemetry row must not be there either.
    tel_rows = _run(_query(db_url, select(telemetry_tbl).where(
        telemetry_tbl.c.run_name == run_name
    )))
    assert tel_rows == []


def test_slurm_state_event_without_reason_clears_stale_reason(integration_client, db_url):
    """A later state event without a reason should NULL the column, not leave a stale value."""
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    # First: state with a reason.
    resp = _post_event(client, run_name, {
        "type": "slurm_state",
        "utc_time": _ts(),
        "state": "PENDING",
        "reason": "Resources",
    })
    assert resp.status_code == 201
    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["slurm_reason"] == "Resources"

    # Then: a state transition with no reason — the column must clear.
    resp = _post_event(client, run_name, {
        "type": "slurm_state",
        "utc_time": _ts(),
        "state": "RUNNING",
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["last_known_slurm_state"] == "RUNNING"
    assert rows[0]["slurm_reason"] is None


def test_out_of_order_pre_nextflow_after_exit_does_not_clobber_exit_code(integration_client, db_url):
    """A late pre_nextflow event must not roll back forward-looking state."""
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    _seed_run(db_url, run_name)

    # exit first
    resp = _post_event(client, run_name, {
        "type": "wrapper_exited",
        "utc_time": _ts(),
        "exit_code": 0,
    })
    assert resp.status_code == 201

    # late pre_nextflow arrives — should add wait_seconds, leave exit_code alone
    resp = _post_event(client, run_name, {
        "type": "pre_nextflow",
        "utc_time": _ts(),
        "wait_seconds": 12,
    })
    assert resp.status_code == 201

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["wrapper_exit_code"] == 0
    assert rows[0]["wait_seconds"] == 12
