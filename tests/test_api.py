"""Unit tests for FastAPI routes with mocked dependencies."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nextflow_telemetry.services.telemetry import _parse_tag


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("SRR123", "SRR123"),            # bare ${meta.sample} — what Nextflow emits
        ("SRR123:run-abc", "SRR123"),    # historical sample:run form still works
        (None, None),
        ("", None),
    ],
)
def test_parse_tag_handles_bare_and_colon_forms(tag, expected):
    # Regression: a bare sample tag must yield the sample id, not None — else
    # MARK_COMPLETE never sets sample_id and jobs never reach `completed`.
    assert _parse_tag(tag) == expected


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


def test_telemetry_happy_path_calls_ingest(app_module, monkeypatch):
    """POST /telemetry should delegate to TelemetryService.ingest."""
    ingested = {}

    async def fake_ingest(event):
        ingested["event"] = event

    monkeypatch.setattr(app_module.telemetry_service, "ingest", fake_ingest)

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
    assert ingested["event"].run_id == "test123"
    assert ingested["event"].event == "test_event"
