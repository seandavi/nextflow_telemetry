import httpx
import pytest

from contract.support import (
    claim,
    eventually,
    job_summary,
    registration,
    run_detail,
    run_event,
    start_run,
)

pytestmark = [pytest.mark.writes, pytest.mark.fast_clock]


def test_claim_expiry(api: httpx.Client, ns: str, fast_clock: float) -> None:
    name = f"{ns}-claimttl"
    wf, _ = registration(api, name)
    batch = claim(api, name)
    assert batch is not None
    run = batch["run_name"]

    def expired() -> None:
        detail = run_detail(api, run)
        assert detail["status"] == "expired"
        assert detail["job_status_counts"] == {}
        summary = job_summary(api, wf["id"])
        assert (summary["claimed"], summary["pending"]) == (0, 1)

    eventually(expired, fast_clock)


def _silent_run(api: httpx.Client, edge: httpx.Client, name: str, max_retries: int) -> tuple[int, str]:
    wf, _ = registration(api, name, max_retries=max_retries)
    run = start_run(api, edge, name)["run_name"]
    assert run_event(edge, run, {"type": "heartbeat"}).status_code == 201
    return wf["id"], run


def test_liveness_expiry_dead_letters(api: httpx.Client, edge: httpx.Client, ns: str, fast_clock: float) -> None:
    pk, run = _silent_run(api, edge, f"{ns}-livedlq", max_retries=0)

    def dead() -> None:
        detail = run_detail(api, run)
        assert detail["status"] == "failed"
        assert "presumed dead" in (detail["slurm_reason"] or "")
        assert job_summary(api, pk)["dead_letter"] == 1

    eventually(dead, fast_clock)


def test_liveness_expiry_retries(api: httpx.Client, edge: httpx.Client, ns: str, fast_clock: float) -> None:
    pk, run = _silent_run(api, edge, f"{ns}-liveretry", max_retries=1)

    def retried() -> None:
        assert run_detail(api, run)["status"] == "failed"
        summary = job_summary(api, pk)
        assert (summary["pending"], summary["dead_letter"]) == (1, 0)

    eventually(retried, fast_clock)
