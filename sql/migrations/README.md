# SQL Migrations

This repository uses plain SQL files with a lightweight runner (`scripts/migrate.py`).

## Principles
- Numbered files in lexical order: `NNN_description.sql`
- Immutable after apply (checksum is tracked)
- Prefer idempotent DDL (`IF NOT EXISTS`, guarded statements)
- Use `-- migrate: no-transaction` at the top for statements that cannot run in a transaction (for example `CREATE INDEX CONCURRENTLY`)

## Commands
- `just migration-status`
- `just migrate`

Both commands use `SQLALCHEMY_URI` from environment unless `--dsn` is provided directly to the script.

## Tracking table
Applied migrations are tracked in:
- `schema_migrations(version, filename, checksum, applied_at)`

## Workflow
1. Add new SQL file in `sql/migrations/` with next sequence number.
2. Run `just migration-status`.
3. Run `just migrate`.
4. Re-run `just migration-status` to verify applied state.
