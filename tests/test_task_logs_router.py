"""Unit tests for the task_logs router using TestClient with a mock engine."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.task_logs import create_task_logs_router


def _make_mock_engine() -> MagicMock:
    """Return an engine mock that supports async context managers."""
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=conn)
    engine.connect = MagicMock(return_value=conn)
    return engine, conn


def _client_with_mock_row(row_dict: dict) -> TestClient:
    engine, conn = _make_mock_engine()

    mapping = MagicMock()
    mapping.one = MagicMock(return_value=row_dict)
    mapping.all = MagicMock(return_value=[row_dict])

    result = MagicMock()
    result.mappings = MagicMock(return_value=mapping)
    conn.execute = AsyncMock(return_value=result)

    app = FastAPI()
    app.include_router(create_task_logs_router(engine))
    return TestClient(app)


_FAKE_ROW = {
    "id": 1,
    "run_name": "happy-goldfish",
    "task_hash": "ab/1234ef",
    "log_type": "command_sh",
    "content": "#!/bin/bash\necho hello",
    "uploaded_at": "2026-01-01T00:00:00+00:00",
}


def _multipart(run_name: str, task_hash: str, log_type: str, content: str) -> dict:
    return dict(
        data={"run_name": run_name, "task_hash": task_hash, "log_type": log_type},
        files={"content": ("content", content.encode(), "text/plain")},
    )


def test_upload_task_log_returns_201():
    client = _client_with_mock_row(_FAKE_ROW)
    r = client.post("/task-logs", **_multipart(
        "happy-goldfish", "ab/1234ef", "command_sh", "#!/bin/bash\necho hello",
    ))
    assert r.status_code == 201
    body = r.json()
    assert body["log_type"] == "command_sh"
    assert body["task_hash"] == "ab/1234ef"


def test_upload_command_out_is_accepted():
    row = {**_FAKE_ROW, "log_type": "command_out", "content": "kraken2 report on stdout"}
    client = _client_with_mock_row(row)
    r = client.post("/task-logs", **_multipart(
        "happy-goldfish", "ab/1234ef", "command_out", "kraken2 report on stdout",
    ))
    assert r.status_code == 201
    assert r.json()["log_type"] == "command_out"


def test_invalid_log_type_is_rejected():
    engine, _ = _make_mock_engine()
    app = FastAPI()
    app.include_router(create_task_logs_router(engine))
    client = TestClient(app)
    r = client.post("/task-logs", **_multipart("x", "ab/cd", "bad_type", "data"))
    assert r.status_code == 422


def test_content_too_large_is_rejected():
    engine, _ = _make_mock_engine()
    app = FastAPI()
    app.include_router(create_task_logs_router(engine))
    client = TestClient(app)
    r = client.post("/task-logs", **_multipart("x", "ab/cd", "command_sh", "x" * (5 * 1024 * 1024 + 1)))
    assert r.status_code == 413


def test_retrieve_task_logs_returns_response():
    engine, conn = _make_mock_engine()

    mapping = MagicMock()
    mapping.all = MagicMock(return_value=[_FAKE_ROW])
    result = MagicMock()
    result.mappings = MagicMock(return_value=mapping)
    conn.execute = AsyncMock(return_value=result)

    app = FastAPI()
    app.include_router(create_task_logs_router(engine))
    client = TestClient(app)
    r = client.get("/task-logs/happy-goldfish/ab/1234ef")
    assert r.status_code == 200
    data = r.json()
    assert data["run_name"] == "happy-goldfish"
    assert data["task_hash"] == "ab/1234ef"
    assert len(data["logs"]) == 1
    assert data["logs"][0]["log_type"] == "command_sh"
