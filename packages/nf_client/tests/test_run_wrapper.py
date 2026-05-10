"""Unit tests for nf_client.run_wrapper.

Verifies the event sequence (wrapper_started → pre_nextflow → heartbeat →
wrapper_exited), the .nextflow.log upload, exit-code propagation, and
the must-not-fail-the-run telemetry policy.

Uses respx for HTTP mocking and a real subprocess (sleep / true / false)
to exercise the actual Popen path without faking it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from nf_client import run_wrapper


def _extract_event(call) -> dict:
    """Pull the `event` JSON out of a captured POST.

    httpx serialises `data={...}` as application/x-www-form-urlencoded when
    no files are attached, and as multipart/form-data when files are. Handle
    both shapes.
    """
    body = call.request.content.decode("utf-8", errors="replace")
    ctype = call.request.headers.get("content-type", "")

    if ctype.startswith("application/x-www-form-urlencoded"):
        return json.loads(parse_qs(body)["event"][0])

    # multipart — find the part with name="event"
    m = re.search(
        r'name="event"\r\n\r\n(.*?)\r\n--',
        body,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(f"no `event` field in body:\n{body[:400]}")
    return json.loads(m.group(1))


def _captured_event_types(route: respx.Route) -> list[str]:
    return [_extract_event(c)["type"] for c in route.calls]


@pytest.fixture
def telemetry_base() -> str:
    return "http://telemetry.test"


def test_event_sequence_for_successful_run(tmp_path, telemetry_base, monkeypatch):
    """A fast-completing nextflow surrogate produces wrapper_started → pre_nextflow → wrapper_exited."""
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / ".nextflow.log"
    log_path.write_text("nextflow log content\n")

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-test/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-test", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        rc = run_wrapper.main([
            "--run-name", "r-test",
            "--telemetry-url", telemetry_base,
            "--heartbeat-seconds", "60",  # won't fire — process exits in <1s
            "--nextflow-log", str(log_path),
            "--", "true",
        ])

    assert rc == 0
    assert route.call_count == 3
    types = _captured_event_types(route)
    assert types == ["wrapper_started", "pre_nextflow", "wrapper_exited"]


def test_failing_subprocess_exit_code_propagates(tmp_path, telemetry_base, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-fail/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-fail", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        rc = run_wrapper.main([
            "--run-name", "r-fail",
            "--telemetry-url", telemetry_base,
            "--", "false",
        ])

    assert rc == 1
    types = _captured_event_types(route)
    assert types[-1] == "wrapper_exited"
    last = _extract_event(route.calls[-1])
    assert last["exit_code"] == 1


def test_nextflow_log_attached_when_present(tmp_path, telemetry_base, monkeypatch):
    """The wrapper_exited POST should include the .nextflow.log file part."""
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / ".nextflow.log"
    log_path.write_text("important diagnostic content\n")

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-log/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-log", "type": "x", "nextflow_log_uploaded": True,
            })
        )

        run_wrapper.main([
            "--run-name", "r-log",
            "--telemetry-url", telemetry_base,
            "--nextflow-log", str(log_path),
            "--", "true",
        ])

    last_body = route.calls[-1].request.content.decode()
    # multipart body should contain both the file content and the field name
    assert "important diagnostic content" in last_body
    assert 'name="nextflow_log"' in last_body


def test_missing_nextflow_log_omits_attachment(tmp_path, telemetry_base, monkeypatch):
    monkeypatch.chdir(tmp_path)
    no_log = tmp_path / "does-not-exist.log"

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-nolog/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-nolog", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        rc = run_wrapper.main([
            "--run-name", "r-nolog",
            "--telemetry-url", telemetry_base,
            "--nextflow-log", str(no_log),
            "--", "true",
        ])

    assert rc == 0
    last_body = route.calls[-1].request.content.decode()
    # No file part — body is form-encoded, not multipart
    assert 'name="nextflow_log"' not in last_body


def test_telemetry_post_failure_does_not_fail_the_run(tmp_path, telemetry_base, monkeypatch):
    """A 500 from the server (or any HTTP error) must not propagate to the wrapper exit code."""
    monkeypatch.chdir(tmp_path)

    with respx.mock(base_url=telemetry_base) as rx:
        rx.post("/api/runs/r-flaky/event").mock(
            side_effect=httpx.ConnectError("boom — server unreachable")
        )

        rc = run_wrapper.main([
            "--run-name", "r-flaky",
            "--telemetry-url", telemetry_base,
            "--", "true",
        ])

    assert rc == 0  # subprocess exited 0, telemetry errors swallowed


def test_pre_nextflow_includes_wait_seconds_when_slurm_env_set(tmp_path, telemetry_base, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLURM_SUBMIT_TIME", "1700000000")
    monkeypatch.setenv("SLURM_JOB_START_TIME", "1700000123")
    monkeypatch.setenv("SLURM_JOB_ID", "999")

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-wait/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-wait", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        run_wrapper.main([
            "--run-name", "r-wait",
            "--telemetry-url", telemetry_base,
            "--", "true",
        ])

    # pre_nextflow is the 2nd POST
    pre = _extract_event(route.calls[1])
    assert pre["wait_seconds"] == 123
    # And wrapper_started carries SLURM_JOB_ID
    started = _extract_event(route.calls[0])
    assert started["slurm_job_id"] == "999"


def test_wait_seconds_omitted_when_slurm_env_absent(tmp_path, telemetry_base, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLURM_SUBMIT_TIME", raising=False)
    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-local/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-local", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        run_wrapper.main([
            "--run-name", "r-local",
            "--telemetry-url", telemetry_base,
            "--", "true",
        ])

    pre = _extract_event(route.calls[1])
    assert pre["wait_seconds"] is None


def test_log_truncation_keeps_tail_for_oversized_file(tmp_path):
    """_read_nextflow_log should keep the last max_bytes when the file is too big."""
    log_path = tmp_path / ".nextflow.log"
    head = b"H" * 1024
    tail = b"T" * 1024
    log_path.write_bytes(head + tail)

    out = run_wrapper._read_nextflow_log(log_path, max_bytes=1024)
    assert out == tail  # head dropped, tail kept


def test_heartbeats_fire_during_long_run(tmp_path, telemetry_base, monkeypatch):
    """A subprocess that runs longer than the heartbeat interval triggers heartbeat events."""
    monkeypatch.chdir(tmp_path)

    with respx.mock(base_url=telemetry_base) as rx:
        route = rx.post("/api/runs/r-hb/event").mock(
            return_value=httpx.Response(201, json={
                "run_name": "r-hb", "type": "x", "nextflow_log_uploaded": False,
            })
        )

        # sleep 0.4s wall, heartbeat every 0.1s → expect ≥2 heartbeats
        rc = run_wrapper.main([
            "--run-name", "r-hb",
            "--telemetry-url", telemetry_base,
            "--heartbeat-seconds", "0",  # 0 means "wait(0)" which is immediate, but Event.wait(0) is non-blocking and returns False
            "--", "sh", "-c", "sleep 0.4",
        ])

    assert rc == 0
    types = _captured_event_types(route)
    # We always have wrapper_started, pre_nextflow, wrapper_exited.
    # With heartbeat_seconds=0 (effectively a tight loop), we should see >0 heartbeats.
    n_hb = sum(1 for t in types if t == "heartbeat")
    assert n_hb >= 1, f"expected at least one heartbeat, got types={types}"
