from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry.routers.samples import create_samples_router


_SAMPLE = {
    "id": 1,
    "sample_id": "SAMP001",
    "srr_accessions": ["SRR123", "SRR456"],
    "metadata_": {"host": "human"},
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class FakeSamplesService:
    async def create(self, *, sample_id: str, srr_accessions: Any = None, metadata_: Any = None) -> dict[str, Any]:
        return {**_SAMPLE, "sample_id": sample_id, "srr_accessions": srr_accessions, "metadata_": metadata_}

    async def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return {"items": [_SAMPLE], "total": 1}

    async def get(self, sample_id: str) -> Optional[dict[str, Any]]:
        if sample_id == "MISSING":
            return None
        return _SAMPLE

    async def update(self, sample_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        if sample_id == "MISSING":
            return None
        return {**_SAMPLE, **kwargs}

    async def delete(self, sample_id: str) -> bool:
        if sample_id == "MISSING":
            return False
        return True


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_samples_router(FakeSamplesService()))
    return TestClient(app)


def test_create_sample_201():
    with _client() as client:
        resp = client.post("/samples", json={"sample_id": "S1", "srr_accessions": ["SRR1"]})
    assert resp.status_code == 201
    assert resp.json()["sample_id"] == "S1"


def test_list_samples():
    with _client() as client:
        resp = client.get("/samples")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_get_sample():
    with _client() as client:
        resp = client.get("/samples/SAMP001")
    assert resp.status_code == 200
    assert resp.json()["sample_id"] == "SAMP001"


def test_get_sample_404():
    with _client() as client:
        resp = client.get("/samples/MISSING")
    assert resp.status_code == 404


def test_update_sample():
    with _client() as client:
        resp = client.patch("/samples/SAMP001", json={"metadata": {"host": "mouse"}})
    assert resp.status_code == 200


def test_delete_sample_204():
    with _client() as client:
        resp = client.delete("/samples/SAMP001")
    assert resp.status_code == 204


def test_delete_sample_404():
    with _client() as client:
        resp = client.delete("/samples/MISSING")
    assert resp.status_code == 404
