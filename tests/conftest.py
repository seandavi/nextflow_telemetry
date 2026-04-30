"""Shared pytest fixtures for integration tests using real postgres via testcontainers."""
from __future__ import annotations

import importlib
import os
import sys
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """Spin up a throwaway postgres instance for the test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def db_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )


@pytest_asyncio.fixture(scope="session")
async def db_engine(db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(db_url, echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async_session = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# App-level fixtures (unit tests with mocked DB)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_module(monkeypatch: pytest.MonkeyPatch):
    """Load the FastAPI app with DB init skipped (for unit tests)."""
    monkeypatch.setenv("TELEMETRY_SKIP_DB_INIT", "1")
    if "nextflow_telemetry.main" in sys.modules:
        module = importlib.reload(sys.modules["nextflow_telemetry.main"])
    else:
        module = importlib.import_module("nextflow_telemetry.main")
    return module


@pytest.fixture()
def integration_app(db_url: str):
    """Load the FastAPI app wired to the testcontainers postgres instance."""
    os.environ["SQLALCHEMY_URI"] = db_url
    os.environ["TELEMETRY_SKIP_DB_INIT"] = "0"
    for mod_name in list(sys.modules):
        if mod_name.startswith("nextflow_telemetry"):
            del sys.modules[mod_name]
    module = importlib.import_module("nextflow_telemetry.main")
    return module
