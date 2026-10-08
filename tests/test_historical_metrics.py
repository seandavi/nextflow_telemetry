"""The historical tier: DuckDB over v2's NDJSON event archive.

The archive is a temp directory laid out like R2 (``dt=YYYY-MM-DD/*.ndjson.gz``);
v2 API reads are monkeypatched. Responses are validated against the v1 models
the dashboard consumes.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextflow_telemetry import models
from nextflow_telemetry.routers.cohorts import CohortFailuresResponse, create_cohorts_router
from nextflow_telemetry.services import v2_api
from nextflow_telemetry.services.event_archive import EventArchive
from nextflow_telemetry.services.process_metrics import ProcessMetricsService

NOW = datetime.now(timezone.utc)
RUNS = {"run-a": ("cmgd_nextflow", "2.2.1"), "run-b": ("cmgd_nextflow", "2.2.0")}


def _task(run, sample, process, status, *, attempt=1, exit_="0", action=None, hours_ago=1, hash_="ab/cdef01"):
    return {
        "run_name": run, "run_id": f"id-{run}", "event": "process_completed", "source": "weblog",
        "sample_id": sample, "process": process,
        "utc_time": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {"runName": run, "event": "process_completed", "trace": {
            "task_id": 1, "status": status, "hash": hash_, "name": f"{process} ({sample})",
            "exit": exit_, "process": process, "tag": sample, "attempt": attempt,
            "error_action": action, "cpus": 8, "memory": 16 * 1024**3, "time": 86400000,
            "realtime": 60000, "%cpu": 400.0, "%mem": 1.5, "peak_rss": 4 * 1024**3,
            "read_bytes": 1024**3, "write_bytes": 1024**3, "rchar": 2 * 1024**3, "wchar": 1024**3,
        }},
    }


def _write(root: Path, day: str, name: str, events: list[dict]) -> None:
    part = root / f"dt={day}"
    part.mkdir(parents=True, exist_ok=True)
    (part / name).write_bytes(gzip.compress("".join(json.dumps(e) + "\n" for e in events).encode()))


EVENTS = [
    {"run_name": "run-a", "event": "started", "source": "weblog", "utc_time": (NOW - timedelta(hours=3)).isoformat(),
     "payload": {"metadata": {"workflow": {"manifest": {"version": "2.2.3"}}}}},
    {"run_name": "run-a", "event": "run_heartbeat", "source": "run_event", "utc_time": NOW.isoformat(),
     "payload": {"type": "heartbeat"}},
    _task("run-a", "s1", "kneaddata", "COMPLETED"),
    _task("run-a", "s1", "fasterq_dump", "COMPLETED"),
    _task("run-a", "s2", "kneaddata", "FAILED", exit_="137", action="RETRY", hash_="aa/000001", hours_ago=2),
    _task("run-a", "s2", "kneaddata", "COMPLETED", attempt=2),
    _task("run-a", "s3", "kneaddata", "FAILED", exit_="1", action="TERMINATE", hash_="bb/000002"),
    _task("run-b", "s1", "kneaddata", "FAILED", exit_="137", action="RETRY", hash_="cc/000003"),
    # Outside the default 7-day window.
    _task("run-a", "s4", "kneaddata", "FAILED", exit_="2", hours_ago=24 * 30),
]


@pytest.fixture()
def v2_calls(monkeypatch):
    calls: list[set[str]] = []

    def run_workflows(_url, run_names):
        calls.append(set(run_names))
        return {r: RUNS[r] for r in run_names if r in RUNS}

    monkeypatch.setattr(v2_api, "run_workflows", run_workflows)
    monkeypatch.setattr(v2_api, "active_workflows", lambda _url: [("cmgd_nextflow", "2.2.1")])
    monkeypatch.setattr(
        v2_api, "collection_samples",
        lambda _url, cid: {"CohortX": ["s1", "s2"], "Empty": []}.get(cid),
    )
    return calls


@pytest.fixture()
def archive(tmp_path, v2_calls):
    _write(tmp_path, "2026-10-01", "000000-old.ndjson.gz", EVENTS[-1:])
    _write(tmp_path, NOW.date().isoformat(), "120000-new.ndjson.gz", EVENTS[:-1])
    return EventArchive(source=str(tmp_path), v2_api_url="http://v2.invalid", refresh_seconds=0)


@pytest.fixture()
def svc(archive) -> ProcessMetricsService:
    return ProcessMetricsService(engine=None, archive=archive)  # type: ignore[arg-type]


async def test_failures_ranks_processes_with_modal_exit_code(svc):
    body = models.ProcessFailuresResponse.model_validate(await svc.failures(min_samples=1))
    assert body.window_days == 7
    top = body.rows[0]
    assert (top.process, top.total_completed, top.success, top.failed) == ("kneaddata", 5, 2, 3)
    assert top.failure_pct == 60.0
    assert top.modal_failure_exit_code == "137"
    assert top.modal_error_action == "RETRY"


async def test_failure_signatures_group_by_process_exit_action(svc):
    body = models.ProcessFailureSignaturesResponse.model_validate(await svc.failure_signatures())
    sigs = {(r.process, r.exit_code, r.error_action): r.failures for r in body.rows}
    assert sigs == {("kneaddata", "137", "RETRY"): 2, ("kneaddata", "1", "TERMINATE"): 1}


async def test_all_time_window_includes_old_partition(svc):
    body = await svc.failure_signatures(window_days=10000)
    assert ("kneaddata", "2") in {(r["process"], r["exit_code"]) for r in body["rows"]}


async def test_workflow_version_filter_uses_v2_run_attribution(svc):
    # run-a's manifest says 2.2.3, v2 says 2.2.1: v2 wins.
    body = await svc.failure_signatures(workflow_id="cmgd_nextflow", workflow_version="2.2.1")
    assert sum(r["failures"] for r in body["rows"]) == 2
    body = await svc.failure_signatures(workflow_version="2.2.3")
    assert body["rows"] == []


async def test_summary_cards_and_event_mix(svc):
    body = models.ProcessSummaryResponse.model_validate(await svc.summary(min_samples=1))
    c = body.cards
    assert (c.process_completed_rows, c.distinct_runs, c.success_rows, c.failure_rows) == (6, 2, 3, 3)
    assert c.retried_rows == 1 and c.retry_success_pct == 100.0
    assert c.memory_efficiency_pct == 25.0
    # Weblog events only, like v1's telemetry table.
    assert {r.event: r.rows for r in body.event_mix} == {"process_completed": 6, "started": 1}
    assert body.top_failure_exit_codes[0].exit_code == "137"


async def test_retries_by_attempt(svc):
    body = models.ProcessRetriesResponse.model_validate(await svc.retries(min_samples=1))
    assert [(r.attempt, r.rows) for r in body.by_attempt] == [(1, 5), (2, 1)]
    assert body.by_process[0].max_attempt == 2


async def test_resources_by_attempt(svc):
    body = models.ProcessResourcesByAttemptResponse.model_validate(
        await svc.resources_by_attempt(min_samples=1)
    )
    row = next(r for r in body.rows if r.process == "kneaddata" and r.attempt == 1)
    assert row.avg_requested_memory_gb == 16.0
    assert row.avg_cpu_efficiency_pct == 50.0
    assert row.avg_peak_rss_gb == 4.0


async def test_tasks_paginates_newest_first(svc):
    body = models.TasksResponse.model_validate(await svc.tasks(status="FAILED", limit=1))
    assert body.total == 3
    row = body.rows[0]
    assert row.utc_time.tzinfo is not None
    assert row.status == "FAILED" and row.workflow_version in {"2.2.1", "2.2.0"}
    assert row.read_gb == 2.0  # v1 reported rchar as read_gb


async def test_timeline_buckets(svc):
    body = models.ProcessTimelineResponse.model_validate(await svc.timeline(bucket="day"))
    assert sum(r.total for r in body.rows) == 6
    assert sum(r.failed for r in body.rows) == 3


async def test_cohort_failures_scoped_to_active_version(svc):
    rows = await svc.cohort_failures("CohortX", "kneaddata", None, None)
    # s2's failure under active 2.2.1; s1's failure is in run-b (2.2.0); s3 is not a member.
    assert [r["task_hash"] for r in rows] == ["aa/000001"]
    rows = await svc.cohort_failures("CohortX", "kneaddata", None, None, include_all_workflows=True)
    assert {r["task_hash"] for r in rows} == {"aa/000001", "cc/000003"}
    rows = await svc.cohort_failures("CohortX", "kneaddata", None, "2.2.0")
    assert [r["task_hash"] for r in rows] == ["cc/000003"]
    assert await svc.cohort_failures("Empty", "kneaddata", None, None) == []


def test_cohort_failures_route(svc):
    app = FastAPI()
    app.include_router(create_cohorts_router(engine=None, process_metrics=svc))  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/cohorts/nope/failures", params={"process": "kneaddata"}).status_code == 404
        resp = client.get("/cohorts/CohortX/failures", params={"process": "kneaddata", "all_workflows": "true"})
    assert resp.status_code == 200
    body = CohortFailuresResponse.model_validate(resp.json())
    assert len(body.rows) == 2 and body.rows[0].attempt == 1


async def test_refresh_reads_only_new_files_and_asks_v2_once_per_run(svc, archive, tmp_path, v2_calls):
    assert (await svc.tasks(window_days=10000))["total"] == 7
    _write(tmp_path, NOW.date().isoformat(), "130000-more.ndjson.gz", [_task("run-a", "s9", "humann", "COMPLETED")])
    assert (await svc.tasks(window_days=10000))["total"] == 8
    assert v2_calls == [{"run-a", "run-b"}]
