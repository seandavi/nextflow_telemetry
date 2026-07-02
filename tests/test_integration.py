"""Integration tests against a real postgres instance (testcontainers).

All tests are synchronous — DB verification uses asyncio.run() with a
short-lived engine to avoid event loop sharing issues between fixtures.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
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
    # Nextflow tags processes with a bare `${meta.sample}` (the sample id, no
    # run suffix) — mirror that here so the suite exercises the real wire format.
    tag = sample_id if sample_id else None
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

    resp = client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001", "metadata": {"source": "gut"}})
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

    resp1 = client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001", "metadata": {"v": 1}})
    resp2 = client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001", "metadata": {"v": 2}})
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # Second upsert updates metadata
    assert resp2.json()["metadata"]["v"] == 2


def test_list_samples(integration_client):
    client, _ = integration_client
    sample_id = f"SRR-{uuid.uuid4().hex[:8]}"
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})

    resp = client.get("/api/samples")
    assert resp.status_code == 200
    ids = [r["sample_id"] for r in resp.json()]
    assert sample_id in ids


def test_get_sample_not_found(integration_client):
    client, _ = integration_client
    resp = client.get("/api/samples/DOES_NOT_EXIST")
    assert resp.status_code == 404


_MISSING = object()  # sentinel: omit ncbi_accession from the payload entirely


@pytest.mark.parametrize("bad_value", [_MISSING, None, "", "   ", ";", " ; ; "])
def test_register_sample_rejects_empty_ncbi_accession(integration_client, bad_value):
    """Samples with no SRRs cause fasterq_dump to fail; reject at the API.

    Covers field-omitted, explicit JSON null, empty/whitespace strings, and
    semicolon-only strings — every input shape that parse_srrs() reduces to [].
    """
    client, _ = integration_client
    sample_id = f"SRR-{uuid.uuid4().hex[:8]}"
    payload: dict = {"sample_id": sample_id}
    if bad_value is not _MISSING:
        payload["ncbi_accession"] = bad_value
    resp = client.post("/api/samples", json=payload)
    assert resp.status_code == 422, resp.text


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

    resp = client.post("/api/workflows", json=payload)
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
    resp = client.post("/api/workflows", json=_wf_payload())
    wf_id = resp.json()["id"]

    resp = client.patch(f"/api/workflows/{wf_id}/status", json={"status": "paused"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = client.patch(f"/api/workflows/{wf_id}/status", json={"status": "retired"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"


def test_workflow_invalid_status_rejected(integration_client):
    client, _ = integration_client
    resp = client.post("/api/workflows", json=_wf_payload())
    wf_id = resp.json()["id"]

    resp = client.patch(f"/api/workflows/{wf_id}/status", json={"status": "deleted"})
    assert resp.status_code == 422


def test_workflow_revision_update(integration_client):
    client, _ = integration_client
    resp = client.post("/api/workflows", json=_wf_payload(revision="v1.0"))
    wf_id = resp.json()["id"]

    resp = client.patch(f"/api/workflows/{wf_id}/revision", json={"revision": "v1.1-fix"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "v1.1-fix"


def test_list_workflows_with_status_filter(integration_client):
    client, _ = integration_client
    wf_id_str = f"wf-filter-{uuid.uuid4().hex[:6]}"
    resp = client.post("/api/workflows", json=_wf_payload(workflow_id=wf_id_str))
    wf_pk = resp.json()["id"]
    client.patch(f"/api/workflows/{wf_pk}/status", json={"status": "paused"})

    active = client.get("/api/workflows?status=active").json()
    paused = client.get("/api/workflows?status=paused").json()
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

    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    wf_resp = client.post("/api/workflows", json=wf_payload)
    wf_pk = wf_resp.json()["id"]

    resp = client.post("/api/admin/reconcile-jobs")
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
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    client.post("/api/workflows", json=_wf_payload(workflow_id=f"idemp-{uuid.uuid4().hex[:6]}"))

    r1 = client.post("/api/admin/reconcile-jobs").json()["jobs_created"]
    r2 = client.post("/api/admin/reconcile-jobs").json()["jobs_created"]
    # Second call creates zero new jobs for existing pairs
    assert r2 == 0 or r2 < r1


def test_reconcile_skips_paused_workflows(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl

    client, _ = integration_client
    sample_id = f"SRR-paused-{uuid.uuid4().hex[:6]}"
    wf_payload = _wf_payload(workflow_id=f"paused-{uuid.uuid4().hex[:6]}")

    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    wf_resp = client.post("/api/workflows", json=wf_payload)
    wf_pk = wf_resp.json()["id"]
    client.patch(f"/api/workflows/{wf_pk}/status", json={"status": "paused"})

    client.post("/api/admin/reconcile-jobs")

    rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_pk == wf_pk,
    )))
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Dispatchability (no-active-daemon detection)
# ---------------------------------------------------------------------------

def _heartbeat(client, *, agent_id: str, workflow_id: str | None) -> None:
    client.put("/api/daemons/heartbeat", json={
        "agent_id": agent_id,
        "hostname": agent_id,
        "workflow_id": workflow_id,   # None = claims any workflow
        "mode": "slurm",
        "batch_size": 10,
        "status": "running",
    })


def test_dispatchability_flags_pending_with_no_active_daemon(integration_client):
    client, _ = integration_client
    _sample, wf_id, _pk = _seed_job(client, workflow_id=f"stuck-{uuid.uuid4().hex[:6]}")

    resp = client.get("/api/admin/dispatchability")
    assert resp.status_code == 200
    stuck_ids = {s["workflow_id"] for s in resp.json()["stuck"]}
    # No daemon heartbeated → our active workflow's pending jobs are unclaimed.
    assert wf_id in stuck_ids


def test_dispatchability_cleared_by_matching_daemon(integration_client):
    client, _ = integration_client
    _sample, wf_id, _pk = _seed_job(client, workflow_id=f"claimed-{uuid.uuid4().hex[:6]}")

    # A daemon whose filter includes this workflow (plus an unrelated one).
    _heartbeat(client, agent_id=f"agent-{uuid.uuid4().hex[:6]}",
               workflow_id=f"{wf_id},some-other-wf")

    stuck_ids = {s["workflow_id"] for s in client.get("/api/admin/dispatchability").json()["stuck"]}
    assert wf_id not in stuck_ids


def test_dispatchability_cleared_by_claim_any_daemon(integration_client):
    client, _ = integration_client
    _sample, wf_id, _pk = _seed_job(client, workflow_id=f"any-{uuid.uuid4().hex[:6]}")

    # A daemon with no workflow filter claims any workflow.
    _heartbeat(client, agent_id=f"agent-any-{uuid.uuid4().hex[:6]}", workflow_id=None)

    stuck_ids = {s["workflow_id"] for s in client.get("/api/admin/dispatchability").json()["stuck"]}
    assert wf_id not in stuck_ids


# ---------------------------------------------------------------------------
# Dispatch protocol (Phase 2)
# ---------------------------------------------------------------------------

def _seed_job(client, *, workflow_id: str | None = None, sample_suffix: str = "") -> tuple[str, str, int]:
    """Register a sample + active workflow + reconcile. Returns (sample_id, workflow_id, wf_pk)."""
    sample_id = f"SRR-disp-{uuid.uuid4().hex[:6]}{sample_suffix}"
    wf_id = workflow_id or f"wf-disp-{uuid.uuid4().hex[:6]}"
    wf_resp = client.post("/api/workflows", json=_wf_payload(workflow_id=wf_id))
    wf_pk = wf_resp.json()["id"]
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    client.post("/api/admin/reconcile-jobs")
    return sample_id, wf_id, wf_pk


def _purge_test_dispatch_state(db_url: str, wf_ids: list[str], sample_ids: list[str]) -> None:
    """Delete jobs / workflow_runs / samples / workflows so subsequent
    reconcile-jobs calls don't see this test's leftovers.

    Without this, samples accumulate in the shared testcontainers DB and
    every subsequent reconcile cross-products them × the next test's
    fresh workflow. The dispatcher's ``LIMIT N`` then returns N rows out
    of a pile of rows-with-tied-created_at and the test's expected
    sample may not be in the slice.

    Uses Core ``in_()`` constructs so SQLAlchemy/asyncpg type the array
    bind correctly — raw ``ANY(:s::text[])`` confuses the parameter
    parser. The dead_letter delete covers both sample_ids and
    workflow_ids so the FK on dead_letter.job_id can't block the
    subsequent ``DELETE FROM jobs``.
    """
    from nextflow_telemetry.db import (
        dead_letter_tbl,
        jobs_tbl,
        samples_tbl,
        telemetry_tbl,
        workflow_runs_tbl,
        workflows_tbl,
    )
    from sqlalchemy import delete, or_

    stmts = [
        delete(dead_letter_tbl).where(
            or_(
                dead_letter_tbl.c.sample_id.in_(sample_ids),
                dead_letter_tbl.c.workflow_id.in_(wf_ids),
            )
        ),
        delete(jobs_tbl).where(
            or_(
                jobs_tbl.c.sample_id.in_(sample_ids),
                jobs_tbl.c.workflow_id.in_(wf_ids),
            )
        ),
        delete(workflow_runs_tbl).where(workflow_runs_tbl.c.workflow_id.in_(wf_ids)),
        delete(telemetry_tbl).where(telemetry_tbl.c.sample_id.in_(sample_ids)),
        delete(samples_tbl).where(samples_tbl.c.sample_id.in_(sample_ids)),
        delete(workflows_tbl).where(workflows_tbl.c.workflow_id.in_(wf_ids)),
    ]
    _run(_exec(db_url, *stmts))


def test_dispatch_skips_locked_workflow_and_picks_an_unlocked_one(integration_client, db_url):
    """The #74 contract: a workflow whose rows are entirely locked must NOT
    starve dispatchers that could otherwise have claimed a different one.

    Setup makes wf_a sort before wf_b alphabetically by workflow_id, so
    the pick step's `ORDER BY workflow_id, workflow_version, created_at`
    *would* pick wf_a if it ignored locks. We:

      1. Spawn a background thread that locks every pending wf_a row
         (`SELECT … FOR UPDATE`) in its own async transaction.
      2. Filter the dispatch to `[wf_a, wf_b]` (both candidates) — so
         the dispatcher is forced to choose between them. The naive
         pick (no SKIP LOCKED) would land on wf_a, then claim_q would
         find every wf_a row locked, return 0 rows, and 204.
      3. Assert the response is 200 with workflow_id=wf_b — proving
         the pick step skipped the entirely-locked wf_a and steered
         to the unlocked wf_b.

    Without `SKIP LOCKED` on the pick step (the round-2 fix in this
    PR), this test would 204-and-spin under contention.
    """
    import asyncio
    import threading

    client, _ = integration_client
    # Force alphabetical ordering: wf_a sorts before wf_b. The pick step
    # default ORDER BY would land on wf_a if not for SKIP LOCKED.
    aaa_id = "wf-aaaa-" + uuid.uuid4().hex[:6]
    zzz_id = "wf-zzzz-" + uuid.uuid4().hex[:6]
    sample_a, wf_a, _ = _seed_job(client, workflow_id=aaa_id)
    sample_b, wf_b, _ = _seed_job(client, workflow_id=zzz_id)
    assert wf_a < wf_b, "test setup invariant: wf_a must sort before wf_b"

    locked = threading.Event()
    proceed = threading.Event()

    def hold_wf_a_lock():
        async def go():
            engine = create_async_engine(db_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "SELECT id FROM jobs "
                            "WHERE workflow_id = :w AND status = 'pending' "
                            "FOR UPDATE"
                        ),
                        {"w": wf_a},
                    )
                    locked.set()
                    while not proceed.is_set():
                        await asyncio.sleep(0.05)
            finally:
                await engine.dispose()

        asyncio.run(go())

    holder = threading.Thread(target=hold_wf_a_lock, daemon=True)
    # Outer try/finally guarantees the holder thread is released and the
    # test data is purged even if the lock-acquisition assertion fails or
    # the dispatch call raises — otherwise a stuck daemon thread holding
    # an open transaction would leak into the session-scoped Postgres.
    holder.start()
    try:
        assert locked.wait(timeout=5.0), "holder thread didn't acquire lock"

        # Both workflows in the filter. Pre-fix: pick lands on wf_a
        # (alphabetically first), claim_q finds it all locked, 204.
        # Post-fix: pick SKIP LOCKED skips wf_a, picks wf_b, succeeds.
        resp = client.post(
            "/api/dispatch/batch",
            json={"workflow_id": [wf_a, wf_b], "limit": 100},
        )
    finally:
        proceed.set()
        holder.join(timeout=5.0)
        # Be loud, not silent, if the lock-holder thread didn't exit.
        # A stuck daemon would leak an open transaction/row locks into
        # the session-scoped Postgres and break subsequent tests.
        assert not holder.is_alive(), "lock-holder thread did not exit within 5s"
        _purge_test_dispatch_state(db_url, [wf_a, wf_b], [sample_a, sample_b])

    assert resp.status_code == 200, resp.text
    assert resp.json()["workflow_id"] == wf_b, (
        f"expected dispatcher to pick the unlocked wf_b, got {resp.json()['workflow_id']}"
    )


def test_dispatch_batch_two_disjoint_workflow_filters_each_get_their_own(integration_client, db_url):
    """Each dispatch response is scoped to its requested workflow_id (#74).

    Pre-#74, FOR UPDATE locks could span multiple workflows before being
    narrowed in Python — the pick-then-lock pattern in this PR locks
    only the batch corresponding to the picked (workflow_id, version),
    so disjoint workflow filters land in independent locked sets.

    This test isn't a true concurrency stress test (TestClient runs
    sequentially), but it does verify the contract: requesting workflow A
    then workflow B in succession produces claims whose top-level
    workflow_id matches the request, and the run_names are distinct.
    Reconcile cross-products samples × workflows so both samples have
    jobs for both workflows, which is fine — what matters is each
    response is scoped correctly.
    """
    client, _ = integration_client
    sample_a, wf_a, _ = _seed_job(client)
    sample_b, wf_b, _ = _seed_job(client)
    try:
        a_resp = client.post("/api/dispatch/batch", json={"workflow_id": [wf_a], "limit": 100})
        assert a_resp.status_code == 200, a_resp.text
        a_data = a_resp.json()
        assert a_data["workflow_id"] == wf_a

        b_resp = client.post("/api/dispatch/batch", json={"workflow_id": [wf_b], "limit": 100})
        assert b_resp.status_code == 200, b_resp.text
        b_data = b_resp.json()
        assert b_data["workflow_id"] == wf_b

        assert a_data["run_name"] != b_data["run_name"]
    finally:
        _purge_test_dispatch_state(db_url, [wf_a, wf_b], [sample_a, sample_b])


def test_dispatch_batch_no_pending_returns_204(integration_client):
    client, _ = integration_client
    # Request a workflow_id that has no jobs
    resp = client.post("/api/dispatch/batch", json={"workflow_id": ["no-such-wf"], "limit": 5})
    assert resp.status_code == 204


def test_dispatch_batch_claims_pending_jobs(integration_client, db_url):
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl

    client, _ = integration_client
    sample_id, wf_id, wf_pk = _seed_job(client)

    resp = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "limit": 10})
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

    batch_resp = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "limit": 10})
    assert batch_resp.status_code == 200
    run_name = batch_resp.json()["run_name"]
    sample_ids = [j["sample_id"] for j in batch_resp.json()["jobs"]]

    resp = client.post("/api/dispatch/submitted", json={
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
    # Issue #73: jobs go to `submitted` here, not `running`. The transition to
    # `running` happens later, on the weblog `started` event.
    assert all(r["status"] == "submitted" for r in job_rows)


def test_weblog_started_advances_submitted_jobs_to_running(integration_client, db_url):
    """The full state machine: pending → claimed → submitted → running."""
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl

    client, _ = integration_client
    _, wf_id, _ = _seed_job(client)

    batch_resp = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "limit": 10})
    assert batch_resp.status_code == 200, batch_resp.text
    batch = batch_resp.json()
    run_name = batch["run_name"]
    sample_ids = [j["sample_id"] for j in batch["jobs"]]

    # claimed → submitted
    submitted_resp = client.post("/api/dispatch/submitted", json={
        "run_name": run_name, "executor_job_id": "SLURM_99", "sample_ids": sample_ids,
    })
    assert submitted_resp.status_code == 200, submitted_resp.text

    # submitted → running (via weblog started event)
    run_id = str(uuid.uuid4())
    resp = client.post("/telemetry",
                       json=_weblog_payload(run_id=run_id, run_name=run_name, event="started"))
    assert resp.status_code == 200

    run_rows = _run(_query(db_url, select(workflow_runs_tbl).where(
        workflow_runs_tbl.c.run_name == run_name
    )))
    assert run_rows[0]["status"] == "running"

    job_rows = _run(_query(db_url, select(jobs_tbl).where(jobs_tbl.c.run_name == run_name)))
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
    wf_resp = client.post("/api/workflows", json=_wf_payload(
        workflow_id=wf_id, version=wf_version, max_retries=0,
    ))
    assert wf_resp.status_code == 201
    wf_pk = wf_resp.json()["id"]

    for sid in sample_ids_all:
        client.post("/api/samples", json={"sample_id": sid, "ncbi_accession": "SRR000001"})
    client.post("/api/admin/reconcile-jobs")

    # Dispatch — use a large limit; filter by both workflow_id AND version so
    # only this test's 3 jobs are returned (shared DB has samples from prior tests).
    batch_resp = client.post("/api/dispatch/batch", json={
        "workflow_id": [wf_id],
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
    client.post("/api/dispatch/submitted", json={
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


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

def _run_until_completed(client, run_id, run_name, wf_id, wf_version):
    """Fire started + completed weblog events without any MARK_COMPLETE."""
    client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="started",
        workflow_id=wf_id, workflow_version=wf_version,
    ))
    client.post("/telemetry", json=_weblog_payload(
        run_id=run_id, run_name=run_name, event="completed",
        workflow_id=wf_id, workflow_version=wf_version,
    ))


def test_failed_job_requeued_when_retries_remain(integration_client, db_url):
    """A job with retries left should reset to 'pending' after a failed run."""
    from nextflow_telemetry.db import jobs_tbl, workflow_runs_tbl

    client, _ = integration_client
    wf_id = f"wf-retry-{uuid.uuid4().hex[:6]}"
    wf_version = f"rv-{uuid.uuid4().hex[:6]}"
    sample_id = f"SRR-retry-{uuid.uuid4().hex[:6]}"

    client.post("/api/workflows", json=_wf_payload(
        workflow_id=wf_id, version=wf_version, max_retries=2,
    ))
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    client.post("/api/admin/reconcile-jobs")

    # First run — dispatch, submit, start, complete with no MARK_COMPLETE
    # limit=500: shared DB has many samples from prior tests; ensure our target is included
    b = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "workflow_version": wf_version, "limit": 500})
    assert b.status_code == 200
    run_name = b.json()["run_name"]
    run_id = str(uuid.uuid4())

    client.post("/api/dispatch/submitted", json={"run_name": run_name, "sample_ids": [sample_id]})
    _run_until_completed(client, run_id, run_name, wf_id, wf_version)

    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_id == wf_id,
    )))
    assert len(job_rows) == 1
    assert job_rows[0]["status"] == "pending", "job should be re-enqueued, not failed"
    assert job_rows[0]["retry_count"] == 1
    assert job_rows[0]["run_name"] is None, "run_name cleared so job re-enters dispatch pool"


def test_job_fails_permanently_when_retries_exhausted(integration_client, db_url):
    """A job that exhausts max_retries should end up 'failed' and in the DLQ."""
    from nextflow_telemetry.db import dead_letter_tbl, jobs_tbl

    client, _ = integration_client
    wf_id = f"wf-exhaust-{uuid.uuid4().hex[:6]}"
    wf_version = f"ev-{uuid.uuid4().hex[:6]}"
    sample_id = f"SRR-exhaust-{uuid.uuid4().hex[:6]}"

    client.post("/api/workflows", json=_wf_payload(
        workflow_id=wf_id, version=wf_version, max_retries=1,
    ))
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    client.post("/api/admin/reconcile-jobs")

    # Exhaust 1 retry — two failed runs; large limit so our sample is always dispatched
    for _ in range(2):
        b = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "workflow_version": wf_version, "limit": 500})
        if b.status_code == 204:
            break
        run_name = b.json()["run_name"]
        run_id = str(uuid.uuid4())
        client.post("/api/dispatch/submitted", json={"run_name": run_name, "sample_ids": [sample_id]})
        _run_until_completed(client, run_id, run_name, wf_id, wf_version)

    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_id == wf_id,
    )))
    assert job_rows[0]["status"] == "failed"
    assert job_rows[0]["retry_count"] == 2

    dlq = _run(_query(db_url, select(dead_letter_tbl).where(
        dead_letter_tbl.c.sample_id == sample_id,
        dead_letter_tbl.c.workflow_id == wf_id,
    )))
    assert len(dlq) == 1


def test_retry_then_success(integration_client, db_url):
    """A job that fails once should complete on the second attempt."""
    from nextflow_telemetry.db import dead_letter_tbl, jobs_tbl

    client, _ = integration_client
    wf_id = f"wf-retrysuc-{uuid.uuid4().hex[:6]}"
    wf_version = f"rs-{uuid.uuid4().hex[:6]}"
    sample_id = f"SRR-retrysuc-{uuid.uuid4().hex[:6]}"

    client.post("/api/workflows", json=_wf_payload(
        workflow_id=wf_id, version=wf_version, max_retries=2,
    ))
    client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"})
    client.post("/api/admin/reconcile-jobs")

    # First attempt: fail without MARK_COMPLETE → re-enqueued
    b = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "workflow_version": wf_version, "limit": 500})
    run1_name = b.json()["run_name"]
    run1_id = str(uuid.uuid4())
    client.post("/api/dispatch/submitted", json={"run_name": run1_name, "sample_ids": [sample_id]})
    _run_until_completed(client, run1_id, run1_name, wf_id, wf_version)

    # Second attempt: succeed with MARK_COMPLETE
    b = client.post("/api/dispatch/batch", json={"workflow_id": [wf_id], "workflow_version": wf_version, "limit": 500})
    assert b.status_code == 200, "job should be back in pending for retry"
    run2_name = b.json()["run_name"]
    run2_id = str(uuid.uuid4())
    client.post("/api/dispatch/submitted", json={"run_name": run2_name, "sample_ids": [sample_id]})

    client.post("/telemetry", json=_weblog_payload(
        run_id=run2_id, run_name=run2_name, event="started",
        workflow_id=wf_id, workflow_version=wf_version,
    ))
    client.post("/telemetry", json=_weblog_payload(
        run_id=run2_id, run_name=run2_name, event="process_completed",
        sample_id=sample_id, process_name="MARK_COMPLETE", process_status="COMPLETED",
    ))
    client.post("/telemetry", json=_weblog_payload(
        run_id=run2_id, run_name=run2_name, event="completed",
        workflow_id=wf_id, workflow_version=wf_version,
    ))

    job_rows = _run(_query(db_url, select(jobs_tbl).where(
        jobs_tbl.c.sample_id == sample_id,
        jobs_tbl.c.workflow_id == wf_id,
    )))
    assert job_rows[0]["status"] == "completed"
    assert job_rows[0]["retry_count"] == 1  # incremented on the first failure

    dlq = _run(_query(db_url, select(dead_letter_tbl).where(
        dead_letter_tbl.c.sample_id == sample_id,
    )))
    assert len(dlq) == 0, "no DLQ entry — job eventually succeeded"


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

def test_admin_stats_shape_and_increments(integration_client):
    client, _ = integration_client

    before = client.get("/api/admin/stats")
    assert before.status_code == 200
    pre = before.json()
    for key in ("samples", "workflows", "jobs_by_status", "jobs_by_status_active", "runs_by_status", "dead_letter_unresolved"):
        assert key in pre
    assert isinstance(pre["jobs_by_status"], dict)
    assert isinstance(pre["jobs_by_status_active"], dict)
    assert isinstance(pre["runs_by_status"], dict)

    sample_id = f"SRR-stats-{uuid.uuid4().hex[:6]}"
    wf_id = f"stats-{uuid.uuid4().hex[:6]}"
    assert client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"}).status_code == 201
    assert client.post("/api/workflows", json=_wf_payload(workflow_id=wf_id)).status_code in (200, 201)
    assert client.post("/api/admin/reconcile-jobs").status_code == 200

    after = client.get("/api/admin/stats").json()
    assert after["samples"] >= pre["samples"] + 1
    assert after["workflows"] >= pre["workflows"] + 1
    pending_before = pre["jobs_by_status"].get("pending", 0)
    pending_after = after["jobs_by_status"].get("pending", 0)
    assert pending_after >= pending_before + 1
    # The new workflow is active, so the active-scoped bucket increments too.
    active_pending_before = pre["jobs_by_status_active"].get("pending", 0)
    active_pending_after = after["jobs_by_status_active"].get("pending", 0)
    assert active_pending_after >= active_pending_before + 1


def test_admin_stats_active_bucket_excludes_retired_versions(integration_client):
    """#114/#116: jobs under a retired workflow version count in jobs_by_status
    but NOT in jobs_by_status_active."""
    client, _ = integration_client
    sample_id = f"SRR-retstat-{uuid.uuid4().hex[:6]}"
    wf_id = f"retstat-{uuid.uuid4().hex[:6]}"
    assert client.post("/api/samples", json={"sample_id": sample_id, "ncbi_accession": "SRR000001"}).status_code == 201
    # Active version -> reconcile creates a pending job under it.
    assert client.post("/api/workflows", json=_wf_payload(workflow_id=wf_id, version="1.0.0")).status_code in (200, 201)
    assert client.post("/api/admin/reconcile-jobs").status_code == 200

    baseline = client.get("/api/admin/stats").json()
    all_pending = baseline["jobs_by_status"].get("pending", 0)
    active_pending = baseline["jobs_by_status_active"].get("pending", 0)

    # Retire the version: its pending job stays in `jobs` but must drop out of
    # the active bucket. All-versions bucket is unchanged.
    wf_pk = client.get(f"/api/workflows?status=active").json()
    pk = next(w["id"] for w in wf_pk if w["workflow_id"] == wf_id)
    assert client.patch(f"/api/workflows/{pk}/status", json={"status": "retired"}).status_code == 200

    after = client.get("/api/admin/stats").json()
    assert after["jobs_by_status"].get("pending", 0) == all_pending          # unchanged
    assert after["jobs_by_status_active"].get("pending", 0) == active_pending - 1  # dropped
