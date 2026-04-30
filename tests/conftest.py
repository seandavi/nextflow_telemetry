"""Shared pytest fixtures.

Integration tests use a session-scoped testcontainers postgres instance.
Schema is created once at session start. Each integration test gets its own
FastAPI TestClient wired to the shared DB.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Generator

import pytest
from testcontainers.postgres import PostgresContainer


# ---------------------------------------------------------------------------
# Postgres container — one per test session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def db_asyncpg_url(postgres_container: PostgresContainer) -> str:
    """asyncpg connection URL for the test database."""
    return postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session", autouse=True)
def create_schema(db_asyncpg_url: str) -> None:
    """Create all tables once before any tests run."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from nextflow_telemetry.db import metadata

    async def _create():
        engine = create_async_engine(db_asyncpg_url)
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


# ---------------------------------------------------------------------------
# App-level fixtures (unit tests — mocked DB)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_module(monkeypatch: pytest.MonkeyPatch):
    """FastAPI app with DB init skipped (unit tests)."""
    monkeypatch.setenv("TELEMETRY_SKIP_DB_INIT", "1")
    if "nextflow_telemetry.main" in sys.modules:
        module = importlib.reload(sys.modules["nextflow_telemetry.main"])
    else:
        module = importlib.import_module("nextflow_telemetry.main")
    return module


# ---------------------------------------------------------------------------
# Integration fixtures — real DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def integration_client(db_asyncpg_url: str):
    """TestClient wired to the testcontainers postgres DB."""
    os.environ["SQLALCHEMY_URI"] = db_asyncpg_url
    os.environ["TELEMETRY_SKIP_DB_INIT"] = "1"
    for mod_name in list(sys.modules):
        if mod_name.startswith("nextflow_telemetry"):
            del sys.modules[mod_name]
    module = importlib.import_module("nextflow_telemetry.main")

    from fastapi.testclient import TestClient
    with TestClient(module.app) as client:
        yield client, module


@pytest.fixture()
def db_url(db_asyncpg_url: str) -> str:
    return db_asyncpg_url
