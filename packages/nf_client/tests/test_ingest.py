"""Tests for sample/collection ingestion (client + the CLI _ingest_rows helper)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from nf_client.cli import _ingest_rows
from nf_client.client import JobClient
from nf_client.config import ClientConfig


@pytest.fixture
def config() -> ClientConfig:
    return ClientConfig(server_url="http://test.local")


@pytest.mark.asyncio
async def test_register_sample_posts_collection_not_cohort(config: ClientConfig):
    with respx.mock(base_url="http://test.local") as mock:
        route = mock.post("/samples").mock(
            return_value=httpx.Response(201, json={"sample_id": "abc", "collections": ["StudyX"]})
        )
        async with JobClient(config) as client:
            await client.register_sample("abc", ncbi_accession="SRR1", collection="StudyX")

    body = json.loads(route.calls.last.request.content)
    assert body == {"sample_id": "abc", "ncbi_accession": "SRR1", "collection": "StudyX"}
    assert "metadata" not in body  # the retired cohort key must not reappear


@pytest.mark.asyncio
async def test_ingest_rows_skips_non_accession_and_falls_back_to_study_name(config: ClientConfig):
    rows = [
        {"ncbi_accession": "SRR100", "study_name": "StudyA"},
        {"ncbi_accession": "Not applicable", "study_name": "StudyA"},  # skipped
        {"ncbi_accession": "SRR200;SRR201", "study_name": "StudyB"},
    ]
    with respx.mock(base_url="http://test.local") as mock:
        route = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection=None, limit=None)

    assert result == {"registered": 2, "skipped": 1}
    bodies = [json.loads(c.request.content) for c in route.calls]
    # collection falls back to each row's study_name when --collection is not given
    assert bodies[0]["collection"] == "StudyA"
    assert bodies[1]["collection"] == "StudyB"


@pytest.mark.asyncio
async def test_ingest_rows_explicit_collection_and_limit(config: ClientConfig):
    rows = [{"ncbi_accession": f"SRR{i}", "study_name": "Ignored"} for i in range(5)]
    with respx.mock(base_url="http://test.local") as mock:
        route = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection="Fixed", limit=3)

    assert result == {"registered": 3, "skipped": 0}
    assert all(json.loads(c.request.content)["collection"] == "Fixed" for c in route.calls)
