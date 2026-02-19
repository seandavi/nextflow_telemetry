# Repository Guidelines

## Project Structure & Module Organization
- `nextflow_telemetry/`: FastAPI application code.
  - `main.py` defines routes (`/health`, `/telemetry`) and database table setup.
  - `models.py` contains request/telemetry models.
  - `config.py` reads runtime settings (for example `SQLALCHEMY_URI`).
- `nf_testing/`: Nextflow test workflow (`main.nf`) for event publishing checks.
- Root infra files: `docker-compose.yml`, `Dockerfile`, `init.sql`, `env` (template), `pyproject.toml`.
- Example payloads and sample data: `telemetry.json`.
- Use modern fastapi patterns including dependency injection, pydantic models for validation, and async/await for I/O operations, and routers for modular route organization as the codebase grows.

## Build, Test, and Development Commands
- `docker compose --profile all up -d`: Start API + Postgres + pgAdmin.
- `docker compose --profile api up -d`: Start API service only (external DB expected).
- `poetry install`: Install Python dependencies for local development.
- `poetry run uvicorn nextflow_telemetry.main:app --reload --host 0.0.0.0 --port 8000`: Run API locally.
- `curl -X POST ... http://localhost:8000/telemetry`: Send a sample telemetry event (see `README.md` for full payload).
- `poetry run mypy nextflow_telemetry`: Type-check core package.

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation.
- Use `snake_case` for functions/variables and module names.
- Keep route handlers small and move schema/model logic to `models.py`.
- Prefer explicit environment-driven configuration via `config.py`; avoid hardcoded connection details.
- Use sqlalchemy async sessions for database interactions, but do not use ORM models.
- Follow RESTful principles for API design (clear endpoints, appropriate HTTP methods, status codes).
- Use logging for important events and errors instead of print statements.

## Testing Guidelines
- Current coverage is mostly integration/manual.
- Minimum checks before a PR:
  - API health endpoint responds: `GET /health`.
  - Telemetry ingest writes to Postgres via `POST /telemetry`.
  - `poetry run mypy nextflow_telemetry` passes.
- Add new tests under a `tests/` directory using `test_*.py` naming when introducing non-trivial logic.

## Commit & Pull Request Guidelines
- Keep commits focused and imperative (examples from history: `feature: ...`, `add config`, `refactor`).
- Prefer one logical change per commit and mention impacted area (`api`, `docker`, `models`, etc.).
- PRs should include:
  - What changed and why.
  - Local verification steps/commands run.
  - Any schema/env changes (for example `.env` keys, DB init updates).
  - Sample request/response when API behavior changes.

## Security & Configuration Tips
- Do not commit secrets; use `.env` locally.
- Validate required DB variables (`POSTGRES_*`, `SQLALCHEMY_URI`) before starting services.
