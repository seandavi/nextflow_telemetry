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
        {"ncbi_accession": "SRR200;SRR201", "study_name": "StudyB", "biosample": "SAMN1"},
    ]
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/samples/bulk").mock(return_value=httpx.Response(404))
        route = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection=None, limit=None)

    assert result == {"registered": 2, "skipped": 1, "unattached": 0}
    bodies = [json.loads(c.request.content) for c in route.calls]
    # collection falls back to each row's study_name when --collection is not given
    assert bodies[0]["collection"] == "StudyA"
    assert bodies[1]["collection"] == "StudyB"
    assert "biosample_id" not in bodies[0] and bodies[1]["biosample_id"] == "SAMN1"


@pytest.mark.asyncio
async def test_ingest_rows_explicit_collection_and_limit(config: ClientConfig):
    rows = [{"ncbi_accession": f"SRR{i}", "study_name": "Ignored"} for i in range(5)]
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/samples/bulk").mock(return_value=httpx.Response(404))
        route = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection="Fixed", limit=3)

    assert result == {"registered": 3, "skipped": 0, "unattached": 0}
    assert all(json.loads(c.request.content)["collection"] == "Fixed" for c in route.calls)


@pytest.mark.asyncio
async def test_ingest_rows_counts_unattached(config: ClientConfig):
    # No --collection and a blank study_name → registered but in no collection.
    rows = [{"ncbi_accession": "SRR1", "study_name": ""}]
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/samples/bulk").mock(return_value=httpx.Response(404))
        route = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection=None, limit=None)

    assert result == {"registered": 1, "skipped": 0, "unattached": 1}
    assert "collection" not in json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
async def test_ingest_rows_uses_bulk_when_supported(config: ClientConfig):
    rows = [
        {"ncbi_accession": "SRR100", "study_name": "StudyA"},
        {"ncbi_accession": "Not applicable", "study_name": "StudyA"},  # skipped
        {"ncbi_accession": "SRR200;SRR201", "study_name": "StudyB", "biosample": "SAMN1"},
        {"ncbi_accession": "SRR300", "study_name": ""},  # no collection
    ]
    with respx.mock(base_url="http://test.local", assert_all_called=False) as mock:
        bulk = mock.post("/samples/bulk").mock(return_value=httpx.Response(200, json={"received": 3}))
        single = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection=None, limit=None)

    assert result == {"registered": 3, "skipped": 1, "unattached": 1}
    assert not single.called and bulk.call_count == 1
    request = bulk.calls.last.request
    assert request.headers["content-type"] == "application/x-ndjson"
    lines = [json.loads(line) for line in request.content.decode().splitlines()]
    assert [set(line) for line in lines] == [
        {"sample_id", "ncbi_accession", "collection"},
        {"sample_id", "ncbi_accession", "collection", "biosample_id"},
        {"sample_id", "ncbi_accession"},
    ]
    assert lines[1]["biosample_id"] == "SAMN1" and lines[1]["ncbi_accession"] == "SRR200;SRR201"


@pytest.mark.asyncio
async def test_ingest_rows_falls_back_on_404(config: ClientConfig):
    rows = [{"ncbi_accession": "SRR100", "study_name": "StudyA"}, {"ncbi_accession": "SRR200", "study_name": "StudyA"}]
    with respx.mock(base_url="http://test.local") as mock:
        bulk = mock.post("/samples/bulk").mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
        single = mock.post("/samples").mock(return_value=httpx.Response(201, json={}))
        async with JobClient(config) as client:
            result = await _ingest_rows(client, rows, collection=None, limit=None)

    assert result == {"registered": 2, "skipped": 0, "unattached": 0}
    assert bulk.call_count == 1 and single.call_count == 2
    assert [json.loads(c.request.content)["ncbi_accession"] for c in single.calls] == ["SRR100", "SRR200"]


@pytest.mark.asyncio
async def test_bulk_error_is_not_swallowed(config: ClientConfig):
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/samples/bulk").mock(return_value=httpx.Response(422, json={"detail": "line 2: bad"}))
        async with JobClient(config) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _ingest_rows(client, [{"ncbi_accession": "SRR1", "study_name": "S"}], collection=None, limit=None)
