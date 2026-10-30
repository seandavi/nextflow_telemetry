import httpx
import pytest

from contract import schemas
from contract.support import (
    claim,
    eventually,
    job_summary,
    registration,
    run_detail,
    start_run,
    validate,
)

pytestmark = pytest.mark.writes


def test_claim_204_when_paused(api: httpx.Client, ns: str) -> None:
    name = f"{ns}-paused"
    wf, _ = registration(api, name)
    pk = wf["id"]
    validate(schemas.Workflow, api.patch(f"workflows/{pk}/status", json={"status": "paused"}))
    try:
        assert claim(api, name) is None
    finally:
        api.patch(f"workflows/{pk}/status", json={"status": "active"})


def test_claim_comma_joined_workflow_ids(api: httpx.Client, ns: str) -> None:
    name = f"{ns}-comma"
    registration(api, name)
    batch = claim(api, [f"{ns}-nosuch,{name}"])
    assert batch is not None and batch["workflow_id"] == name


def test_claim_limit(api: httpx.Client, ns: str) -> None:
    name = f"{ns}-limit"
    wf, _ = registration(api, name, samples=3)
    batch = claim(api, name, limit=1)
    assert batch is not None and len(batch["jobs"]) == 1
    summary = job_summary(api, wf["id"])
    assert (summary["pending"], summary["claimed"]) == (2, 1)


def test_submitted_unknown_run(api: httpx.Client, ns: str) -> None:
    resp = api.post("dispatch/submitted", json={"run_name": f"{ns}-nosuch"})
    validate(schemas.Detail, resp, 404)


def test_close_run(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-close"
    registration(api, name)
    run = start_run(api, edge, name)["run_name"]

    closed = validate(schemas.ClosedRun, api.post("admin/close-run", json={"run_name": run}))
    assert closed.already_closed is False
    assert run_detail(api, run)["status"] == "completed"

    again = validate(schemas.ClosedRun, api.post("admin/close-run", json={"run_name": run}))
    assert again.already_closed is True
    assert api.post("admin/close-run", json={"run_name": f"{ns}-nosuch"}).status_code == 404


def test_reset_running(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-reset"
    wf, _ = registration(api, name)
    pk = wf["id"]
    start_run(api, edge, name)
    eventually(lambda: _running(api, pk, 1), lag)

    validate(schemas.ResetRunning, api.post("admin/reset-running", params={"workflow_pk": pk}))
    summary = job_summary(api, pk)
    assert summary["running"] == 0 and summary["pending"] >= 1
    assert api.post("admin/reset-running").status_code == 422


def test_deprecated_noops(api: httpx.Client) -> None:
    validate(schemas.RequeuedExpired, api.post("dispatch/requeue-expired", json={}))
    validate(schemas.HeartbeatWatchdog, api.post("admin/heartbeat-watchdog", json={}))


def _running(api: httpx.Client, pk: int, expected: int) -> None:
    assert job_summary(api, pk)["running"] == expected
