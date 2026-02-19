from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.process_metrics import create_process_metrics_router


class FakeProcessMetricsService:
    def retries(self, *, window_days=None, min_samples=50, limit=50):
        return {
            "window_days": window_days,
            "summary": {"process_completed_rows": 10},
            "by_attempt": [],
            "by_process": [],
        }

    def resources_by_attempt(self, *, window_days=None, min_samples=50, limit=100):
        return {"window_days": window_days, "rows": [{"process": "kneaddata", "attempt": 1}]}

    def failures(self, *, window_days=None, min_samples=50, limit=50):
        return {"window_days": window_days, "rows": [{"process": "kneaddata", "failed": 2}]}

    def failure_signatures(self, *, window_days=None, limit=100):
        return {"window_days": window_days, "rows": [{"process": "kneaddata", "exit_code": "1"}]}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_process_metrics_router(FakeProcessMetricsService()))
    return TestClient(app)


def test_retries_endpoint_returns_summary_shape():
    with _client() as client:
        response = client.get("/metrics/processes/retries", params={"window_days": 7})

    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 7
    assert body["summary"]["process_completed_rows"] == 10


def test_resources_by_attempt_endpoint_returns_rows():
    with _client() as client:
        response = client.get("/metrics/processes/resources-by-attempt")

    assert response.status_code == 200
    assert response.json()["rows"][0]["process"] == "kneaddata"


def test_failures_endpoint_returns_rows():
    with _client() as client:
        response = client.get("/metrics/processes/failures")

    assert response.status_code == 200
    assert response.json()["rows"][0]["failed"] == 2


def test_failure_signatures_endpoint_returns_rows():
    with _client() as client:
        response = client.get("/metrics/processes/failure-signatures", params={"limit": 25})

    assert response.status_code == 200
    assert response.json()["rows"][0]["exit_code"] == "1"
