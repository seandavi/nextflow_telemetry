import httpx
import pytest

from contract import schemas
from contract.support import claim, eventually, registration, run_detail, run_event, submitted, validate

pytestmark = pytest.mark.writes


def _submitted_run(api: httpx.Client, name: str) -> str:
    registration(api, name)
    batch = claim(api, name)
    assert batch is not None
    submitted(api, batch["run_name"], [j["sample_id"] for j in batch["jobs"]])
    return str(batch["run_name"])


def test_wrapper_lifecycle(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    run = _submitted_run(api, f"{ns}-wrapper")

    for event_type in ("wrapper_started", "heartbeat"):
        accepted = validate(schemas.RunEventAccepted, run_event(edge, run, {"type": event_type}), 201)
        assert accepted.type == event_type
        assert not accepted.nextflow_log_uploaded and not accepted.wrapper_output_log_uploaded

    files = {
        "nextflow_log": ("nextflow.log", b"nf says hi", "text/plain"),
        "wrapper_output_log": ("wrapper_output.log", b"OOM", "text/plain"),
    }
    exited = validate(schemas.RunEventAccepted, run_event(edge, run, {"type": "wrapper_exited", "exit_code": 0}, files), 201)
    assert exited.nextflow_log_uploaded and exited.wrapper_output_log_uploaded

    def done() -> None:
        detail = run_detail(api, run)
        assert detail["status"] == "completed"
        assert detail["nextflow_log_available"] and detail["wrapper_output_log_available"]

    eventually(done, lag)

    logs = validate(schemas.TaskLogs, api.get(f"task-logs/{run}/nextflow_log"))
    assert [(log.log_type, log.content) for log in logs.logs] == [
        ("nextflow_log", "nf says hi"),
        ("wrapper_output_log", "OOM"),
    ]


def test_wrapper_exit_nonzero(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    run = _submitted_run(api, f"{ns}-wrapperfail")
    validate(schemas.RunEventAccepted, run_event(edge, run, {"type": "wrapper_exited", "exit_code": 1}), 201)

    def failed() -> None:
        detail = run_detail(api, run)
        assert detail["status"] == "failed"
        assert detail["slurm_reason"] == "wrapper exited 1"

    eventually(failed, lag)


def test_wrapper_rejects(edge: httpx.Client, ns: str) -> None:
    run = f"{ns}-nosuch"
    bad_json = edge.post(f"api/runs/{run}/event", data={"event": "not json"})
    validate(schemas.Detail, bad_json, 422)
    validate(schemas.Detail, run_event(edge, run, {}), 422)

    log = {"nextflow_log": ("nextflow.log", b"x", "text/plain")}
    validate(schemas.Detail, run_event(edge, run, {"type": "heartbeat"}, log), 422)
    validate(schemas.Detail, run_event(edge, run, {"type": "wrapper_exited", "exit_code": 0}, log), 404)
