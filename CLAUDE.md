# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
just sync                  # uv sync --group dev

# Run server locally (requires SQLALCHEMY_URI in env)
just run                   # uvicorn with --reload on http://localhost:8000

# Tests and type checks
just test                  # pytest
just typecheck             # mypy src/nextflow_telemetry
just check                 # typecheck then test

# Migrations
just migrate               # alembic upgrade head

# Seed sample data
just seed                  # scripts/seed_from_tsv.py from ArtachoA_2021_sample.tsv

# Run dispatch daemon locally
just daemon                # nf-client daemon --config client-local.yaml --batch-size 10

# Docker
just up-all                # API + pgAdmin with external DB (uses SQLALCHEMY_URI from .env)
just up-db                 # postgres only
just down
```

Run a single test: `uv run pytest tests/test_api.py::test_name -v`

## Architecture

Two packages in one repo:

- **`src/nextflow_telemetry/`** — FastAPI server (the telemetry/orchestration backend)
- **`packages/nf_client/`** — CLI client (`nf-client`) that runs on HPC nodes to claim and submit jobs

### Server layout

- **`db.py`** — SQLAlchemy Core table definitions (`telemetry_tbl`, `samples_tbl`, `workflows_tbl`, `jobs_tbl`, `workflow_runs_tbl`, `dead_letter_tbl`). No ORM. Alembic imports `metadata` directly from here.
- **`models.py`** — Pydantic v2 request/response models only (no SQLAlchemy).
- **`config.py`** — Dataclass `Settings` loaded from env vars. `SQLALCHEMY_URI` auto-upgrades `postgresql://` → `postgresql+asyncpg://`.
- **`main.py`** — Creates `AsyncEngine`, instantiates services, wires routers via factory functions.
- **`routers/`** — Each module exports a `create_X_router(engine)` factory; the engine is injected so routers are testable without global state.
- **`services/`** — Business logic as `@dataclass` classes holding an `AsyncEngine`. All DB work is done with `async with engine.begin() as conn`.
- **`migrations/`** — Alembic async migrations. Name format: `YYYYMMDD_<hash>_<description>.py`.

### SQL style: SQLAlchemy Core vs raw `text()`

Both styles are used and that's deliberate. Pick by the SQL, not by file:

- **Core constructs** (`insert(tbl)`, `select(tbl.c.x)`, `update(tbl)`) for simple typed CRUD where you want column references and `.returning(*tbl.c)`. See `services/telemetry.py`, `routers/dispatch.py`.
- **Raw `text()`** when the query leans on Postgres-specific shapes Core handles awkwardly: `ON CONFLICT ON CONSTRAINT`, JSONB operators (`trace->>'process'`), `INSERT…SELECT` with cross joins, conditional WHERE chunks built by f-string, multi-aggregate analytical queries. See `services/process_metrics.py`, `services/cohort.py`, `services/reconcile.py`, `routers/task_logs.py`.

When using raw `text()`, **always pass user-derived values as `:name` bind parameters** — never f-string interpolation. F-strings are fine for static SQL fragments (e.g. optionally appending `AND foo = :foo` based on whether `foo` was supplied), but the values themselves go through bind params.

### Job lifecycle

```
pending → claimed → submitted → running → completed
                                        ↘ failed → dead_letter
```

- `pending`: created by `reconcile_jobs()` (cross-product of samples × active workflows)
- `claimed`: `POST /dispatch/batch` atomically selects jobs with `SELECT ... FOR UPDATE SKIP LOCKED` and creates a `workflow_run` record
- `submitted`: `POST /dispatch/submitted` confirms the executor accepted the job. Distinct from `running`: a SLURM job that's queued but not yet started sits in `submitted` for the queue-wait duration. The gap between `submitted` and `running` is the scheduler queue wait; surfacing it separately is what allows dashboards to distinguish "slow pipeline" from "slow cluster."
- `running`: set when Nextflow sends a `started` weblog event to `POST /telemetry` (transitions from `submitted` in the normal flow; defensively also accepts `claimed` for out-of-order events).
- `completed`: set when Nextflow sends `process_completed` for the `MARK_COMPLETE` sentinel process with `status=COMPLETED`
- On run `completed` event: `sweep_run_incomplete()` retries jobs within `max_retries` budget or routes to DLQ. Sweep covers `claimed`, `submitted`, *and* `running` jobs.

### nf-client

Config via YAML (`ClientConfig`). Profile (e.g. `anvil`, `alpine`) lives in the client config — the same workflow runs on different HPC systems. Submission modes: `local`, `slurm`, `pbs`, `lsf`. The submit script is rendered via Jinja2 from the path in `submission.template_path`. **All submit templates live in the repo's top-level `templates/` directory** (`submit_alpine.sh.j2`, `submit_anvil.sh.j2`, and `submit_example.sh.j2` — the `run_wrapper` reference). There is no separate template folder inside `packages/nf_client/`.

### Tests

Integration tests (`test_integration.py`, `test_e2e.py`) spin up a real Postgres via `testcontainers` — no mocking. The session-scoped fixture creates schema once; each test gets a fresh `AsyncEngine`. `test_api.py` uses unit-style mocking with `TELEMETRY_SKIP_DB_INIT=1`.

## Key gotchas

- **Pydantic v2**: `Optional[X]` fields require explicit `= None` default or the model will reject missing fields with a 422.
- **Weblog auth**: Nextflow's `-with-weblog` does not support sending auth tokens. The `/telemetry` endpoint is intentionally unauthenticated.
- **`MARK_COMPLETE` sentinel**: Per-sample completion is signalled by a Nextflow process named `MARK_COMPLETE` (checked via `.endswith("MARK_COMPLETE")`). The pipeline must emit this process with `status=COMPLETED` for a job to be marked complete.
- **Server restart after code changes**: uvicorn caches modules at startup — always restart after a `git pull` on a deployed instance.
