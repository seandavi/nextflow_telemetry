"""End-to-end tests: real Nextflow process posting weblog events to a live server.

These tests start a FastAPI/uvicorn server on a free port, register samples
and workflows via the API, then run the nf_testing stub pipeline as a
subprocess. Nextflow's -with-weblog flag POSTs events to our server, which
drives job state transitions exactly as it would in production.

Requires:
  - nextflow on PATH
  - Docker available (for testcontainers postgres)

Skip with:  NF_E2E_SKIP=1 pytest tests/test_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("NF_E2E_SKIP") == "1" or not shutil.which("nextflow"),
    reason="nextflow not on PATH or NF_E2E_SKIP=1",
)

NF_TESTING_DIR = Path(__file__).parent.parent / "nf_testing"


# ---------------------------------------------------------------------------
# Live server fixture (module-scoped: one server for all E2E tests)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class LiveServer:
    """Container for a running uvicorn test server and its captured logs."""
    url: str
    _log_lines: list[str] = field(default_factory=list)

    def get_logs(self, last_n: int = 100) -> str:
        return "".join(self._log_lines[-last_n:])

    # Convenience: f"{server}/path" and httpx.Client(base_url=str(server)) work.
    def __str__(self) -> str:
        return self.url


@pytest.fixture(scope="module")
def live_server(db_asyncpg_url):
    """Uvicorn server in a subprocess backed by the testcontainers postgres DB.

    Running in a subprocess (not a thread) avoids asyncpg event-loop conflicts
    that arise when the SQLAlchemy engine is used across different asyncio loops.
    Server logs are captured in a background thread so they're available in
    failure messages without blocking.
    """
    port = _free_port()
    env = {
        **os.environ,
        "SQLALCHEMY_URI": db_asyncpg_url,
        "TELEMETRY_SKIP_DB_INIT": "1",
    }
    server = LiveServer(url=f"http://127.0.0.1:{port}")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "nextflow_telemetry.main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "info",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _drain():
        for line in proc.stdout:
            server._log_lines.append(line.decode(errors="replace"))

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    base_url = server.url
    for _ in range(40):
        try:
            httpx.get(f"{base_url}/health", timeout=1).raise_for_status()
            break
        except Exception:
            if proc.poll() is not None:
                drain_thread.join(timeout=1)
                pytest.fail(f"Live server exited early:\n{server.get_logs()}")
            time.sleep(0.25)
    else:
        proc.terminate()
        drain_thread.join(timeout=2)
        pytest.fail(f"Live server did not start in time:\n{server.get_logs()}")

    yield server

    proc.terminate()
    proc.wait(timeout=5)
    drain_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(server: LiveServer | str) -> httpx.Client:
    return httpx.Client(base_url=str(server), timeout=30)


def _register_workflow(client: httpx.Client, wf_id: str, version: str, max_retries: int = 2) -> int:
    resp = client.post("/workflows", json={
        "workflow_id": wf_id,
        "version": version,
        "repository_url": str(NF_TESTING_DIR / "main.nf"),
        "revision": "local",
        "profile": "test",
        "max_retries": max_retries,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _run_nextflow(
    *,
    run_name: str,
    sample_ids: list[str],
    wf_id: str,
    wf_version: str,
    weblog_url: str,
    fail_at: str = "",
    work_dir: Path,
) -> subprocess.CompletedProcess:
    """Execute the nf_testing pipeline and wait for it to finish."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "nextflow", "run", str(NF_TESTING_DIR / "main.nf"),
        "-profile", "test",
        "-name", run_name,
        "-with-weblog", weblog_url,
        "--sample_ids", ",".join(sample_ids),
        "--workflow_id", wf_id,
        "--workflow_version", wf_version,
        "--run_name", run_name,
        "-w", str(work_dir / "work"),
    ]
    if fail_at:
        cmd += ["--fail_at", fail_at]

    return subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=120)


