"""services/registry.py (#373): registration, bulk loading, workflows, job summary and paging."""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nextflow_telemetry import readset
from nextflow_telemetry.control_db import collections_tbl, dead_letter_tbl, jobs_tbl, readset_runs_tbl, samples_tbl
from nextflow_telemetry.control_models import RegisterWorkflowRequest, SampleRegistration
from nextflow_telemetry.services import registry, transitions

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def engine(db_asyncpg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_asyncpg_url)
    yield engine
    await engine.dispose()


def sample(sample_id: str, accession: str, **fields: object) -> SampleRegistration:
    return SampleRegistration(sample_id=sample_id, ncbi_accession=accession, **fields)  # type: ignore[arg-type]


async def register(engine: AsyncEngine, *rows: SampleRegistration) -> registry.RegistrationResult:
    async with engine.begin() as conn:
        return await registry.register_samples(conn, list(rows), now=NOW)


async def count(engine: AsyncEngine, table: object) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()  # type: ignore[arg-type]


async def test_register_samples_single_and_bulk_same_path(engine: AsyncEngine) -> None:
    one = await register(engine, sample("a", "SRR1", collection="PRJNA1"))
    assert (one.received, one.samples_inserted, one.samples_updated, one.collections_created) == (1, 1, 0, 1)
    many = await register(engine, sample("a", "SRR1", collection="PRJNA1"), sample("b", "SRR2", collection="PRJNA1"))
    assert (many.received, many.samples_inserted, many.samples_updated) == (2, 1, 1)
    assert (many.collections_created, many.memberships_added, many.readset_runs_added) == (0, 1, 1)
    async with engine.connect() as conn:
        got = await registry.get_sample(conn, "a")
    assert got is not None and got["collections"] == ["PRJNA1"] and got["readset_id"] == readset.readset_id_for_runs("SRR1")


async def test_registration_writes_readset_runs_and_membership(engine: AsyncEngine) -> None:
    await register(engine, sample("m", "SRR2, SRR1;SRR1", collection="c1"), sample("n", "SRR3;Not_applicable"))
    async with engine.connect() as conn:
        rs = readset.readset_id_for_runs("SRR1;SRR2")
        by_run = await registry.get_sample_by_run(conn, "SRR2")
        assert by_run is not None and by_run["sample_id"] == "m" and by_run["ncbi_accession"] == "SRR1;SRR2"
        assert await registry.get_sample_by_run(conn, "SRR3") is None
        assert (await registry.get_sample(conn, rs or ""))["sample_id"] == "m"  # type: ignore[index]
        runs = (await conn.execute(select(readset_runs_tbl.c.run_accession, readset_runs_tbl.c.readset_id))).all()
    assert sorted(runs) == [("SRR1", rs), ("SRR2", rs)]


async def test_collection_source_rule(engine: AsyncEngine) -> None:
    await register(engine, *(sample(f"s{i}", f"SRR{i}", collection=c) for i, c in enumerate(["PRJNA9", "SRP123", "ERP1", "my-set"], 1)))
    async with engine.connect() as conn:
        sources = dict((await conn.execute(select(collections_tbl.c.collection_id, collections_tbl.c.source))).all())
    assert sources == {"PRJNA9": "bioproject", "SRP123": "sra_study", "ERP1": "sra_study", "my-set": "manual"}


async def test_readset_conflict_rolls_back(engine: AsyncEngine) -> None:
    await register(engine, sample("first", "SRR1;SRR2"))
    with pytest.raises(registry.ReadsetConflict) as err:
        await register(engine, sample("fresh", "SRR9"), sample("clash", "SRR2;SRR3"))
    assert err.value.runs == ["SRR2"]
    assert await count(engine, samples_tbl) == 1

    with pytest.raises(registry.ReadsetConflict) as within:
        await register(engine, sample("x", "SRR7;SRR8"), sample("y", "SRR8;SRR6"))
    assert within.value.runs == ["SRR8"]
    assert await count(engine, samples_tbl) == 1


async def test_bulk_last_line_wins_for_duplicate_sample_id(engine: AsyncEngine) -> None:
    result = await register(
        engine,
        sample("dup", "SRR1", biosample_id="old", collection="c-old"),
        sample("dup", "SRR2", biosample_id="new", metadata={"k": 1}, collection="c-new"),
    )
    assert (result.received, result.samples_inserted) == (2, 1)
    async with engine.connect() as conn:
        got = await registry.get_sample(conn, "dup")
    assert got is not None
    assert (got["ncbi_accession"], got["biosample_id"], got["metadata"]) == ("SRR2", "new", {"k": 1})
    assert got["collections"] == ["c-new", "c-old"]


