import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_module(monkeypatch):
    monkeypatch.setenv("TELEMETRY_SKIP_DB_INIT", "1")
    if "nextflow_telemetry.main" in sys.modules:
        module = importlib.reload(sys.modules["nextflow_telemetry.main"])
    else:
        module = importlib.import_module("nextflow_telemetry.main")
    return module


def test_health_returns_healthy_when_database_is_available(app_module, monkeypatch):
    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _statement):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(app_module, "engine", FakeEngine())

    with TestClient(app_module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "Healthy"


def test_health_returns_503_when_database_is_unavailable(app_module, monkeypatch):
    class FailingEngine:
        def connect(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(app_module, "engine", FailingEngine())

    with TestClient(app_module.app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "Unhealthy"


def test_telemetry_happy_path_executes_insert(app_module, monkeypatch):
    captured = {}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, statement):
            captured["statement"] = statement
            return None

    class FakeEngine:
        def begin(self):
            return FakeTransaction()

    monkeypatch.setattr(app_module, "engine", FakeEngine())

    payload = {
        "runId": "test123",
        "runName": "test_run",
        "event": "test_event",
        "utcTime": "2024-01-01T00:00:00",
        "metadata": {},
        "trace": {},
    }

    with TestClient(app_module.app) as client:
        response = client.post("/telemetry", json=payload)

    assert response.status_code == 200
    assert response.json()["runId"] == "test123"
    statement = captured["statement"]
    assert statement.compile().params["run_id"] == "test123"
    assert statement.compile().params["event"] == "test_event"
