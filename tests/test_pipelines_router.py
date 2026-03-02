from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.pipelines import create_pipelines_router


_PIPELINE = {
    "id": 1,
    "pipeline_id": "nf-core/taxprofiler",
    "repository": "https://github.com/nf-core/taxprofiler",
    "branch": "main",
    "description": "Taxonomic profiling pipeline",
    "default_params": {"db": "kraken2"},
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class FakePipelinesService:
    async def create(self, *, pipeline_id: str, repository: Any = None, branch: Any = "main", description: Any = None, default_params: Any = None) -> dict[str, Any]:
        return {**_PIPELINE, "pipeline_id": pipeline_id, "repository": repository, "branch": branch, "description": description, "default_params": default_params}

    async def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return {"items": [_PIPELINE], "total": 1}

    async def get(self, pipeline_id: str) -> Optional[dict[str, Any]]:
        if pipeline_id == "MISSING":
            return None
        return _PIPELINE

    async def update(self, pipeline_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        if pipeline_id == "MISSING":
            return None
        return {**_PIPELINE, **kwargs}

    async def delete(self, pipeline_id: str) -> bool:
        if pipeline_id == "MISSING":
            return False
        return True


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_pipelines_router(FakePipelinesService()))
    return TestClient(app)


def test_create_pipeline_201():
    with _client() as client:
        resp = client.post("/pipelines", json={"pipeline_id": "my-pipe", "repository": "https://github.com/test"})
    assert resp.status_code == 201
    assert resp.json()["pipeline_id"] == "my-pipe"


def test_list_pipelines():
    with _client() as client:
        resp = client.get("/pipelines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_get_pipeline():
    with _client() as client:
        resp = client.get("/pipelines/nf-core-taxprofiler")
    assert resp.status_code == 200


def test_get_pipeline_404():
    with _client() as client:
        resp = client.get("/pipelines/MISSING")
    assert resp.status_code == 404


def test_update_pipeline():
    with _client() as client:
        resp = client.patch("/pipelines/nf-core-taxprofiler", json={"description": "Updated"})
    assert resp.status_code == 200


def test_delete_pipeline_204():
    with _client() as client:
        resp = client.delete("/pipelines/nf-core-taxprofiler")
    assert resp.status_code == 204


def test_delete_pipeline_404():
    with _client() as client:
        resp = client.delete("/pipelines/MISSING")
    assert resp.status_code == 404