async def test_reregistration_replaces_fields(engine: AsyncEngine) -> None:
    await register(engine, sample("r", "SRR1", biosample_id="bio", metadata={"a": 1}))
    await register(engine, sample("r", "SRR1"))
    async with engine.connect() as conn:
        got = await registry.get_sample(conn, "r")
        assert got is not None and (got["biosample_id"], got["metadata"]) == (None, {})
        assert [s["sample_id"] for s in await registry.samples_by_biosample(conn, "bio")] == []


def test_parse_bulk_ndjson_and_tsv() -> None:
    ndjson = b'{"sample_id": "a", "ncbi_accession": "SRR1", "metadata": {"x": 1}}\n\n{"sample_id": "b", "ncbi_accession": "SRR2", "collection": "own"}\n'
    rows = registry.parse_bulk(ndjson, "application/x-ndjson", "dflt")
    assert [(r.sample_id, r.collection, r.metadata) for r in rows] == [("a", "dflt", {"x": 1}), ("b", "own", None)]

    tsv = b"sample_id\tncbi_accession\tbiosample_id\tcollection\nc\tSRR3\t\t\nd\tSRR4\tSAMN1\tcol\n"
    rows = registry.parse_bulk(tsv, "text/tab-separated-values", None)
    assert [(r.sample_id, r.biosample_id, r.collection) for r in rows] == [("c", None, None), ("d", "SAMN1", "col")]


def test_parse_bulk_errors_name_lines() -> None:
    body = b'{"sample_id": "a", "ncbi_accession": "SRR1"}\nnot json\n{"sample_id": "", "ncbi_accession": "SRR2"}\n[1]\n'
    with pytest.raises(registry.BulkParseError) as err:
        registry.parse_bulk(body, "application/x-ndjson", None)
    message = str(err.value)
    assert "line 2: invalid JSON" in message and "line 3: sample_id" in message and "line 4: expected a JSON object" in message

    with pytest.raises(registry.BulkParseError, match="header"):
        registry.parse_bulk(b"sample_id\tother\nx\ty\n", "text/tab-separated-values", None)
    with pytest.raises(registry.BulkParseError, match="UTF-8"):
        registry.parse_bulk(b"\xff\xfe", "application/x-ndjson", None)

    many = b"".join(b'{"sample_id": "", "ncbi_accession": "x"}\n' for _ in range(40))
    with pytest.raises(registry.BulkParseError) as capped:
        registry.parse_bulk(many, "application/x-ndjson", None)
    assert len(str(capped.value).split("; ")) == registry.BULK_MAX_ERRORS

    with pytest.raises(registry.BulkTooLarge):
        registry.parse_bulk(b'{"a": 1}\n' * (registry.BULK_MAX_ROWS + 1), "application/x-ndjson", None)


async def test_bulk_ten_thousand_rows(engine: AsyncEngine) -> None:
    rows = [sample(f"bulk{i}", f"SRR{5_000_000 + i}", metadata={"i": i}, collection="PRJNA10K") for i in range(10_000)]
    started = time.monotonic()
    result = await register(engine, *rows)
    assert time.monotonic() - started < 30
    assert (result.samples_inserted, result.memberships_added, result.readset_runs_added) == (10_000, 10_000, 10_000)
    assert await count(engine, samples_tbl) == 10_000


def workflow(workflow_id: str, **fields: object) -> RegisterWorkflowRequest:
    return RegisterWorkflowRequest(workflow_id=workflow_id, version="1.0.0", repository_url="u", revision="main", **fields)  # type: ignore[arg-type]


async def test_workflow_bundle_conflict_and_reordered_params(engine: AsyncEngine) -> None:
    params = {"humann_bundle": "x", "skip_humann": False}
    async with engine.begin() as conn:
        first, conflict = await registry.register_workflow(conn, workflow("wf", params=params), now=NOW)
        assert not conflict and first["status"] == "active" and first["sample_key"] == "readset_id" and first["max_retries"] == 3
        again, conflict = await registry.register_workflow(
            conn, workflow("wf", params={"skip_humann": False, "humann_bundle": "x"}, max_retries=5), now=NOW
        )
        assert not conflict and again["id"] == first["id"] and again["max_retries"] == 5
        clash, conflict = await registry.register_workflow(conn, workflow("wf", params={**params, "skip_humann": True}), now=NOW)
        assert conflict and clash["params"] == params and clash["max_retries"] == 5
        _, conflict = await registry.register_workflow(conn, workflow("wf", params={"skip_humann": 0, "humann_bundle": "x"}), now=NOW)
        assert conflict