def _wait_for_job_status(
    db_asyncpg_url: str,
    *,
    sample_id: str,
    wf_id: str,
    expected_status: str,
    timeout: int = 20,
) -> dict:
    """Poll the DB until the job reaches expected_status or timeout."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from nextflow_telemetry.db import jobs_tbl

    async def _poll():
        engine = create_async_engine(db_asyncpg_url)
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            rows = []
            while asyncio.get_running_loop().time() < deadline:
                async with engine.connect() as conn:
                    rows = (await conn.execute(
                        select(jobs_tbl).where(
                            jobs_tbl.c.sample_id == sample_id,
                            jobs_tbl.c.workflow_id == wf_id,
                        )
                    )).mappings().all()
                if rows and rows[0]["status"] == expected_status:
                    return dict(rows[0])
                await asyncio.sleep(0.5)
            return dict(rows[0]) if rows else {}
        finally:
            await engine.dispose()

    return asyncio.run(_poll())


def _wait_for_run_status(
    db_asyncpg_url: str,
    *,
    run_name: str,
    expected_status: str,
    timeout: int = 20,
) -> dict:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from nextflow_telemetry.db import workflow_runs_tbl

    async def _poll():
        engine = create_async_engine(db_asyncpg_url)
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            rows = []
            while asyncio.get_running_loop().time() < deadline:
                async with engine.connect() as conn:
                    rows = (await conn.execute(
                        select(workflow_runs_tbl).where(
                            workflow_runs_tbl.c.run_name == run_name
                        )
                    )).mappings().all()
                if rows and rows[0]["status"] == expected_status:
                    return dict(rows[0])
                await asyncio.sleep(0.5)
            return dict(rows[0]) if rows else {}
        finally:
            await engine.dispose()

    return asyncio.run(_poll())


def _query_jobs(db_asyncpg_url: str, sample_ids: list[str], wf_id: str) -> dict[str, dict]:
    """Return {sample_id: job_row} for the given samples and workflow."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from nextflow_telemetry.db import jobs_tbl

    async def _fetch():
        engine = create_async_engine(db_asyncpg_url)
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(
                    select(jobs_tbl).where(
                        jobs_tbl.c.sample_id.in_(sample_ids),
                        jobs_tbl.c.workflow_id == wf_id,
                    )
                )).mappings().all()
            return {r["sample_id"]: dict(r) for r in rows}
        finally:
            await engine.dispose()

    return asyncio.run(_fetch())


