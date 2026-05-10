"""Integration tests for the cohort summary router (issue #36).

Uses the shared testcontainers postgres fixture from conftest.py.

Each test scopes its data with a unique TAG (random hex). The `cohort_data`
fixture cleans up rows tagged with that suffix after the test, so leftover
samples don't poison reconcile-driven tests elsewhere in the suite.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _run(coro):
    return asyncio.run(coro)


async def _exec(db_url: str, *stmts):
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(stmt)
    finally:
        await engine.dispose()


@pytest.fixture()
def cohort_data(db_url):
    """Provides a unique tag and tears down all rows that include it.

    Sample IDs, collection IDs, and workflow IDs created via the test helpers
    must all contain the tag so cleanup is reliable.
    """
    tag = uuid.uuid4().hex[:10]

    yield tag

    pat = f"%{tag}%"
    cleanup = [
        text("DELETE FROM telemetry WHERE sample_id LIKE :p OR run_name LIKE :p OR workflow_id LIKE :p").bindparams(p=pat),
        text("DELETE FROM jobs WHERE sample_id LIKE :p OR workflow_id LIKE :p").bindparams(p=pat),
        text("DELETE FROM collection_samples WHERE collection_id LIKE :p OR sample_id LIKE :p").bindparams(p=pat),
        text("DELETE FROM collections WHERE collection_id LIKE :p").bindparams(p=pat),
        text("DELETE FROM samples WHERE sample_id LIKE :p").bindparams(p=pat),
        text("DELETE FROM workflows WHERE workflow_id LIKE :p").bindparams(p=pat),
    ]
    _run(_exec(db_url, *cleanup))


def _seed_cohort(db_url: str, *, collection_id: str, sample_ids: list[str]) -> None:
    """Insert a collection plus the listed samples and their join rows."""
    from nextflow_telemetry.db import (
        collection_samples_tbl,
        collections_tbl,
        samples_tbl,
    )

    now = _ts()
    stmts = [
        insert(collections_tbl).values(
            collection_id=collection_id,
            source="manual",
            label=f"label-{collection_id}",
            created_at=now,
            updated_at=now,
        )
    ]
    for sid in sample_ids:
        stmts.append(
            insert(samples_tbl).values(
                sample_id=sid,
                ncbi_accession=None,
                created_at=now,
                updated_at=now,
            )
        )
        stmts.append(
            insert(collection_samples_tbl).values(
                collection_id=collection_id, sample_id=sid
            )
        )
    _run(_exec(db_url, *stmts))


def _seed_jobs(db_url: str, sample_id: str, status: str, *, workflow_id: str, workflow_version: str = "1.0.0") -> None:
    from nextflow_telemetry.db import jobs_tbl, workflows_tbl

    now = _ts()
    # workflows row needs to exist for the FK on workflow_pk
    engine = create_async_engine(db_url)

    async def _do():
        async with engine.begin() as conn:
            # upsert workflow
            existing = (await conn.execute(
                select(workflows_tbl).where(
                    workflows_tbl.c.workflow_id == workflow_id,
                    workflows_tbl.c.version == workflow_version,
                )
            )).mappings().first()
            if not existing:
                await conn.execute(
                    insert(workflows_tbl).values(
                        workflow_id=workflow_id,
                        version=workflow_version,
                        repository_url="https://example.com/wf",
                        revision="main",
                        max_retries=3,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
                wf_pk = (await conn.execute(
                    select(workflows_tbl.c.id).where(
                        workflows_tbl.c.workflow_id == workflow_id,
                        workflows_tbl.c.version == workflow_version,
                    )
                )).scalar_one()
            else:
                wf_pk = existing["id"]
            await conn.execute(
                insert(jobs_tbl).values(
                    sample_id=sample_id,
                    workflow_pk=wf_pk,
                    workflow_id=workflow_id,
                    workflow_version=workflow_version,
                    status=status,
                    retry_count=0,
                    created_at=now,
                )
            )
    try:
        _run(_do())
    finally:
        _run(engine.dispose())


def _seed_telemetry_failure(
    db_url: str,
    *,
    sample_id: str,
    process: str,
    workflow_id: str,
    run_name: str = "run-1",
    workflow_version: str = "1.0.0",
    status: str = "FAILED",
    task_hash: str = "ab/cd1234",
    exit_code: str = "1",
) -> None:
    from nextflow_telemetry.db import telemetry_tbl

    _run(_exec(
        db_url,
        insert(telemetry_tbl).values(
            run_id=str(uuid.uuid4()),
            run_name=run_name,
            event="process_completed",
            utc_time=_ts(),
            sample_id=sample_id,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            metadata_=None,
            trace={
                "process": process,
                "status": status,
                "name": f"{process} ({sample_id})",
                "hash": task_hash,
                "exit": exit_code,
                "task_id": "1",
            },
        ),
    ))


# ---------------------------------------------------------------------------
# /cohorts (list)
# ---------------------------------------------------------------------------

def test_list_cohorts_returns_collections_with_sample_counts(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    _seed_cohort(db_url, collection_id=cid, sample_ids=[f"S-{tag}-{i}" for i in range(3)])

    resp = client.get("/api/cohorts")
    assert resp.status_code == 200
    rows = resp.json()
    match = [r for r in rows if r["collection_id"] == cid]
    assert len(match) == 1
    assert match[0]["sample_count"] == 3
    assert match[0]["source"] == "manual"


# ---------------------------------------------------------------------------
# /cohorts/{id}/summary
# ---------------------------------------------------------------------------

def test_summary_404_for_unknown_cohort(integration_client):
    client, _ = integration_client
    resp = client.get("/api/cohorts/does-not-exist/summary")
    assert resp.status_code == 404


def test_summary_counts_jobs_by_status(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    wf = f"wf-{tag}"
    samples = [f"S-{tag}-{i}" for i in range(4)]
    _seed_cohort(db_url, collection_id=cid, sample_ids=samples)

    _seed_jobs(db_url, samples[0], "completed", workflow_id=wf)
    _seed_jobs(db_url, samples[1], "completed", workflow_id=wf)
    _seed_jobs(db_url, samples[2], "failed",    workflow_id=wf)
    _seed_jobs(db_url, samples[3], "pending",   workflow_id=wf)

    resp = client.get(f"/api/cohorts/{cid}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sample_count"] == 4
    assert body["total_jobs"] == 4
    assert body["job_status_counts"]["completed"] == 2
    assert body["job_status_counts"]["failed"] == 1
    assert body["job_status_counts"]["pending"] == 1
    assert body["completion_pct"] == 50.0


def test_summary_zero_jobs_returns_zero_completion(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    _seed_cohort(db_url, collection_id=cid, sample_ids=[f"S-{tag}-0"])

    # Filter to a workflow that doesn't exist so the empty-cohort branch is exercised
    # even if reconcile in another test created jobs against this sample.
    resp = client.get(f"/api/cohorts/{cid}/summary?workflow_id=does-not-exist-{tag}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_jobs"] == 0
    assert body["completion_pct"] == 0.0


def test_summary_failure_by_process_aggregates(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    wf = f"wf-{tag}"
    samples = [f"S-{tag}-{i}" for i in range(3)]
    _seed_cohort(db_url, collection_id=cid, sample_ids=samples)

    _seed_telemetry_failure(db_url, sample_id=samples[0], process="FETCH_READS", workflow_id=wf, run_name=f"run-{tag}-1")
    _seed_telemetry_failure(db_url, sample_id=samples[1], process="FETCH_READS", workflow_id=wf, run_name=f"run-{tag}-2")
    _seed_telemetry_failure(db_url, sample_id=samples[1], process="KNEADDATA",   workflow_id=wf, run_name=f"run-{tag}-3")
    _seed_telemetry_failure(db_url, sample_id=samples[2], process="KNEADDATA",   workflow_id=wf, status="ABORTED", run_name=f"run-{tag}-4")

    resp = client.get(f"/api/cohorts/{cid}/summary?workflow_id={wf}&workflow_version=1.0.0")
    assert resp.status_code == 200
    body = resp.json()
    by_proc = {row["process"]: row for row in body["failure_by_process"]}
    assert by_proc["FETCH_READS"]["failed_count"] == 2
    assert by_proc["FETCH_READS"]["sample_count"] == 2
    assert by_proc["KNEADDATA"]["failed_count"] == 2  # FAILED + ABORTED both count
    assert by_proc["KNEADDATA"]["sample_count"] == 2


def test_summary_workflow_filter_excludes_other_workflows(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    wfA = f"wfA-{tag}"
    wfB = f"wfB-{tag}"
    samples = [f"S-{tag}-{i}" for i in range(2)]
    _seed_cohort(db_url, collection_id=cid, sample_ids=samples)

    _seed_jobs(db_url, samples[0], "completed", workflow_id=wfA)
    _seed_jobs(db_url, samples[1], "failed",    workflow_id=wfB)

    resp = client.get(f"/api/cohorts/{cid}/summary?workflow_id={wfA}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_jobs"] == 1
    assert body["job_status_counts"]["completed"] == 1


def test_summary_excludes_samples_not_in_cohort(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    wf = f"wf-{tag}"
    sid_in = f"S-{tag}-in"
    sid_out = f"S-{tag}-out"
    _seed_cohort(db_url, collection_id=cid, sample_ids=[sid_in])

    # sid_out has a job and a failure but is NOT in the cohort
    from nextflow_telemetry.db import samples_tbl
    _run(_exec(
        db_url,
        insert(samples_tbl).values(sample_id=sid_out, created_at=_ts(), updated_at=_ts()),
    ))
    _seed_jobs(db_url, sid_in, "completed", workflow_id=wf)
    _seed_jobs(db_url, sid_out, "failed",   workflow_id=wf)
    _seed_telemetry_failure(db_url, sample_id=sid_out, process="KNEADDATA", workflow_id=wf, run_name=f"run-{tag}")

    resp = client.get(f"/api/cohorts/{cid}/summary?workflow_id={wf}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_jobs"] == 1
    assert body["job_status_counts"]["completed"] == 1
    assert body["failure_by_process"] == []


# ---------------------------------------------------------------------------
# /cohorts/{id}/failures (drill-down)
# ---------------------------------------------------------------------------

def test_failures_returns_per_task_rows_with_task_hash(integration_client, db_url, cohort_data):
    client, _ = integration_client
    tag = cohort_data
    cid = f"COHORT-{tag}"
    wf = f"wf-{tag}"
    samples = [f"S-{tag}-{i}" for i in range(2)]
    _seed_cohort(db_url, collection_id=cid, sample_ids=samples)

    _seed_telemetry_failure(db_url, sample_id=samples[0], process="FETCH_READS", workflow_id=wf, run_name=f"run-{tag}-1", task_hash="aa/000001")
    _seed_telemetry_failure(db_url, sample_id=samples[1], process="FETCH_READS", workflow_id=wf, run_name=f"run-{tag}-2", task_hash="bb/000002")
    _seed_telemetry_failure(db_url, sample_id=samples[0], process="KNEADDATA",   workflow_id=wf, run_name=f"run-{tag}-1", task_hash="cc/000003")

    resp = client.get(f"/api/cohorts/{cid}/failures?process=FETCH_READS&workflow_id={wf}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["process"] == "FETCH_READS"
    assert len(body["rows"]) == 2
    hashes = {r["task_hash"] for r in body["rows"]}
    assert hashes == {"aa/000001", "bb/000002"}
    sample_set = {r["sample_id"] for r in body["rows"]}
    assert sample_set == set(samples)