async def test_collections_omitted_keeps_scope_null_widens(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        first, _ = await registry.register_workflow(conn, workflow("scope", collections=["c1"]), now=NOW)
        assert first["collections"] == ["c1"]
        kept, _ = await registry.register_workflow(conn, workflow("scope"), now=NOW)
        assert kept["collections"] == ["c1"]
        widened, _ = await registry.register_workflow(conn, workflow("scope", collections=None), now=NOW)
        assert widened["collections"] is None
        ignored, _ = await registry.register_workflow(conn, workflow("other", status="retired"), now=NOW)
        assert ignored["status"] == "active"


async def test_retire_purges_pending_jobs_and_their_dead_letters(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        wf, _ = await registry.register_workflow(conn, workflow("retire", collections=["rc"], max_retries=0), now=NOW)
        await registry.register_samples(conn, [sample("r1", "SRR1", collection="rc"), sample("r2", "SRR2", collection="rc")], now=NOW)
        await transitions.reconcile_jobs(conn)
        batch = await transitions.claim_batch(conn, limit=1, workflow_ids=None, workflow_version=None, now=NOW)
        assert batch is not None
        await transitions.close_run(conn, batch["run_name"], "failed", None, now=NOW)
        await transitions.requeue_dead_letter(conn, workflow_pk=None, collection=None, job_ids=None, now=NOW)
    async with engine.begin() as conn:
        done = await registry.set_workflow_status(conn, wf["id"], "retired", now=NOW)
    assert done is not None and done["status"] == "retired"
    assert await count(engine, jobs_tbl) == 0 and await count(engine, dead_letter_tbl) == 0
    async with engine.begin() as conn:
        assert await registry.set_workflow_status(conn, 999_999, "active", now=NOW) is None


async def test_job_summary_and_keyset_paging(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        wf, _ = await registry.register_workflow(conn, workflow("page", collections=["pc"]), now=NOW)
        await registry.register_samples(conn, [sample(f"p{i}", f"SRR{i}", collection="pc") for i in range(1, 4)], now=NOW)
        await transitions.reconcile_jobs(conn)
        batch = await transitions.claim_batch(conn, limit=1, workflow_ids=None, workflow_version=None, now=NOW)
        assert batch is not None
        await transitions.mark_submitted(conn, batch["run_name"], None, now=NOW)
        await transitions.mark_running(conn, batch["run_name"], None, now=NOW)
        await transitions.complete_sample(conn, batch["run_name"], batch["jobs"][0]["sample_id"], now=NOW)

    async with engine.connect() as conn:
        summary = await registry.job_summary(conn, wf["id"])
        assert summary is not None
        assert (summary["total"], summary["pending"], summary["completed"], summary["dead_letter"], summary["completion_pct"]) == (3, 2, 1, 0, 33.33)
        assert await registry.job_summary(conn, 999_999) is None

        page = await registry.list_jobs(conn, wf["id"], status=None, after=0, limit=2)
        rest = await registry.list_jobs(conn, wf["id"], status=None, after=page[-1]["job_id"], limit=2)
        done = await registry.list_jobs(conn, wf["id"], status="completed", after=0, limit=10)
    assert [j["job_id"] for j in page] == sorted(j["job_id"] for j in page) and len(page) == 2 and len(rest) == 1
    assert rest[0]["job_id"] > page[-1]["job_id"]
    assert len(done) == 1 and done[0]["sample_key"] == done[0]["readset_id"] and done[0]["collections"] == ["pc"]


async def test_list_samples_search_collection_and_facets(engine: AsyncEngine) -> None:
    await register(engine, sample("alpha", "SRR1", collection="c1"), sample("beta", "SRR2", collection="c1"), sample("gamma", "SRR3", collection="c2"))
    async with engine.connect() as conn:
        items, total = await registry.list_samples(conn, limit=10, offset=0, search="ALP", collection=None)
        assert ([s["sample_id"] for s in items], total) == (["alpha"], 1)
        items, total = await registry.list_samples(conn, limit=1, offset=1, search=None, collection="c1")
        assert ([s["sample_id"] for s in items], total) == (["beta"], 2)
        facets = await registry.collection_facets(conn)
    assert facets == {"total": 3, "collections": [{"collection": "c1", "count": 2}, {"collection": "c2", "count": 1}]}


async def test_json_metadata_round_trips_as_text_free_jsonb(engine: AsyncEngine) -> None:
    await register(engine, sample("j", "SRR1", metadata={"nested": {"a": [1, 2]}, "s": "quote\"d"}))
    async with engine.connect() as conn:
        raw = (await conn.execute(text("select jsonb_typeof(metadata) from samples"))).scalar_one()
        got = await registry.get_sample(conn, "j")
    assert raw == "object" and got is not None and json.dumps(got["metadata"], sort_keys=True) == json.dumps({"nested": {"a": [1, 2]}, "s": 'quote"d'}, sort_keys=True)
