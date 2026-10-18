# Metadata source catalog

Owns source/asset descriptors, their change history, and immutable snapshots of
original acquired bytes. Independent of processing identity, job dispatch and
lake sample-value loads. See `docs/source-catalog.md` and issue #333.

- `models.py`: manifest validation. Unknown access/license must stay unknown.
- `store.py`: public `SourceCatalog` interface, SQLAlchemy Core over Postgres.
  Descriptor writes and captures serialize on the source row. Preview writes
  nothing. Exact repeats must not churn audit rows or timestamps.
- `archive.py`: private local whole-file archive. Preserve full original bytes,
  including compressed files; never prefilter or execute upstream data.
- `cli.py`: `nf-meta sources` adapter. Must not initialize the lake or FastAPI.
- Tables: `db.py`; changes need an Alembic migration and disposable DB tests.

Tests: `uv run pytest tests/test_source_catalog.py`; types:
`uv run mypy src/nextflow_telemetry`. Existing `nf-meta load/views` must keep
working. No schema auto-creation, production migration, source auto-enrollment,
public snapshot upload, or credentials in manifests. Use examples with synthetic
or public descriptors; never copy private spreadsheet details into this repo.
