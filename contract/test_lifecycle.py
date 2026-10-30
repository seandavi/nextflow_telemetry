import time

import httpx
import pytest
from pydantic import TypeAdapter

from contract import schemas
from contract.support import (
    claim,
    eventually,
    job_summary,
    mark_complete,
    registration,
    run_detail,
    start_run,
    submitted,
    validate,
    weblog,
)

pytestmark = pytest.mark.writes


def test_happy_path(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-happy"
    wf, _ = registration(api, name, samples=2, max_retries=1)
    pk = wf["id"]

    batch = claim(api, name, limit=2)
    assert batch is not None
    assert len(batch["jobs"]) == 2
    assert batch["workflow_id"] == name
    assert batch["repository_url"] == wf["repository_url"]
    assert batch["params"] == wf["params"]
    run = batch["run_name"]
    assert job_summary(api, pk)["claimed"] == 2

    submitted(api, run, [j["sample_id"] for j in batch["jobs"]])
    assert job_summary(api, pk)["submitted"] == 2
    assert run_detail(api, run)["status"] == "submitted"

    weblog(edge, run, "started")
    eventually(lambda: _assert_equal(job_summary(api, pk)["running"], 2), lag)
    eventually(lambda: _assert_equal(run_detail(api, run)["status"], "running"), lag)

    mark_complete(edge, run, batch["jobs"][0]["sample_id"])
    eventually(lambda: _assert_equal(job_summary(api, pk)["completed"], 1), lag)

    weblog(edge, run, "completed")

    def closed() -> None:
        assert run_detail(api, run)["status"] == "completed"
        summary = job_summary(api, pk)
        assert (summary["completed"], summary["running"], summary["pending"]) == (1, 0, 1)

    eventually(closed, lag)

    cohort = validate(schemas.CohortSummary, api.get(f"cohorts/{name}/summary"))
    assert cohort.samples_completed == 1
    board = validate(TypeAdapter(list[schemas.Cohort]), api.get("cohorts/leaderboard"))
    assert [c.sample_count for c in board if c.collection_id == name] == [2]


def test_late_mark_complete_never_flips(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-late"
    wf, _ = registration(api, name, samples=2, max_retries=1)
    pk = wf["id"]
    batch = start_run(api, edge, name, limit=2)
    run = batch["run_name"]
    mark_complete(edge, run, batch["jobs"][0]["sample_id"])
    weblog(edge, run, "completed")
    eventually(lambda: _assert_equal(run_detail(api, run)["status"], "completed"), lag)
    completed = job_summary(api, pk)["completed"]

    mark_complete(edge, run, batch["jobs"][1]["sample_id"])
    time.sleep(lag)
    assert job_summary(api, pk)["completed"] == completed


def test_retry_budget_dead_letters(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-dlq"
    wf, _ = registration(api, name, max_retries=0)
    pk = wf["id"]
    run = start_run(api, edge, name)["run_name"]
    weblog(edge, run, "completed")

    def dead() -> None:
        summary = job_summary(api, pk)
        assert (summary["failed"], summary["dead_letter"]) == (1, 1)
        assert run_detail(api, run)["job_status_counts"] == {"failed": 1}

    eventually(dead, lag)

    requeued = validate(schemas.RequeuedDeadLetter, api.post("admin/requeue-dead-letter", json={}))
    assert requeued.requeued >= 1
    summary = job_summary(api, pk)
    assert (summary["failed"], summary["pending"]) == (0, 1)


def test_nextflow_failure_reason(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-nffail"
    registration(api, name)
    run = start_run(api, edge, name)["run_name"]
    workflow = {"success": False, "errorMessage": None, "errorReport": "Failed to pull singularity image"}
    weblog(edge, run, "completed", metadata={"workflow": workflow})

    def failed() -> None:
        detail = run_detail(api, run)
        assert detail["status"] == "failed"
        assert detail["slurm_reason"] == "Failed to pull singularity image"

    eventually(failed, lag)


def test_task_counts_and_failed_tasks(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    name = f"{ns}-tasks"
    registration(api, name)
    batch = start_run(api, edge, name)
    run, tag = batch["run_name"], batch["jobs"][0]["sample_id"]
    trace = {"tag": tag, "process": "cmgd:kneaddata", "hash": "ab/cdef12"}
    weblog(edge, run, "process_completed", trace={**trace, "status": "FAILED", "exit": 137, "attempt": 1, "error_action": "RETRY"})
    weblog(edge, run, "process_completed", trace={**trace, "status": "COMPLETED", "exit": 0, "attempt": 2})

    def counted() -> None:
        detail = run_detail(api, run)
        assert detail["task_status_counts"] == {"FAILED": 1, "COMPLETED": 1}
        assert detail["failed_tasks"] == [
            {
                "process": "cmgd:kneaddata",
                "sample_id": tag,
                "exit_code": "137",
                "task_hash": "ab/cdef12",
                "attempt": 1,
                "error_action": "RETRY",
            }
        ]

    eventually(counted, lag)


def test_runs_filter_and_paging(api: httpx.Client, edge: httpx.Client, ns: str) -> None:
    name = f"{ns}-runs"
    registration(api, name)
    start_run(api, edge, name)
    page = validate(schemas.RunList, api.get("runs", params={"workflow_id": name, "limit": 1}))
    assert page.total >= 1 and page.limit == 1
    assert [r.workflow_id for r in page.runs] == [name]


def _assert_equal(actual: object, expected: object) -> None:
    assert actual == expected
