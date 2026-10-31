"""Unit tests for FastAPI routes with mocked dependencies."""
from __future__ import annotations

from fastapi.testclient import TestClient


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
