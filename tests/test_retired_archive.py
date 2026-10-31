"""Retired-job archive (services/retired_archive.py, #374)."""
from __future__ import annotations

import gzip
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nextflow_telemetry.control_db import (
    dead_letter_tbl,
    jobs_tbl,
    runs_tbl,
    samples_tbl,
    workflows_tbl,
)
from nextflow_telemetry.services import retired_archive
from nextflow_telemetry.services.retired_archive import (
    ArchiveResult,
    RetiredJobArchive,
    seconds_until_archive,
)

T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture()
async def engine(db_asyncpg_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(db_asyncpg_url)
    yield eng
    await eng.dispose()


async def _seed_version(
    engine: AsyncEngine, version: str, status: str, job_statuses: list[str],
) -> int:
    """A workflow version with one job per status; failed jobs also get a dead_letter row."""
    async with engine.begin() as conn:
        pk = (await conn.execute(
            insert(workflows_tbl)
            .values(workflow_id="wf", version=version, repository_url="https://example.test/wf",
                    revision="main", status=status)
            .returning(workflows_tbl.c.id)
        )).scalar_one()
        run_name = f"run-{version}"
        await conn.execute(insert(runs_tbl).values(
            run_name=run_name, status="completed", completed_at=T0, workflow_pk=pk))
        for i, job_status in enumerate(job_statuses):
            sample_id = f"s-{version}-{i}"
            await conn.execute(insert(samples_tbl).values(sample_id=sample_id))
            job_id = (await conn.execute(
                insert(jobs_tbl)
                .values(sample_id=sample_id, workflow_pk=pk, workflow_id="wf",
                        workflow_version=version, status=job_status,
                        run_name=None if job_status == "pending" else run_name,
                        completed_at=T0 if job_status == "completed" else None,
                        failed_at=T0 if job_status == "failed" else None)
                .returning(jobs_tbl.c.id)
            )).scalar_one()
            if job_status == "failed":
                await conn.execute(insert(dead_letter_tbl).values(
                    job_id=job_id, run_name=run_name, sample_id=sample_id, workflow_id="wf",
                    workflow_version=version, reason="boom"))
    return pk


async def _count(engine: AsyncEngine, table) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


def _read(path: Path) -> list[dict]:
    with gzip.open(path, "rb") as f:
        return [orjson.loads(line) for line in f.read().splitlines()]


def _objects(root: Path, pk: int) -> list[Path]:
    return sorted((root / "jobs" / str(pk)).glob("*.ndjson.gz"), key=lambda p: p.name)


async def test_archives_and_purges_retired_jobs(engine: AsyncEngine, tmp_path: Path) -> None:
    pk = await _seed_version(engine, "1", "retired", ["completed", "completed", "failed"])
    active_pk = await _seed_version(engine, "2", "active", ["completed"])
    archive = RetiredJobArchive(engine, str(tmp_path / "jobs"))

    result = await archive.archive()

    assert result == ArchiveResult(workflows_archived=1, jobs_archived=3)
    (obj,) = _objects(tmp_path, pk)
    assert obj.name.endswith("Z-0.ndjson.gz")
    lines = _read(obj)
    assert [line["status"] for line in lines[:3]] == ["completed", "completed", "failed"]
    assert lines[3]["reason"] == "boom"
    assert lines[0]["completed_at"].startswith("2026-10-30T12:00:00")
    assert await _count(engine, dead_letter_tbl) == 0
    async with engine.connect() as conn:
        left = (await conn.execute(select(jobs_tbl.c.workflow_pk))).scalars().all()
    assert left == [active_pk]
    assert not (tmp_path / "jobs" / str(active_pk)).exists()


async def test_skips_version_with_jobs_in_flight(engine: AsyncEngine, tmp_path: Path) -> None:
    pk = await _seed_version(engine, "1", "retired", ["completed", "running"])
    archive = RetiredJobArchive(engine, str(tmp_path / "jobs"))

    assert await archive.archive() == ArchiveResult()
    assert await _count(engine, jobs_tbl) == 2

    async with engine.begin() as conn:
        await conn.execute(update(jobs_tbl).where(jobs_tbl.c.status == "running")
                           .values(status="completed", completed_at=T0))
    assert await archive.archive() == ArchiveResult(workflows_archived=1, jobs_archived=2)
    assert len(_objects(tmp_path, pk)) == 1
    assert await _count(engine, jobs_tbl) == 0


async def test_active_and_unknown_versions_untouched(engine: AsyncEngine, tmp_path: Path) -> None:
    pk = await _seed_version(engine, "1", "active", ["completed"])
    archive = RetiredJobArchive(engine, str(tmp_path / "jobs"))

    assert await archive.archive(pk) == ArchiveResult()
    assert await archive.archive(pk + 1000) == ArchiveResult()
    assert await _count(engine, jobs_tbl) == 1
    assert not (tmp_path / "jobs").exists()


async def test_archive_chunks(engine: AsyncEngine, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(retired_archive, "ARCHIVE_CHUNK", 2)
    pk = await _seed_version(engine, "1", "retired", ["completed"] * 5)
    archive = RetiredJobArchive(engine, str(tmp_path / "jobs"))

    result = await archive.archive()

    assert result == ArchiveResult(workflows_archived=1, jobs_archived=5)
    objects = _objects(tmp_path, pk)
    assert [o.name.rsplit("-", 1)[1] for o in objects] == ["0.ndjson.gz", "2.ndjson.gz", "4.ndjson.gz"]
    assert [len(_read(o)) for o in objects] == [2, 2, 1]


async def test_failed_put_keeps_rows(engine: AsyncEngine, tmp_path: Path) -> None:
    await _seed_version(engine, "1", "retired", ["completed", "failed"])
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    archive = RetiredJobArchive(engine, str(blocker / "jobs"))

    with pytest.raises(Exception):
        await archive.archive()

    assert await _count(engine, jobs_tbl) == 2
    assert await _count(engine, dead_letter_tbl) == 1


def test_seconds_until_archive() -> None:
    def at(h: int, m: int = 0) -> datetime:
        return datetime(2026, 10, 30, h, m, tzinfo=timezone.utc)

    assert seconds_until_archive(at(5, 59)) == 60
    assert seconds_until_archive(at(6)) == 86400
    assert seconds_until_archive(at(7)) == 82800


async def test_archive_endpoint_requires_operator(
        engine: AsyncEngine, tmp_path: Path, monkeypatch, request: pytest.FixtureRequest) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", "t")
    monkeypatch.setenv("RETIRED_ARCHIVE_URL", str(tmp_path / "jobs"))
    pk = await _seed_version(engine, "1", "retired", ["completed"])
    client, _ = request.getfixturevalue("integration_client")

    assert client.post("/api/admin/archive-retired").status_code == 401
    resp = client.post(
        "/api/admin/archive-retired", params={"workflow_pk": pk},
        headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json() == {"workflows_archived": 1, "jobs_archived": 1}
    assert len(_objects(tmp_path, pk)) == 1
