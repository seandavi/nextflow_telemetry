"""Migration fa0b1c2d (#371): control-plane schema and v1_* renames.

Each test uses its own scratch database on the session testcontainers Postgres and
runs Alembic in a subprocess against it.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from nextflow_telemetry import control_db as c
from nextflow_telemetry.db import (
    metadata,
    metadata_assets_tbl,
    metadata_snapshots_tbl,
    metadata_sources_tbl,
    submissions_tbl,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "e9fa0b1c"
V1_TABLES = (
    "telemetry", "task_executions", "samples", "collections", "collection_samples",
    "workflows", "jobs", "workflow_runs", "task_logs", "dead_letter", "daemon_agents",
)
NEW_TABLES = (
    "studies", "study_aliases", "samples", "readset_runs", "collections",
    "collection_samples", "workflows", "runs", "jobs", "dead_letter", "daemons",
    "run_task_counts", "run_failed_tasks", "ingest_cursor", "curation_decisions",
)
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _scratch_db(db_asyncpg_url: str, name: str) -> str:
    async def _create() -> None:
        engine = create_async_engine(db_asyncpg_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as con:
            await con.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            await con.execute(text(f"CREATE DATABASE {name}"))
        await engine.dispose()

    asyncio.run(_create())
    return make_url(db_asyncpg_url).set(database=name).render_as_string(hide_password=False)


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, env={**os.environ, "SQLALCHEMY_URI": url}, check=True,
    )


def _with_connection(url: str, fn: Callable[[AsyncConnection], Awaitable[None]],
                     rollback: bool = True) -> None:
    async def _run() -> None:
        engine = create_async_engine(url)
        async with engine.connect() as con:
            await fn(con)
            if rollback:
                await con.rollback()
            else:
                await con.commit()
        await engine.dispose()

    asyncio.run(_run())


async def _all(con: AsyncConnection, sql: str, **params):
    return (await con.execute(text(sql), params)).all()


async def _index_names(con: AsyncConnection, tables: tuple[str, ...]) -> set[tuple[str, str]]:
    rows = await _all(
        con,
        "select tablename, indexname from pg_indexes "
        "where schemaname = 'public' and tablename = any(:t)",
        t=list(tables),
    )
    return {(r[0], r[1]) for r in rows}


async def _owned_sequences(con: AsyncConnection, table: str) -> set[str]:
    rows = await _all(
        con,
        "select s.relname from pg_class s join pg_depend d on d.objid = s.oid "
        "where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass "
        "and d.refobjid = to_regclass(:t) and s.relkind = 'S' and d.deptype in ('a', 'i')",
        t=table,
    )
    return {r[0] for r in rows}


async def _table_names(con: AsyncConnection) -> set[str]:
    rows = await _all(con, "select tablename from pg_tables where schemaname = 'public'")
    return {r[0] for r in rows}


async def _rejects(con: AsyncConnection, stmt) -> None:
    with pytest.raises(IntegrityError):
        async with con.begin_nested():
            await con.execute(stmt)


async def _accepts(con: AsyncConnection, stmt) -> None:
    async with con.begin_nested():
        await con.execute(stmt)


def test_upgrade_downgrade_round_trip(db_asyncpg_url: str) -> None:
    url = _scratch_db(db_asyncpg_url, "nf_m1_roundtrip")
    _alembic(url, "upgrade", PREVIOUS_HEAD)
    before: dict[str, set[tuple[str, str]]] = {}

    async def seed(con: AsyncConnection) -> None:
        await con.execute(text(
            "insert into samples (sample_id, created_at, updated_at) values ('s1', now(), now())"))
        await con.execute(text(
            "insert into workflows (workflow_id, version, repository_url, revision, "
            "max_retries, status, created_at, updated_at) "
            "values ('wf', '1', 'https://example.test/wf', 'main', 3, 'active', now(), now())"))
        await con.execute(text(
            "insert into jobs (sample_id, workflow_pk, workflow_id, workflow_version, "
            "status, retry_count, created_at) "
            "select 's1', id, 'wf', '1', 'pending', 0, now() from workflows"))
        before["indexes"] = await _index_names(con, V1_TABLES)
        before["tables"] = {(t, "") for t in await _table_names(con)}

    _with_connection(url, seed, rollback=False)

    _alembic(url, "upgrade", "head")

    async def after_upgrade(con: AsyncConnection) -> None:
        tables = await _table_names(con)
        assert {f"v1_{t}" for t in V1_TABLES} <= tables
        assert set(NEW_TABLES) <= tables
        for t in V1_TABLES:
            for _, index in await _index_names(con, (f"v1_{t}",)):
                assert index.startswith("v1_"), index
            for seq in await _owned_sequences(con, f"v1_{t}"):
                assert seq.startswith("v1_"), seq
        assert (await _all(con, "select sample_id from v1_samples")) == [("s1",)]
        assert (await _all(con, "select count(*) from v1_jobs"))[0][0] == 1
        # Row inserted after the rename still draws from the renamed sequence.
        new_id = (await _all(
            con,
            "insert into v1_samples (sample_id, created_at, updated_at) "
            "values ('s2', now(), now()) returning id"))[0][0]
        assert new_id == 2
        columns = {c_["name"] for c_ in await con.run_sync(
            lambda sc: inspect(sc).get_columns("submissions"))}
        assert {"source_snapshot_source_id", "source_snapshot_asset_id",
                "source_snapshot_version"} <= columns

    _with_connection(url, after_upgrade)

    _alembic(url, "downgrade", PREVIOUS_HEAD)

    async def after_downgrade(con: AsyncConnection) -> None:
        tables = await _table_names(con)
        assert not (set(NEW_TABLES) - set(V1_TABLES)) & tables
        assert not any(t.startswith("v1_") for t in tables)
        assert await _index_names(con, V1_TABLES) == before["indexes"]
        assert (await _all(con, "select sample_id from samples")) == [("s1",)]
        assert (await _all(con, "select count(*) from jobs"))[0][0] == 1
        assert not await _all(
            con, "select 1 from pg_proc where proname = 'workflows_bundle_immutable'")

    _with_connection(url, after_downgrade)

    _alembic(url, "upgrade", "head")


@pytest.fixture(scope="module")
def migrated_url(db_asyncpg_url: str) -> Iterator[str]:
    url = _scratch_db(db_asyncpg_url, "nf_m1_constraints")
    _alembic(url, "upgrade", "head")
    yield url


def test_columns_match_table_definitions(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        for name in (*NEW_TABLES, "submissions", "deadline_sweep"):
            expected = {col.name: col.nullable for col in metadata.tables[name].columns}
            actual = {
                col["name"]: col["nullable"]
                for col in await con.run_sync(lambda sc, n=name: inspect(sc).get_columns(n))
            }
            assert actual == expected, name

    _with_connection(migrated_url, check)


async def _seed_parents(con: AsyncConnection) -> dict:
    workflow_pk = (await con.execute(
        insert(c.workflows_tbl)
        .values(workflow_id="wf", version="1", repository_url="https://example.test/wf",
                revision="main")
        .returning(c.workflows_tbl.c.id)
    )).scalar_one()
    await con.execute(insert(c.samples_tbl).values(sample_id="s1"))
    await con.execute(insert(c.runs_tbl).values(run_name="r1", status="claimed"))
    return {"workflow_pk": workflow_pk}


def _job(workflow_pk: int, **kw):
    return insert(c.jobs_tbl).values(
        sample_id="s1", workflow_pk=workflow_pk, workflow_id="wf", workflow_version="1", **kw)


def test_workflow_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        await _seed_parents(con)
        w = c.workflows_tbl
        await _rejects(con, insert(w).values(
            workflow_id="wf", version="1", repository_url="u", revision="main"))
        await _rejects(con, insert(w).values(
            workflow_id="wf", version="2", repository_url="u", revision="main", collections=[]))
        await _accepts(con, insert(w).values(
            workflow_id="wf", version="3", repository_url="u", revision="main",
            collections=["c1"], params={"k": 1}))
        await _rejects(con, update(w).values(params={"k": 2}).where(w.c.version == "3"))
        await _rejects(con, update(w).values(sample_key="sample_id").where(w.c.version == "3"))
        await _accepts(con, update(w).values(revision="v2").where(w.c.version == "3"))

    _with_connection(migrated_url, check)


def test_membership_and_readset_run_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        await _seed_parents(con)
        await con.execute(insert(c.collections_tbl).values(collection_id="col"))
        await con.execute(insert(c.collection_samples_tbl).values(collection_id="col", sample_id="s1"))
        await _rejects(con, insert(c.collection_samples_tbl).values(collection_id="col", sample_id="s1"))

        rr = c.readset_runs_tbl
        await con.execute(insert(rr).values(run_accession="SRR1", readset_id="rs1"))
        await _rejects(con, insert(rr).values(run_accession="SRR1", readset_id="rs2"))
        await _rejects(con, insert(rr).values(run_accession="XRR1", readset_id="rs1"))
        await _rejects(con, insert(rr).values(run_accession="SRR", readset_id="rs1"))

    _with_connection(migrated_url, check)


def test_job_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        pk = (await _seed_parents(con))["workflow_pk"]
        await con.execute(_job(pk))
        await _rejects(con, _job(pk))  # duplicate (sample_id, workflow_pk)
        await con.execute(c.samples_tbl.insert().values(sample_id="s2"))

        def other(**kw):
            return insert(c.jobs_tbl).values(
                sample_id="s2", workflow_pk=pk, workflow_id="wf", workflow_version="1", **kw)

        await _rejects(con, other(status="bogus", run_name="r1"))
        await _rejects(con, other(status="completed", run_name="r1"))
        await _rejects(con, other(status="failed", run_name="r1"))
        await _rejects(con, other(status="pending", run_name="r1"))
        await _rejects(con, other(status="claimed"))
        await _rejects(con, other(retry_count=-1))
        await _accepts(con, other(status="completed", run_name="r1", completed_at=NOW))

    _with_connection(migrated_url, check)


def test_run_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        r = c.runs_tbl
        await _rejects(con, insert(r).values(run_name="x", status="bogus"))
        await _rejects(con, insert(r).values(
            run_name="x", status="claimed", claim_expires_at=NOW, submit_deadline=NOW))
        await _rejects(con, insert(r).values(
            run_name="x", status="completed", completed_at=NOW, liveness_deadline=NOW))
        await _rejects(con, insert(r).values(run_name="x", status="failed"))
        await _accepts(con, insert(r).values(run_name="x", status="running", liveness_deadline=NOW))
        await _accepts(con, insert(r).values(run_name="y", status="completed", completed_at=NOW))

    _with_connection(migrated_url, check)


def test_daemon_alias_and_curation_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        def daemon(agent_id: str, status: str):
            return insert(c.daemons_tbl).values(
                agent_id=agent_id, hostname="h", status=status, last_seen_at=NOW, started_at=NOW)

        await _rejects(con, daemon("a", "stale"))
        await _accepts(con, daemon("b", "stopped"))

        study_id = (await con.execute(
            insert(c.studies_tbl).values(name="st").returning(c.studies_tbl.c.study_id)
        )).scalar_one()
        await _rejects(con, insert(c.study_aliases_tbl).values(
            study_id=study_id, scheme="nope", value="v"))
        await _accepts(con, insert(c.study_aliases_tbl).values(
            study_id=study_id, scheme="doi", value="10.1/x"))

        d = c.curation_decisions_tbl
        await _rejects(con, insert(d).values(
            subject_type="study", subject_id="1", decision="maybe", reason="r", actor="a"))
        await _accepts(con, insert(d).values(
            subject_type="study", subject_id="1", decision="exclude", reason="r", actor="a"))

    _with_connection(migrated_url, check)


def test_submission_snapshot_constraints(migrated_url: str) -> None:
    async def check(con: AsyncConnection) -> None:
        await con.execute(insert(metadata_sources_tbl).values(
            source_id="src", descriptor={}, created_at=NOW, updated_at=NOW))
        await con.execute(insert(metadata_assets_tbl).values(
            source_id="src", asset_id="asset", descriptor={}, created_at=NOW, updated_at=NOW))
        await con.execute(insert(metadata_snapshots_tbl).values(
            source_id="src", asset_id="asset", version="v1", sha256="0" * 64, size_bytes=1,
            artifact_uri="r2://x", source_descriptor={}, asset_descriptor={},
            captured_at=NOW, captured_by="t"))

        def submission(sid: str, **kw):
            return insert(submissions_tbl).values(
                submission_id=sid, method="manual", submitted_by="t", status="succeeded",
                created_at=NOW, **kw)

        await _rejects(con, submission("a", source_snapshot_source_id="src"))
        await _rejects(con, submission(
            "b", source_snapshot_source_id="src", source_snapshot_asset_id="asset"))
        await _rejects(con, submission(
            "c", source_snapshot_source_id="src", source_snapshot_asset_id="asset",
            source_snapshot_version="missing"))
        await _accepts(con, submission(
            "d", source_snapshot_source_id="src", source_snapshot_asset_id="asset",
            source_snapshot_version="v1"))

    _with_connection(migrated_url, check)
