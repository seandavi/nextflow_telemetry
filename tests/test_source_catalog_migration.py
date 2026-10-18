"""Exercise the new Alembic DDL, not just metadata.create_all()."""
import asyncio
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def test_source_catalog_upgrade_and_downgrade(db_url):
    migration = importlib.import_module(
        "nextflow_telemetry.migrations.versions.20261018_e9fa0b1c_metadata_source_catalog")
    schema = "source_catalog_migration_test"
    expected = {"metadata_sources", "metadata_source_assets", "metadata_source_snapshots",
                "metadata_source_events"}

    def exercise(con):
        with Operations.context(MigrationContext.configure(con)):
            migration.upgrade()
            assert set(inspect(con).get_table_names(schema=schema)) == expected
            snapshot = inspect(con).get_columns("metadata_source_snapshots", schema=schema)
            assert {c["name"] for c in snapshot} >= {
                "sha256", "artifact_uri", "source_descriptor", "asset_descriptor"}
            migration.downgrade()
            assert inspect(con).get_table_names(schema=schema) == []
            migration.upgrade()
            assert set(inspect(con).get_table_names(schema=schema)) == expected

    async def scenario():
        engine = create_async_engine(db_url)
        try:
            async with engine.begin() as con:
                await con.execute(text(f"CREATE SCHEMA {schema}"))
                await con.execute(text(f"SET LOCAL search_path TO {schema}"))
                await con.run_sync(exercise)
                await con.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        finally:
            await engine.dispose()
    asyncio.run(scenario())
