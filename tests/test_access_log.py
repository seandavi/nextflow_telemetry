"""Unit tests for the structured access-log middleware (#19).

Verifies:
  - one ``http.request`` log record per request, with method/path/status/duration
  - /health logs at DEBUG (Docker healthcheck noise floor)
  - other endpoints log at INFO
  - JSONFormatter produces valid JSON with caller-supplied extras as top-level fields
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from nextflow_telemetry.log import JSONFormatter


class _FakeConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, *_):
        return None


class _HealthyEngine:
    def connect(self):
        return _FakeConnection()


def test_access_log_emits_one_record_per_request(app_module, monkeypatch, caplog):
    monkeypatch.setattr(app_module, "engine", _HealthyEngine())
    with caplog.at_level(logging.DEBUG, logger="nextflow_telemetry"):
        with TestClient(app_module.app) as client:
            response = client.get("/health", headers={"user-agent": "pytest/access-log"})

    assert response.status_code == 200
    records = [r for r in caplog.records if r.getMessage() == "http.request"]
    assert len(records) == 1
    rec = records[0]
    assert rec.method == "GET"
    assert rec.path == "/health"
    assert rec.status == 200
    assert isinstance(rec.duration_ms, float)
    assert rec.user_agent == "pytest/access-log"


def test_access_log_health_is_debug_level(app_module, monkeypatch, caplog):
    """/health emits at DEBUG so the healthcheck doesn't drown INFO streams."""
    monkeypatch.setattr(app_module, "engine", _HealthyEngine())
    with caplog.at_level(logging.DEBUG, logger="nextflow_telemetry"):
        with TestClient(app_module.app) as client:
            client.get("/health")
    health_records = [
        r for r in caplog.records
        if r.getMessage() == "http.request" and getattr(r, "path", None) == "/health"
    ]
    assert health_records, "expected at least one /health access-log record"
    assert all(r.levelno == logging.DEBUG for r in health_records)


def test_access_log_non_health_is_info_level(app_module, monkeypatch, caplog):
    """Any path other than /health logs at INFO by default."""
    monkeypatch.setattr(app_module, "engine", _HealthyEngine())
    with caplog.at_level(logging.INFO, logger="nextflow_telemetry"):
        with TestClient(app_module.app) as client:
            # /openapi.json is built-in; doesn't require routers
            client.get("/openapi.json")
    records = [
        r for r in caplog.records
        if r.getMessage() == "http.request" and getattr(r, "path", None) == "/openapi.json"
    ]
    assert records, "expected at least one /openapi.json access-log record"
    assert all(r.levelno == logging.INFO for r in records)


def test_access_log_records_error_on_unhandled_exception(app_module, monkeypatch, caplog):
    """An exception inside a route surfaces as an ERROR-level access-log record."""
    # Force /health to blow up so the middleware sees the exception path.
    class _CrashingEngine:
        def connect(self):
            raise RuntimeError("simulated db meltdown")

    monkeypatch.setattr(app_module, "engine", _CrashingEngine())

    with caplog.at_level(logging.ERROR, logger="nextflow_telemetry"):
        with TestClient(app_module.app) as client:
            # /health catches the engine error itself and returns 503,
            # so we hit a path that lets the exception propagate to the
            # middleware. The /openapi.json call is fine; the engine
            # crash here is just to set up the unit-test scenario.
            # Easier: just trigger the middleware error path with a route
            # that raises. Use a small monkeypatched route.
            from fastapi import APIRouter

            crashy = APIRouter()

            @crashy.get("/__crash__")
            def _crash():
                raise RuntimeError("boom from route")

            app_module.app.include_router(crashy)
            with pytest.raises(RuntimeError):
                client.get("/__crash__")

    records = [
        r for r in caplog.records
        if r.getMessage() == "http.request" and getattr(r, "path", None) == "/__crash__"
    ]
    assert records, "expected one access-log record for the crashing route"
    rec = records[0]
    assert rec.levelno == logging.ERROR
    assert rec.status == 500
    assert "boom from route" in getattr(rec, "error", "")


# ---------------------------------------------------------------------------
# JSONFormatter unit tests — independent of FastAPI
# ---------------------------------------------------------------------------

def test_json_formatter_emits_valid_json_with_extras() -> None:
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="nextflow_telemetry",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    # `extra=…` on a real logger ends up as attributes on the record:
    record.method = "GET"
    record.path = "/some/path"
    record.status = 200

    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "nextflow_telemetry"
    assert payload["method"] == "GET"
    assert payload["path"] == "/some/path"
    assert payload["status"] == 200
    assert "ts" in payload


def test_json_formatter_handles_non_serializable_via_str() -> None:
    """`default=str` keeps the formatter from raising on weird extras."""
    class Weird:
        def __str__(self) -> str:
            return "WEIRD"

    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="nextflow_telemetry",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    record.weird = Weird()
    payload = json.loads(formatter.format(record))
    assert payload["weird"] == "WEIRD"
