from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.jobs import create_jobs_router


_JOB = {
    "id": 1,
    "sample_id": "SAMP001",
    "pipeline_id": "nf-core/taxprofiler",
    "status": "pending",
    "submitted_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class FakeJobsService:
    async def create(self, *, sample_id: str, pipeline_id: str) -> dict[str, Any]:
        return {**_JOB, "sample_id": sample_id, "pipeline_id": pipeline_id}

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        sample_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
    ) -> dict[str, Any]:
        items = [_JOB]
        if status is not None:
            items = [j for j in items if j["status"] == status]
        if sample_id is not None:
            items = [j for j in items if j["sample_id"] == sample_id]
        return {"items": items, "total": len(items)}

    async def get(self, job_id: int) -> Optional[dict[str, Any]]:
        if job_id == 999:
            return None
        return _JOB

    async def update_status(self, job_id: int, status: str) -> Optional[dict[str, Any]]:
        if job_id == 999:
            return None
        return {**_JOB, "status": status}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_jobs_router(FakeJobsService()))
    return TestClient(app)


def test_create_job_201():
    with _client() as client:
        resp = client.post("/jobs", json={"sample_id": "S1", "pipeline_id": "P1"})
    assert resp.status_code == 201
    assert resp.json()["sample_id"] == "S1"
    assert resp.json()["pipeline_id"] == "P1"


def test_list_jobs():
    with _client() as client:
        resp = client.get("/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1


def test_list_jobs_filter_status():
    with _client() as client:
        resp = client.get("/jobs", params={"status": "pending"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_list_jobs_filter_status_no_match():
    with _client() as client:
        resp = client.get("/jobs", params={"status": "running"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_list_jobs_filter_sample_id():
    with _client() as client:
        resp = client.get("/jobs", params={"sample_id": "SAMP001"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_get_job():
    with _client() as client:
        resp = client.get("/jobs/1")
    assert resp.status_code == 200
    assert resp.json()["id"] == 1


def test_get_job_404():
    with _client() as client:
        resp = client.get("/jobs/999")
    assert resp.status_code == 404


def test_update_job_status():
    with _client() as client:
        resp = client.patch("/jobs/1", json={"status": "running"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def test_update_job_status_404():
    with _client() as client:
        resp = client.patch("/jobs/999", json={"status": "running"})
    assert resp.status_code == 404


def test_update_job_invalid_status():
    with _client() as client:
        resp = client.patch("/jobs/1", json={"status": "invalid"})
    assert resp.status_code == 422
