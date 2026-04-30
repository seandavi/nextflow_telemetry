"""Integration tests against a real postgres instance (testcontainers).

All tests are synchronous — DB verification uses asyncio.run() with a
short-lived engine to avoid event loop sharing issues between fixtures.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select
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
    workflow_id: str = "curatedMetagenomics",
    workflow_version: str = "1.0.0",
) -> dict:
    tag = f"{sample_id}:{run_name}" if sample_id else None
    return {
        "runId": run_id,
        "runName": run_name,
        "event": event,
        "utcTime": "2026-01-01T00:00:00",
        "metadata": {
            "params": {
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
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
    return asyncio.run(coro)


async def _query(db_url: str, stmt):
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            return result.mappings().all()
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


# ---------------------------------------------------------------------------
# Basic connectivity
# ---------------------------------------------------------------------------

def test_health_endpoint(integration_client):
    client, _ = integration_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "Healthy"


# ---------------------------------------------------------------------------
# Sample catalog
# ---------------------------------------------------------------------------

def test_register_sample_creates_row(integration_client, db_url):
    from nextflow_telemetry.db import samples_tbl

    client, _ = integration_client
    sample_id = f"SRR-{uuid.uuid4().hex[:8]}"

    resp = client.post("/samples", json={"sample_id": sample_id, "metadata": {"source": "gut"}})
    assert resp.status_code == 201
    data = resp.json()
    assert data["sample_id"] == sample_id
    assert data["metadata"]["source"] == "gut"

    rows = _run(_query(db_url, select(samples_tbl).where(
        samples_tbl.c.sample_id == sample_id
    )))
    assert len(rows) == 1


def test_register_sample_idempotent(integration_client):
    client, _ = integration_client
    sample_id = f"SRR-{uuid.uuid4().hex[:8]}"

    resp1 = client.post("/samples", json={"sample_id": sample_id, "metadata": {"v": 1}})
    resp2 = client.post("/samples", json={"sample_id": sample_id, "metadata": {"v": 2}})
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # Second upsert updates metadata
    assert resp2.json()["metadata"]["v"] == 2


def test_list_samples(integration_client):
    client, _ = integration_client
    sample_id = f"SRR-{uuid.uuid4().hex[:8]}"
    client.post("/samples", json={"sample_id": sample_id})

    resp = client.get("/samples")
    assert resp.status_code == 200
    ids = [r["sample_id"] for r in resp.json()]
    assert sample_id in ids


def test_get_sample_not_found(integration_client):
    client, _ = integration_client
    resp = client.get("/samples/DOES_NOT_EXIST")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Workflow registry
# ---------------------------------------------------------------------------

def _wf_payload(**overrides) -> dict:
    base = {
        "workflow_id": f"wf-{uuid.uuid4().hex[:6]}",
        "version": "1.0.0",
        "repository_url": "https://github.com/org/pipeline",
        "revision": "abc1234",
        "profile": "standard",
    }
    return {**base, **overrides}


def test_register_workflow(integration_client, db_url):
    from nextflow_telemetry.db import workflows_tbl

    client, _ = integration_client
    payload = _wf_payload()

    resp = client.post("/workflows", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["workflow_id"] == payload["workflow_id"]
    assert data["status"] == "active"
    assert data["revision"] == "abc1234"

    rows = _run(_query(db_url, select(workflows_tbl).where(
        workflows_tbl.c.id == data["id"]
    )))
    assert len(rows) == 1


def test_workflow_status_lifecycle(integration_client):
    client, _ = integration_client
    resp = client.post("/workflows", json=_wf_payload())
    wf_id = resp.json()["id"]

    resp = client.patch(f"/workflows/{wf_id}/status", json={"status": "paused"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = client.patch(f"/workflows/{wf_id}/status", json={"status": "retired"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"


def test_workflow_invalid_status_rejected(integration_client):
    client, _ = integration_client
    resp = client.post("/workflows", json=_wf_payload())
    wf_id = resp.json()["id"]

    resp = client.patch(f"/workflows/{wf_id}/status", json={"status": "deleted"})
    assert resp.status_code == 422


def test_workflow_revision_update(integration_client):
    client, _ = integration_client
    resp = client.post("/workflows", json=_wf_payload(revision="v1.0"))
    wf_id = resp.json()["id"]

    resp = client.patch(f"/workflows/{wf_id}/revision", json={"revision": "v1.1-fix"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "v1.1-fix"


def test_list_workflows_with_status_filter(integration_client):
    client, _ = integration_client
    wf_id_str = f"wf-filter-{uuid.uuid4().hex[:6]}"
    resp = client.post("/workflows", json=_wf_payload(workflow_id=wf_id_str))
    wf_pk = resp.json()["id"]
    client.patch(f"/workflows/{wf_pk}/status", json={"status": "paused"})

    active = client.get("/workflows?status=active").json()
    paused = client.get("/workflows?status=paused").json()
    assert not any(w["id"] == wf_pk for w in active)
    assert any(w["id"] == wf_pk for w in paused)


# ---------------------------------------------------------------------------
# Reconcile jobs
# ---------------------------------------------------------------------------

def test_reconcile_creates_jobs(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl

    client, _ = integration_client
    sample_id = f"SRR-recon-{uuid.uuid4().hex[:6]}"
    wf_payload = _wf_payload()

    client.post("/samples", json={"sample_id": sample_id})
    wf_resp = client.post("/workflows", json=wf_payload)
    wf_pk = wf_resp.json()["id"]

    resp = client.post("/admin/reconcile-jobs")
    assert resp.status_code == 200
    created = resp.json()["jobs_created"]
    assert created >= 1

    rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_pk == wf_pk,
    )))
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_reconcile_is_idempotent(integration_client):
    client, _ = integration_client
    sample_id = f"SRR-idemp-{uuid.uuid4().hex[:6]}"
    client.post("/samples", json={"sample_id": sample_id})
    client.post("/workflows", json=_wf_payload(workflow_id=f"idemp-{uuid.uuid4().hex[:6]}"))

    r1 = client.post("/admin/reconcile-jobs").json()["jobs_created"]
    r2 = client.post("/admin/reconcile-jobs").json()["jobs_created"]
    # Second call creates zero new jobs for existing pairs
    assert r2 == 0 or r2 < r1


def test_reconcile_skips_paused_workflows(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl

    client, _ = integration_client
    sample_id = f"SRR-paused-{uuid.uuid4().hex[:6]}"
    wf_payload = _wf_payload(workflow_id=f"paused-{uuid.uuid4().hex[:6]}")

    client.post("/samples", json={"sample_id": sample_id})
    wf_resp = client.post("/workflows", json=wf_payload)
    wf_pk = wf_resp.json()["id"]
    client.patch(f"/workflows/{wf_pk}/status", json={"status": "paused"})

    client.post("/admin/reconcile-jobs")

    rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_pk == wf_pk,
    )))
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Dispatch protocol (Phase 2)
# ---------------------------------------------------------------------------

def _seed_job(client, *, workflow_id: str | None = None, sample_suffix: str = "") -> tuple[str, str, int]:
    """Register a sample + active workflow + reconcile. Returns (sample_id, workflow_id, wf_pk)."""
    sample_id = f"SRR-disp-{uuid.uuid4().hex[:6]}{sample_suffix}"
    wf_id = workflow_id or f"wf-disp-{uuid.uuid4().hex[:6]}"
    wf_resp = client.post("/workflows", json=_wf_payload(workflow_id=wf_id))
    wf_pk = wf_resp.json()["id"]
    client.post("/samples", json={"sample_id": sample_id})
    client.post("/admin/reconcile-jobs")
    return sample_id, wf_id, wf_pk


def test_dispatch_batch_no_pending_returns_204(integration_client):
    client, _ = integration_client
    # Request a workflow_id that has no jobs
    resp = client.post("/dispatch/batch", json={"workflow_id": "no-such-wf", "limit": 5})
    assert resp.status_code == 204


def test_dispatch_batch_claims_pending_jobs(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl

    client, _ = integration_client
    sample_id, wf_id, wf_pk = _seed_job(client)

    resp = client.post("/dispatch/batch", json={"workflow_id": wf_id, "limit": 10})
    assert resp.status_code == 200
    data = resp.json()

    assert "run_name" in data
    assert data["repository_url"] == "https://github.com/org/pipeline"
    assert data["revision"] == "abc1234"
    assert len(data["jobs"]) >= 1
    assert any(j["sample_id"] == sample_id for j in data["jobs"])

    # Verify workflow_run created
    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == data["run_name"]
    )))
    assert len(rows) == 1
    assert rows[0]["status"] == "claimed"
    assert rows[0]["revision"] == "abc1234"

    # Verify job status
    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_pk == wf_pk,
    )))
    assert job_rows[0]["status"] == "claimed"


def test_dispatch_submitted_transitions(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl

    client, _ = integration_client
    sample_id, wf_id, wf_pk = _seed_job(client)

    batch_resp = client.post("/dispatch/batch", json={"workflow_id": wf_id, "limit": 10})
    assert batch_resp.status_code == 200
    run_name = batch_resp.json()["run_name"]
    sample_ids = [j["sample_id"] for j in batch_resp.json()["jobs"]]

    resp = client.post("/dispatch/submitted", json={
        "run_name": run_name,
        "executor_job_id": "SLURM_42",
        "sample_ids": sample_ids,
    })
    assert resp.status_code == 200

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert run_rows[0]["status"] == "submitted"
    assert run_rows[0]["executor_job_id"] == "SLURM_42"

    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.run_name == run_name,
    )))
    assert all(r["status"] == "running" for r in job_rows)


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


def test_telemetry_started_updates_run_status(integration_client, db_url):
    from nextflow_telemetry.db import workflow_runs_tbl

    client, _ = integration_client
    run_name = _make_run_name()
    run_id = str(uuid.uuid4())

    _run(_exec(
        db_url,
        insert(workflow_runs_tbl).values(
            run_name=run_name,
            workflow_id="curatedMetagenomics",
            workflow_version="1.0.0",
            status="submitted",
        ),
    ))

    resp = client.post("/telemetry",
                       json=_weblog_payload(run_id=run_id, run_name=run_name, event="started"))
    assert resp.status_code == 200

    rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert rows[0]["status"] == "running"
    assert rows[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Full lifecycle: reconcile → dispatch → submit → MARK_COMPLETE → completed
# ---------------------------------------------------------------------------

def test_full_lifecycle(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl, dead_letter_tbl

    client, _ = integration_client
    # Unique version so reconcile+dispatch scope is exactly our 3 samples.
    wf_id = f"wf-lifecycle-{uuid.uuid4().hex[:6]}"
    wf_version = f"lc-{uuid.uuid4().hex[:8]}"
    sample_ids_all = [f"SRR-lc-{uuid.uuid4().hex[:6]}" for _ in range(3)]

    # Register workflow + samples
    wf_resp = client.post("/workflows", json=_wf_payload(
        workflow_id=wf_id, version=wf_version,
    ))
    assert wf_resp.status_code == 201
    wf_pk = wf_resp.json()["id"]

    for sid in sample_ids_all:
        client.post("/samples", json={"sample_id": sid})
    client.post("/admin/reconcile-jobs")

    # Dispatch — use a large limit; filter by both workflow_id AND version so
    # only this test's 3 jobs are returned (shared DB has samples from prior tests).
    batch_resp = client.post("/dispatch/batch", json={
        "workflow_id": wf_id,
        "workflow_version": wf_version,
        "limit": 500,
    })
    assert batch_resp.status_code == 200
    batch = batch_resp.json()
    run_name = batch["run_name"]
    run_id = str(uuid.uuid4())
    dispatched_ids = [j["sample_id"] for j in batch["jobs"]]

    # All 3 of our samples should have been dispatched
    assert set(sample_ids_all).issubset(set(dispatched_ids))

    # Submit
    client.post("/dispatch/submitted", json={
        "run_name": run_name,
        "executor_job_id": "SLURM_77",
        "sample_ids": dispatched_ids,
    })

    # Nextflow fires "started"
    client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="started",
        workflow_id=wf_id, workflow_version=wf_version,
    ))

    # Two of our samples complete via MARK_COMPLETE, one does not
    for sid in sample_ids_all[:2]:
        client.post("/telemetry", json=_weblog_payload(
            run_id=run_id, run_name=run_name, event="process_completed",
            sample_id=sid, process_name="MARK_COMPLETE", process_status="COMPLETED",
        ))

    # Pipeline-level completion triggers sweep
    client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="completed",
    ))

    # Verify our 3 samples: 2 completed, 1 failed
    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.workflow_pk == wf_pk,
        jobs_tbl.c.sample_id.in_(sample_ids_all),
    )))
    statuses = {r["sample_id"]: r["status"] for r in job_rows}
    assert statuses[sample_ids_all[0]] == "completed"
    assert statuses[sample_ids_all[1]] == "completed"
    assert statuses[sample_ids_all[2]] == "failed"

    # Only the 1 failed sample (from our test) should appear in DLQ for this run
    dlq_rows = _run(_query(db_url, select(dead_letter_tbl).where(
        dead_letter_tbl.c.run_name == run_name,
        dead_letter_tbl.c.sample_id == sample_ids_all[2],
    )))
    assert len(dlq_rows) == 1

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert run_rows[0]["status"] == "completed"
