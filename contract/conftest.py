import os
import secrets
from collections.abc import Iterator

import httpx
import pytest

from contract.support import Target, target_from_env


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "writes: mutates the target; needs CONTRACT_ALLOW_WRITES=1")
    config.addinivalue_line("markers", "fast_clock: needs CONTRACT_FAST_CLOCK_SECONDS")
    config.addinivalue_line("markers", "offline: needs no target")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if all("offline" in item.keywords for item in items):
        return
    try:
        target = target_from_env(os.environ)
    except ValueError as err:
        pytest.exit(str(err), returncode=2)
    no_writes = pytest.mark.skip(reason="writes need CONTRACT_ALLOW_WRITES=1")
    no_fast_clock = pytest.mark.skip(reason="timers need CONTRACT_FAST_CLOCK_SECONDS")
    for item in items:
        if "writes" in item.keywords and not target.allow_writes:
            item.add_marker(no_writes)
        if "fast_clock" in item.keywords and target.fast_clock_seconds is None:
            item.add_marker(no_fast_clock)


@pytest.fixture(scope="session")
def target() -> Target:
    return target_from_env(os.environ)


@pytest.fixture(scope="session")
def api(target: Target) -> Iterator[httpx.Client]:
    headers = {"Authorization": f"Bearer {target.token}"} if target.token else {}
    with httpx.Client(base_url=f"{target.base_url}/api/", headers=headers, timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def anon(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{target.base_url}/api/", timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def edge(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{target.event_url}/", timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def ns() -> str:
    return f"ct{secrets.token_hex(4)}"


@pytest.fixture(scope="session")
def lag(target: Target) -> float:
    return target.event_lag_seconds


@pytest.fixture(scope="session")
def fast_clock(target: Target) -> float:
    assert target.fast_clock_seconds is not None
    return target.fast_clock_seconds + 10
