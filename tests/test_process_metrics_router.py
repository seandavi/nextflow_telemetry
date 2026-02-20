from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.process_metrics import create_process_metrics_router


class FakeProcessMetricsService:
    async def summary(self, *, window_days=None, min_samples=50, limit=10):
        return {
            "generated_at_utc": "2026-01-01T00:00:00Z",
            "window_days": window_days,
            "cards": {
                "process_completed_rows": 123,
                "distinct_runs": 4,
                "distinct_processes": 2,
                "success_rows": 118,
                "failure_rows": 5,
                "failure_pct": 4.2,
                "retried_rows": 11,
                "retry_pct": 8.9,
                "retry_success_pct": 30.0,
                "latest_process_completed_utc": "2026-01-01T00:00:00Z",
            },
            "event_mix": [{"event": "process_completed", "rows": 123}],
            "top_failures": [
                {"process": "kneaddata", "total_completed": 50, "failed": 3, "failure_pct": 6.0}
            ],
            "top_retries": [
                {
                    "process": "kneaddata",
                    "total_completed": 50,
                    "retried": 8,
                    "retried_pct": 16.0,
                    "retried_success": 2,
                    "retried_failed": 6,
                }
            ],
            "top_failure_exit_codes": [{"exit_code": "1", "failures": 10}],
        }

    async def retries(self, *, window_days=None, min_samples=50, limit=50):
        return {
            "generated_at_utc": "2026-01-01T00:00:00Z",
            "window_days": window_days,
            "summary": {
                "process_completed_rows": 10,
                "retried_rows": 2,
                "retried_pct": 20.0,
                "retry_success_rows": 1,
                "retry_failure_rows": 1,
                "retry_success_pct": 50.0,
            },
            "by_attempt": [{"attempt": 1, "rows": 8, "success": 8, "failed": 0}],
            "by_process": [
                {
                    "process": "kneaddata",
                    "total_completed": 10,
                    "retried": 2,
                    "retried_pct": 20.0,
                    "retried_success": 1,
                    "retried_failed": 1,
                    "max_attempt": 2,
                }
            ],
        }

    async def resources_by_attempt(self, *, window_days=None, min_samples=50, limit=100):
        return {
            "generated_at_utc": "2026-01-01T00:00:00Z",
            "window_days": window_days,
            "rows": [
                {
                    "process": "kneaddata",
                    "attempt": 1,
                    "rows": 10,
                    "success": 9,
                    "failed": 1,
                    "avg_requested_cpus": 8.0,
                    "avg_requested_memory_gb": 32.0,
                    "avg_requested_time_min": 180.0,
                    "avg_pct_cpu": 300.0,
                    "p95_pct_cpu": 500.0,
                    "avg_pct_mem": 3.2,
                    "p95_pct_mem": 4.1,
                    "avg_peak_rss_gb": 10.0,
                    "p95_peak_rss_gb": 12.0,
                    "avg_read_gb": 1.1,
                    "avg_write_gb": 0.9,
                }
            ],
        }

    async def failures(self, *, window_days=None, min_samples=50, limit=50):
        return {
            "generated_at_utc": "2026-01-01T00:00:00Z",
            "window_days": window_days,
            "rows": [
                {
                    "process": "kneaddata",
                    "total_completed": 10,
                    "success": 8,
                    "failed": 2,
                    "failure_pct": 20.0,
                    "modal_failure_exit_code": "1",
                }
            ],
        }

    async def failure_signatures(self, *, window_days=None, limit=100):
        return {
            "generated_at_utc": "2026-01-01T00:00:00Z",
            "window_days": window_days,
            "rows": [{"process": "kneaddata", "exit_code": "1", "failures": 2}],
        }


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


def test_summary_endpoint_returns_dashboard_cards():
    with _client() as client:
        response = client.get("/metrics/processes/summary", params={"window_days": 14, "limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 14
    assert body["cards"]["process_completed_rows"] == 123
    assert body["top_failure_exit_codes"][0]["exit_code"] == "1"


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
