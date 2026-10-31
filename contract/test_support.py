"""support.py's guards and helpers; no target, no network."""

import json
from datetime import datetime

import httpx
import pytest

from contract.support import run_event, target_from_env

pytestmark = pytest.mark.offline

PRODUCTION = "https://nf-telemetry.seandavi.workers.dev"
WRITES = {"CONTRACT_ALLOW_WRITES": "1"}


def test_refuses_production_base() -> None:
    with pytest.raises(ValueError, match="production host"):
        target_from_env({**WRITES, "CONTRACT_BASE_URL": PRODUCTION})


def test_refuses_production_event_url() -> None:
    env = {**WRITES, "CONTRACT_BASE_URL": "http://127.0.0.1:8000", "CONTRACT_EVENT_URL": PRODUCTION}
    with pytest.raises(ValueError, match="production host"):
        target_from_env(env)


def test_refuses_non_loopback_without_disposable_host() -> None:
    with pytest.raises(ValueError, match="not loopback; set CONTRACT_DISPOSABLE_HOST=10.0.0.5"):
        target_from_env({**WRITES, "CONTRACT_BASE_URL": "http://10.0.0.5:8000"})


def test_allows_named_disposable_host() -> None:
    env = {**WRITES, "CONTRACT_BASE_URL": "http://10.0.0.5:8000", "CONTRACT_DISPOSABLE_HOST": "10.0.0.5"}
    assert target_from_env(env).allow_writes


def test_disposable_host_cannot_name_production() -> None:
    env = {**WRITES, "CONTRACT_BASE_URL": PRODUCTION, "CONTRACT_DISPOSABLE_HOST": "nf-telemetry.seandavi.workers.dev"}
    with pytest.raises(ValueError, match="production host"):
        target_from_env(env)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8000", "http://localhost:8787", "http://[::1]:8000"])
def test_allows_loopback(base_url: str) -> None:
    assert target_from_env({**WRITES, "CONTRACT_BASE_URL": base_url}).allow_writes


def test_read_only_against_production_is_allowed() -> None:
    assert not target_from_env({"CONTRACT_BASE_URL": PRODUCTION}).allow_writes


def test_requires_base_url() -> None:
    with pytest.raises(ValueError, match="CONTRACT_BASE_URL is required"):
        target_from_env({})


def test_run_event_utc_time_is_isoformat() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(201)

    with httpx.Client(base_url="http://x.invalid/", transport=httpx.MockTransport(handler)) as client:
        run_event(client, "run", {"type": "heartbeat"}, files={"nextflow_log": ("n.log", b"x", "text/plain")})
    body = seen[0].decode()
    event = json.loads(body.split('name="event"')[1].split("\r\n\r\n")[1].split("\r\n")[0])
    assert event["utc_time"].endswith("+00:00")
    assert datetime.fromisoformat(event["utc_time"]).utcoffset().total_seconds() == 0  # type: ignore[union-attr]
