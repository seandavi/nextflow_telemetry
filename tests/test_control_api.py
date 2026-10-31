"""The control-plane HTTP surface (#373) against the testcontainers Postgres and a local blob store."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TOKEN = {"Authorization": "Bearer test-token"}


@pytest.fixture
def control_client(
    db_asyncpg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    (tmp_path / "events").mkdir()
    monkeypatch.setenv("SQLALCHEMY_URI", db_asyncpg_url)
    monkeypatch.setenv("TELEMETRY_SKIP_DB_INIT", "1")
    monkeypatch.setenv("DISPATCH_TOKEN", "test-token")
    monkeypatch.setenv("BLOB_STORE_URL", str(tmp_path / "blobs"))
    monkeypatch.setenv("EVENTS_ARCHIVE_URL", str(tmp_path / "events"))
    monkeypatch.setenv("V2_API_URL", "http://v2.invalid/api")
    for name in list(sys.modules):
        if name.startswith("nextflow_telemetry"):
            del sys.modules[name]
    module = importlib.import_module("nextflow_telemetry.main")
    with TestClient(module.app) as client:
        yield client


def run_sql(url: str, sql: str, **params: Any) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(go())


def workflow(client: TestClient, name: str, **fields: Any) -> dict[str, Any]:
    body = {"workflow_id": name, "version": "1", "repository_url": "u", "revision": "r", "max_retries": 0, "collections": [name]}
    resp = client.post("/api/workflows", json=body | fields, headers=TOKEN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def sample(client: TestClient, name: str, run: str, **fields: Any) -> dict[str, Any]:
    body = {"sample_id": f"{name}-{run}", "ncbi_accession": run, "collection": name} | fields
    resp = client.post("/api/samples", json=body, headers=TOKEN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health_at_both_paths(control_client: TestClient) -> None:
    for path in ("/health", "/api/health"):
        resp = control_client.get(path)
        assert resp.status_code == 200 and resp.json()["status"] == "Healthy"


def test_writes_need_token_events_do_not(control_client: TestClient) -> None:
    writes = [
        ("POST", "/api/samples"), ("POST", "/api/samples/bulk"), ("POST", "/api/workflows"),
        ("PATCH", "/api/workflows/1/status"), ("PATCH", "/api/workflows/1/revision"), ("PUT", "/api/daemons/heartbeat"),
        ("DELETE", "/api/daemons/x"), ("POST", "/api/dispatch/batch"), ("POST", "/api/dispatch/submitted"),
        ("POST", "/api/dispatch/requeue-expired"), ("POST", "/api/admin/reconcile-jobs"),
        ("POST", "/api/admin/reset-running"), ("POST", "/api/admin/close-run"),
        ("POST", "/api/admin/requeue-dead-letter"), ("POST", "/api/admin/expire-stale-runs"),
        ("POST", "/api/admin/heartbeat-watchdog"),
    ]
    for method, path in writes:
        anonymous = control_client.request(method, path, json={})
        wrong = control_client.request(method, path, json={}, headers={"Authorization": "Bearer nope"})
        assert (anonymous.status_code, wrong.status_code) == (401, 401), (method, path)

    assert control_client.post("/telemetry", json={"runName": "x", "event": "started"}).status_code == 200
    event = control_client.post("/api/runs/x/event", data={"event": '{"type": "heartbeat"}'})
    assert event.status_code == 201
    log = control_client.post(
        "/api/task-logs", data={"run_name": "x", "task_hash": "ab/cdef12", "log_type": "command_out"},
        files={"content": ("content", b"x", "text/plain")},
    )
    assert log.status_code == 201
    assert control_client.get("/api/admin/stats").status_code == 200


def test_422_detail_is_a_string(control_client: TestClient) -> None:
    resp = control_client.post("/api/samples", json={"sample_id": "x"}, headers=TOKEN)
    assert resp.status_code == 422 and isinstance(resp.json()["detail"], str) and "ncbi_accession" in resp.json()["detail"]
    bad_status = control_client.patch("/api/workflows/1/status", json={"status": "bogus"}, headers=TOKEN)
    assert bad_status.status_code == 422 and isinstance(bad_status.json()["detail"], str)
    # The token gate runs before body validation.
    assert control_client.post("/api/samples", json={"sample_id": "x"}).status_code == 401


def test_claim_to_completion_over_http(control_client: TestClient) -> None:
    wf = workflow(control_client, "e2e", max_retries=1)
    registered = [sample(control_client, "e2e", f"SRR{n}") for n in (101, 102)]
    assert control_client.post("/api/admin/reconcile-jobs", headers=TOKEN).json() == {"jobs_created": 2}

    assert control_client.post("/api/dispatch/batch", json={"workflow_id": "nosuch"}, headers=TOKEN).status_code == 204
    resp = control_client.post("/api/dispatch/batch", json={"limit": 5, "workflow_id": "nosuch, e2e"}, headers=TOKEN)
    assert resp.status_code == 200
    batch = resp.json()
    assert batch["workflow_pk"] == wf["id"] and sorted(j["sample_id"] for j in batch["jobs"]) == sorted(s["readset_id"] for s in registered)
    run = batch["run_name"]

    submitted = control_client.post("/api/dispatch/submitted", json={"run_name": run, "executor_job_id": "9"}, headers=TOKEN)
    assert submitted.json() == {"run_name": run, "status": "submitted"}
    assert control_client.post("/api/dispatch/submitted", json={"run_name": run}, headers=TOKEN).status_code == 404

    started = {"runId": "nf", "runName": run, "event": "started"}
    assert control_client.post("/telemetry", json=started).status_code == 200
    done = {"process": "x:MARK_COMPLETE", "status": "COMPLETED", "tag": batch["jobs"][0]["sample_id"], "hash": "aa/bbbbbb"}
    control_client.post("/telemetry", json={"runName": run, "event": "process_completed", "trace": done})
    failed = {"process": "x:ALIGN", "status": "FAILED", "exit": 1, "tag": batch["jobs"][1]["sample_id"], "hash": "cc/dddddd", "attempt": 1}
    control_client.post("/telemetry", json={"runName": run, "event": "process_completed", "trace": failed})

    detail = control_client.get(f"/api/runs/{run}").json()
    assert detail["status"] == "running" and detail["job_status_counts"] == {"completed": 1, "running": 1}
    assert detail["task_status_counts"] == {"COMPLETED": 1, "FAILED": 1} and detail["failed_tasks"][0]["exit_code"] == "1"
    assert detail["classification"] == "active"

    control_client.post("/telemetry", json={"runName": run, "event": "completed", "metadata": {"workflow": {"success": True}}})
    summary = control_client.get(f"/api/workflows/{wf['id']}/job-summary").json()
    assert (summary["completed"], summary["pending"], summary["total"]) == (1, 1, 2)
    listed = control_client.get("/api/runs", params={"status": "completed"}).json()
    assert [r["run_name"] for r in listed["runs"]] == [run] and listed["total"] == 1
    cohort = control_client.get("/api/cohorts/e2e/summary").json()
    assert (cohort["samples_completed"], cohort["completion_pct"], cohort["job_status_counts"]["pending"]) == (1, 50.0, 1)
    assert control_client.get("/api/cohorts/nosuch/summary").status_code == 404
    leaderboard = control_client.get("/api/cohorts/leaderboard").json()
    assert leaderboard[0]["collection_id"] == "e2e" and leaderboard[0]["samples_remaining"] == 1
    stats = control_client.get("/api/admin/stats").json()
    assert stats["samples"] == 2 and stats["jobs_by_status"] == {"completed": 1, "pending": 1}


def test_ill_typed_run_event_values_are_accepted(control_client: TestClient) -> None:
    for event in ({"type": "pre_nextflow", "wait_seconds": 1.5}, {"type": "wrapper_exited", "exit_code": "x"}):
        assert control_client.post("/api/runs/nosuch/event", data={"event": json.dumps(event)}).status_code == 201


def test_bulk_endpoint_content_types_and_limits(control_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = importlib.import_module("nextflow_telemetry.services.registry")
    post = lambda body, content_type, **params: control_client.post(  # noqa: E731
        "/api/samples/bulk", content=body, params=params, headers=TOKEN | {"Content-Type": content_type}
    )
    assert post(b"{}", "application/json").status_code == 415

    ok = post(b'{"sample_id": "a", "ncbi_accession": "SRR1"}\n{"sample_id": "b", "ncbi_accession": "SRR2", "collection": "own"}\n', "application/x-ndjson", collection="dflt")
    assert ok.status_code == 200 and ok.json() == {
        "received": 2, "samples_inserted": 2, "samples_updated": 0, "collections_created": 2, "memberships_added": 2, "readset_runs_added": 2,
    }
    tsv = post(b"sample_id\tncbi_accession\nc\tSRR3\n", "text/tab-separated-values; charset=utf-8")
    assert tsv.status_code == 200 and tsv.json()["samples_inserted"] == 1

    bad = post(b'{"sample_id": "d", "ncbi_accession": "SRR4"}\n{"sample_id": "e"}\n', "application/x-ndjson")
    assert bad.status_code == 422 and "line 2" in bad.json()["detail"]
    clash = post(b'{"sample_id": "f", "ncbi_accession": "SRR5"}\n{"sample_id": "g", "ncbi_accession": "SRR2;SRR6"}\n', "application/x-ndjson")
    assert clash.status_code == 409 and "SRR2" in clash.json()["detail"]
    assert control_client.get("/api/samples").json()["total"] == 3

    monkeypatch.setattr(registry, "BULK_MAX_ROWS", 2)
    many = post(b'{"sample_id": "h", "ncbi_accession": "SRR7"}\n' * 3, "application/x-ndjson")
    assert many.status_code == 413
    single_clash = control_client.post("/api/samples", json={"sample_id": "z", "ncbi_accession": "SRR1;SRR9"}, headers=TOKEN)
    assert single_clash.status_code == 409 and isinstance(single_clash.json()["detail"], str)


def test_run_event_attachments_and_task_logs_round_trip(control_client: TestClient) -> None:
    workflow(control_client, "logs")
    sample(control_client, "logs", "SRR201")
    control_client.post("/api/admin/reconcile-jobs", headers=TOKEN)
    run = control_client.post("/api/dispatch/batch", json={"workflow_id": "logs"}, headers=TOKEN).json()["run_name"]
    control_client.post("/api/dispatch/submitted", json={"run_name": run}, headers=TOKEN)

    files = {"nextflow_log": ("nextflow.log", b"nf says hi", "text/plain"), "wrapper_output_log": ("w.log", b"OOM", "text/plain")}
    event = '{"type": "wrapper_exited", "exit_code": 0}'
    assert control_client.post(f"/api/runs/{run}/event", data={"event": event}, files={"nextflow_log": files["nextflow_log"]}).json()["wrapper_output_log_uploaded"] is False
    resp = control_client.post(f"/api/runs/{run}/event", data={"event": event}, files=files)
    assert resp.status_code == 201 and resp.json()["nextflow_log_uploaded"] and resp.json()["wrapper_output_log_uploaded"]

    detail = control_client.get(f"/api/runs/{run}").json()
    assert (detail["status"], detail["nextflow_log_available"], detail["wrapper_output_log_available"]) == ("completed", True, True)
    assert detail["nextflow_log_uploaded_at"] is not None and detail["wrapper_exit_code"] == 0
    logs = control_client.get(f"/api/task-logs/{run}/nextflow_log").json()["logs"]
    assert [(entry["log_type"], entry["content"]) for entry in logs] == [("nextflow_log", "nf says hi"), ("wrapper_output_log", "OOM")]

    heartbeat = control_client.post(f"/api/runs/{run}/event", data={"event": '{"type": "heartbeat"}'}, files={"nextflow_log": files["nextflow_log"]})
    assert heartbeat.status_code == 422 and "wrapper_exited" in heartbeat.json()["detail"]
    orphan = control_client.post("/api/runs/nosuch/event", data={"event": event}, files={"nextflow_log": files["nextflow_log"]})
    assert orphan.status_code == 404
    assert control_client.post(f"/api/runs/{run}/event", data={"event": "nope"}).status_code == 422
    assert control_client.post(f"/api/runs/{run}/event", data={"event": "{}"}).json()["detail"] == "event.type is required"
    assert control_client.get("/api/runs/nosuch").status_code == 404


def test_task_log_rejects_and_short_hash(control_client: TestClient) -> None:
    def upload(content: bytes = b"he\x00llo", **fields: str) -> Any:
        data = {"run_name": "r1", "task_hash": "ab/cdef1234567890", "log_type": "command_err"} | fields
        return control_client.post("/api/task-logs", data=data, files={"content": ("content", content, "text/plain")})

    done = upload()
    assert done.status_code == 201 and done.json()["task_hash"] == "ab/cdef12" and done.json()["content"] == "hello"
    assert upload(run_name="").status_code == 400 and upload(run_name="null").status_code == 400
    assert upload(log_type="bogus").status_code == 422
    assert control_client.post("/api/task-logs", data={"run_name": "r1", "log_type": "command_out"}).status_code == 422
    assert upload(b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    fetched = control_client.get("/api/task-logs/r1/ab/cdef12").json()
    assert [(e["log_type"], e["content"]) for e in fetched["logs"]] == [("command_err", "hello")]
    assert control_client.get("/api/task-logs/r1/zz/000000").json()["logs"] == []


def test_dead_letter_list_filters(control_client: TestClient) -> None:
    wf_a, wf_b = workflow(control_client, "dl-a"), workflow(control_client, "dl-b")
    sample(control_client, "dl-a", "SRR301")
    sample(control_client, "dl-b", "SRR302")
    control_client.post("/api/admin/reconcile-jobs", headers=TOKEN)
    for name in ("dl-a", "dl-b"):
        run = control_client.post("/api/dispatch/batch", json={"workflow_id": name}, headers=TOKEN).json()["run_name"]
        assert control_client.post("/api/admin/close-run", params={"run_name": run}, headers=TOKEN).json()["swept"] == 1

    everything = control_client.get("/api/admin/dead-letter").json()
    assert everything["total"] == 2 and {i["job_status"] for i in everything["items"]} == {"failed"}
    by_wf = control_client.get("/api/admin/dead-letter", params={"workflow_pk": wf_a["id"]}).json()
    assert [i["workflow_id"] for i in by_wf["items"]] == ["dl-a"]
    by_collection = control_client.get("/api/admin/dead-letter", params={"collection": "dl-b"}).json()
    assert [i["collections"] for i in by_collection["items"]] == [["dl-b"]]

    job_a = by_wf["items"][0]["job_id"]
    requeue = control_client.post("/api/admin/requeue-dead-letter", json={"job_ids": [job_a]}, headers=TOKEN)
    assert requeue.json() == {"requeued": 1}
    assert control_client.get("/api/admin/dead-letter", params={"resolved": "false"}).json()["total"] == 1
    assert control_client.get("/api/admin/dead-letter", params={"resolved": "true"}).json()["items"][0]["job_id"] == job_a
    assert control_client.post("/api/admin/requeue-dead-letter", headers=TOKEN).json() == {"requeued": 1}
    assert control_client.post("/api/admin/requeue-dead-letter", json={}, headers=TOKEN).json() == {"requeued": 0}
    assert wf_b["id"]


def test_heartbeat_restart_resets_started_at_and_prunes(control_client: TestClient, db_asyncpg_url: str) -> None:
    def beat(agent: str, **fields: Any) -> dict[str, Any]:
        body = {"agent_id": agent, "hostname": "h", "workflow_id": "wf", "mode": "slurm", "batch_size": 1} | fields
        resp = control_client.put("/api/daemons/heartbeat", json=body, headers=TOKEN)
        assert resp.status_code == 200, resp.text
        return resp.json()

    first = beat("d1")
    assert beat("d1")["started_at"] == first["started_at"]
    run_sql(db_asyncpg_url, "update daemons set last_seen_at = now() - interval '10 minutes', started_at = now() - interval '1 day'")
    stale = control_client.get("/api/daemons").json()[0]
    assert (stale["status"], stale["is_active"]) == ("stale", False)
    restarted = beat("d1")
    assert restarted["started_at"] > stale["started_at"] and restarted["is_active"]

    stopped = beat("d1", status="stopped")
    assert (stopped["status"], stopped["is_active"]) == ("stopped", False)
    assert control_client.get("/api/daemons", params={"active_only": "true"}).json() == []
    again = beat("d1")
    assert again["started_at"] >= stopped["started_at"] and again["is_active"]

    assert control_client.put("/api/daemons/heartbeat", json={"agent_id": "d1"}, headers=TOKEN).status_code == 422
    bad = control_client.put("/api/daemons/heartbeat", json={"agent_id": "d1", "hostname": "h", "status": "weird"}, headers=TOKEN)
    assert bad.status_code == 422

    run_sql(db_asyncpg_url, "update daemons set last_seen_at = now() - interval '8 days'")
    assert control_client.get("/api/daemons/").json() == []
    assert run_sql(db_asyncpg_url, "select count(*) from daemons") == [(0,)]


def test_dispatchability_ignores_stopped_daemons(control_client: TestClient) -> None:
    workflow(control_client, "disp")
    sample(control_client, "disp", "SRR401")
    control_client.post("/api/admin/reconcile-jobs", headers=TOKEN)
    stuck = control_client.get("/api/admin/dispatchability").json()
    assert (stuck["active_daemons"], stuck["stuck_pending_total"]) == (0, 1)
    assert stuck["stuck"][0]["reason"] == "no active daemon claims this workflow"

    def beat(agent: str, workflow_id: str, status: str = "idle") -> None:
        body = {"agent_id": agent, "hostname": "h", "workflow_id": workflow_id, "status": status}
        assert control_client.put("/api/daemons/heartbeat", json=body, headers=TOKEN).status_code == 200

    beat("other", "someone-else")
    beat("gone", "disp", "stopped")
    assert control_client.get("/api/admin/dispatchability").json()["stuck_pending_total"] == 1
    beat("live", " x , disp ")
    covered = control_client.get("/api/admin/dispatchability").json()
    assert (covered["active_daemons"], covered["stuck_pending_total"]) == (2, 0)
    beat("all", "")
    assert control_client.get("/api/admin/dispatchability").json()["active_daemons"] == 3


def test_workflow_and_sample_lookups_over_http(control_client: TestClient) -> None:
    wf = workflow(control_client, "look", params={"a": 1})
    clash = control_client.post("/api/workflows", json={"workflow_id": "look", "version": "1", "repository_url": "u", "revision": "r", "params": {"a": 2}}, headers=TOKEN)
    assert clash.status_code == 409 and "ADR-0004" in clash.json()["detail"]
    assert control_client.get("/api/workflows/999999").status_code == 404
    assert control_client.get("/api/workflows/999999/jobs").status_code == 404
    assert control_client.get("/api/workflows", params={"status": "paused"}).json() == []
    assert control_client.patch(f"/api/workflows/{wf['id']}/revision", json={"revision": "abc"}, headers=TOKEN).json()["revision"] == "abc"

    made = sample(control_client, "look", "SRR501", biosample_id="SAMN9")
    assert control_client.get(f"/api/samples/{made['readset_id']}").json()["sample_id"] == made["sample_id"]
    assert control_client.get("/api/samples/by-srr/SRR501").json()["sample_id"] == made["sample_id"]
    assert control_client.get("/api/samples/by-srr/SRR50").status_code == 404
    assert [s["sample_id"] for s in control_client.get("/api/samples/by-biosample/SAMN9").json()] == [made["sample_id"]]
    assert control_client.get("/api/samples", params={"limit": 0, "offset": -5}).json()["limit"] == 1
    assert control_client.get("/api/samples/facets/collections").json()["collections"] == [{"collection": "look", "count": 1}]
    assert control_client.post("/api/admin/reset-running", headers=TOKEN).status_code == 422
    assert control_client.post("/api/admin/close-run", json={}, headers=TOKEN).status_code == 422
    assert control_client.post("/api/admin/close-run", json={"run_name": "nosuch"}, headers=TOKEN).status_code == 404
    noop = control_client.post("/api/dispatch/requeue-expired", headers=TOKEN)
    assert noop.json() == {"requeued_runs": 0} and noop.headers["deprecation"] == "true"
