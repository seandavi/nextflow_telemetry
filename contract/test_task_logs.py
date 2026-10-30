import httpx
import pytest

from contract import schemas
from contract.support import validate

pytestmark = pytest.mark.writes


def _upload(edge: httpx.Client, content: bytes = b"boom", **fields: str) -> httpx.Response:
    data = {"task_hash": "ab/cdef1234567890", "log_type": "command_err"} | fields
    return edge.post("api/task-logs", data=data, files={"content": ("content", content, "text/plain")})


def test_upload_and_read(api: httpx.Client, edge: httpx.Client, ns: str) -> None:
    run = f"{ns}-r"
    uploaded = validate(schemas.TaskLogUploaded, _upload(edge, run_name=run), 201)
    assert uploaded.task_hash == "ab/cdef12"

    logs = validate(schemas.TaskLogs, api.get(f"task-logs/{run}/ab/cdef12"))
    assert len(logs.logs) == 1
    assert (logs.logs[0].content, logs.logs[0].log_type) == ("boom", "command_err")


def test_rejects(edge: httpx.Client) -> None:
    assert edge.post("api/task-logs", data={"log_type": "command_err"}).status_code == 400
    assert _upload(edge, run_name="").status_code == 400
    assert _upload(edge, run_name="null").status_code == 400
    assert _upload(edge, run_name="x", log_type="bogus").status_code == 422


def test_strips_nul(api: httpx.Client, edge: httpx.Client, ns: str) -> None:
    run = f"{ns}-nul"
    validate(schemas.TaskLogUploaded, _upload(edge, b"he\x00llo\x00", run_name=run), 201)
    logs = validate(schemas.TaskLogs, api.get(f"task-logs/{run}/ab/cdef12"))
    assert logs.logs[0].content == "hello"