def _count_telemetry_rows(db_asyncpg_url: str, run_name: str) -> int:
    """Count raw weblog events stored for this run — used for diagnostics."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine
    from nextflow_telemetry.db import telemetry_tbl

    async def _count():
        engine = create_async_engine(db_asyncpg_url)
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(
                    select(func.count()).select_from(telemetry_tbl).where(
                        telemetry_tbl.c.run_name == run_name
                    )
                )).scalar()
            return row or 0
        finally:
            await engine.dispose()

    return asyncio.run(_count())


# ---------------------------------------------------------------------------
# E2E: happy path — all samples complete via MARK_COMPLETE
# ---------------------------------------------------------------------------

def test_e2e_happy_path(live_server: LiveServer, db_asyncpg_url, tmp_path):
    """Real nextflow run: all samples reach MARK_COMPLETE → jobs completed."""
    wf_id = f"e2e-happy-{uuid.uuid4().hex[:6]}"
    wf_version = f"hp-{uuid.uuid4().hex[:6]}"
    sample_ids = [f"SRR-hp-{uuid.uuid4().hex[:6]}" for _ in range(2)]
    weblog_url = f"{live_server}/telemetry"

    with _api(live_server) as client:
        _register_workflow(client, wf_id, wf_version, max_retries=0)
        for sid in sample_ids:
            client.post("/samples", json={"sample_id": sid})
        client.post("/admin/reconcile-jobs")

        batch = client.post("/dispatch/batch", json={
            "workflow_id": wf_id, "workflow_version": wf_version, "limit": 500,
        })
        assert batch.status_code == 200, batch.text
        data = batch.json()
        run_name = data["run_name"]
        dispatched = [j["sample_id"] for j in data["jobs"]]
        assert set(sample_ids).issubset(set(dispatched))

        client.post("/dispatch/submitted", json={"run_name": run_name, "sample_ids": dispatched})

    result = _run_nextflow(
        run_name=run_name,
        sample_ids=sample_ids,
        wf_id=wf_id,
        wf_version=wf_version,
        weblog_url=weblog_url,
        work_dir=tmp_path,
    )
    assert result.returncode == 0, (
        f"nextflow exited {result.returncode}\n"
        f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-1000:]}"
    )

    telemetry_count = _count_telemetry_rows(db_asyncpg_url, run_name)

    # Poll until the workflow_run record is "completed" (confirms final event processed)
    run_row = _wait_for_run_status(db_asyncpg_url, run_name=run_name, expected_status="completed")
    assert run_row.get("status") == "completed", (
        f"workflow_run status={run_row.get('status')!r} "
        f"(telemetry rows stored: {telemetry_count})\n"
        f"nextflow stdout: {result.stdout[-2000:]}\n"
        f"server logs:\n{live_server.get_logs()}"
    )

    job_rows = _query_jobs(db_asyncpg_url, sample_ids, wf_id)
    for sid in sample_ids:
        assert job_rows.get(sid, {}).get("status") == "completed", (
            f"Sample {sid} status={job_rows.get(sid, {}).get('status')!r}\n"
            f"server logs:\n{live_server.get_logs()}"
        )


# ---------------------------------------------------------------------------
# E2E: failure + retry — inject a process failure, then succeed on retry
# ---------------------------------------------------------------------------

def test_e2e_retry_on_failure(live_server: LiveServer, db_asyncpg_url, tmp_path):
    """Nextflow run that fails → job re-enqueued → second run succeeds."""
    from nextflow_telemetry.db import dead_letter_tbl
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    wf_id = f"e2e-retry-{uuid.uuid4().hex[:6]}"
    wf_version = f"rt-{uuid.uuid4().hex[:6]}"
    sample_id = f"SRR-rt-{uuid.uuid4().hex[:6]}"
    weblog_url = f"{live_server}/telemetry"

    with _api(live_server) as client:
        _register_workflow(client, wf_id, wf_version, max_retries=2)
        client.post("/samples", json={"sample_id": sample_id})
        client.post("/admin/reconcile-jobs")

        b1 = client.post("/dispatch/batch", json={
            "workflow_id": wf_id, "workflow_version": wf_version, "limit": 500,
        })
        assert b1.status_code == 200
        run1_name = b1.json()["run_name"]
        client.post("/dispatch/submitted", json={
            "run_name": run1_name,
            "sample_ids": [j["sample_id"] for j in b1.json()["jobs"]],
        })

    # First run: FETCH_READS fails — MARK_COMPLETE is never reached
    result1 = _run_nextflow(
        run_name=run1_name,
        sample_ids=[sample_id],
        wf_id=wf_id,
        wf_version=wf_version,
        weblog_url=weblog_url,
        fail_at="FETCH_READS",
        work_dir=tmp_path / "run1",
    )
    # returncode will be non-zero — that's expected for a failed run

    telemetry_count1 = _count_telemetry_rows(db_asyncpg_url, run1_name)

    # Poll until the job is back to "pending" (sweep processed)
    job = _wait_for_job_status(
        db_asyncpg_url, sample_id=sample_id, wf_id=wf_id, expected_status="pending"
    )
    assert job.get("status") == "pending", (
        f"Expected job to be re-enqueued, got {job.get('status')!r} "
        f"(telemetry rows for run1: {telemetry_count1})\n"
        f"nextflow stdout: {result1.stdout[-2000:]}\n"
        f"server logs:\n{live_server.get_logs()}"
    )
    assert job.get("retry_count") == 1

    with _api(live_server) as client:
        # Second run: no failure injection → reaches MARK_COMPLETE
        b2 = client.post("/dispatch/batch", json={
            "workflow_id": wf_id, "workflow_version": wf_version, "limit": 500,
        })
        assert b2.status_code == 200, "Job should be available for retry"
        run2_name = b2.json()["run_name"]
        client.post("/dispatch/submitted", json={
            "run_name": run2_name,
            "sample_ids": [j["sample_id"] for j in b2.json()["jobs"]],
        })

    result2 = _run_nextflow(
        run_name=run2_name,
        sample_ids=[sample_id],
        wf_id=wf_id,
        wf_version=wf_version,
        weblog_url=weblog_url,
        work_dir=tmp_path / "run2",
    )
    assert result2.returncode == 0, (
        f"Second run failed unexpectedly:\n{result2.stdout[-1000:]}\n{result2.stderr[-500:]}"
    )

    job = _wait_for_job_status(
        db_asyncpg_url, sample_id=sample_id, wf_id=wf_id, expected_status="completed"
    )
    assert job.get("status") == "completed"
    assert job.get("retry_count") == 1  # only incremented on the first failure

    # No DLQ entry — job eventually succeeded
    async def _check_dlq():
        engine = create_async_engine(db_asyncpg_url)
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(
                    select(dead_letter_tbl).where(
                        dead_letter_tbl.c.sample_id == sample_id,
                        dead_letter_tbl.c.workflow_id == wf_id,
                    )
                )).mappings().all()
            return list(rows)
        finally:
            await engine.dispose()

    dlq_rows = asyncio.run(_check_dlq())
    assert len(dlq_rows) == 0, "No DLQ entry expected — job succeeded on retry"
