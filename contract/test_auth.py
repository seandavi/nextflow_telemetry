import httpx
import pytest

from contract.support import Target, run_event

pytestmark = pytest.mark.writes


@pytest.fixture(autouse=True)
def _needs_token(target: Target) -> None:
    if not target.token:
        pytest.skip("auth tests need CONTRACT_TOKEN")


def test_writes_need_bearer(anon: httpx.Client) -> None:
    calls = [
        ("POST", "admin/reconcile-jobs"),
        ("POST", "samples"),
        ("POST", "workflows"),
        ("PATCH", "workflows/1/status"),
        ("PUT", "daemons/heartbeat"),
        ("POST", "dispatch/batch"),
    ]
    for method, path in calls:
        resp = anon.request(method, path, json={})
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code} {resp.text[:200]}"


def test_event_routes_are_open(edge: httpx.Client, ns: str) -> None:
    run = f"{ns}-auth"
    telemetry = edge.post("telemetry", json={"runId": "x", "runName": run, "event": "started", "utcTime": "2026-01-01T00:00:00Z"})
    assert telemetry.status_code != 401
    assert run_event(edge, run, {"type": "heartbeat"}).status_code != 401
    log = edge.post(
        "api/task-logs",
        data={"run_name": run, "task_hash": "ab/cdef12", "log_type": "command_out"},
        files={"content": ("content", b"x", "text/plain")},
    )
    assert log.status_code != 401


def test_reads_are_open(anon: httpx.Client) -> None:
    assert anon.get("admin/stats").status_code == 200
