set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Show a contextual command guide for this repository.
default: help

help:
	@echo "nextflow_telemetry command guide"
	@echo ""
	@echo "Setup and environment"
	@echo "  just sync          Install/update local dev dependencies (.venv) with uv."
	@echo "  just doctor        Show key tool versions (uv, python, docker compose)."
	@echo ""
	@echo "Local API workflows"
	@echo "  just run           Run FastAPI locally on http://localhost:8000 with reload."
	@echo "  just health        Check the local health endpoint and pretty-print status."
	@echo "  just sample-post   Send a minimal telemetry payload to the local API."
	@echo ""
	@echo "Quality checks"
	@echo "  just test          Run pytest suite (tests/test_api.py and future tests)."
	@echo "  just typecheck     Run mypy against nextflow_telemetry package."
	@echo "  just check         Run typecheck then tests (good pre-commit baseline)."
	@echo "  just ci            CI-equivalent local gate: sync dev deps, typecheck, tests."
	@echo ""
	@echo "Container workflows"
	@echo "  just up-all        Start API + Postgres + pgAdmin via docker compose profile 'all'."
	@echo "  just up-api        Start API profile only (expects external DB config in .env)."
	@echo "  just down          Stop and remove compose services/containers."
	@echo "  just logs          Tail API container logs for troubleshooting."

# Install project + dev dependencies into .venv using uv.
sync:
	uv sync --group dev

# Quick environment diagnostics for returning contributors.
doctor:
	@echo "uv: $$(uv --version)"
	@echo "python: $$(uv run python --version)"
	@echo "docker compose: $$(docker compose version --short)"

# Run API locally with auto-reload (expects SQLALCHEMY_URI in env).
run:
	uv run uvicorn nextflow_telemetry.main:app --reload --host 0.0.0.0 --port 8000

# Validate local API + DB connectivity.
health:
	curl -sS http://localhost:8000/health | uv run python -m json.tool

# Post sample telemetry event for smoke testing ingest path.
sample-post:
	curl -sS -X POST \
	  -H "Content-Type: application/json" \
	  -d '{"runId":"test123","runName":"test_run","event":"test_event","utcTime":"2024-01-01T00:00:00","metadata":{"workflow":{}},"trace":{}}' \
	  http://localhost:8000/telemetry | uv run python -m json.tool

# Run automated tests.
test:
	uv run pytest

# Static type checks.
typecheck:
	uv run mypy nextflow_telemetry

# Fast pre-commit quality gate.
check: typecheck test

# CI-equivalent local gate for reproducible verification.
ci:
	uv sync --group dev --frozen
	uv run mypy nextflow_telemetry
	uv run pytest

# Start full stack (API + DB + admin UI) via compose profiles.
up-all:
	docker compose --profile all up -d

# Start API service only.
up-api:
	docker compose --profile api up -d

# Stop compose services.
down:
	docker compose down

# Tail API service logs.
logs:
	docker compose logs -f nextflow_telemetry_api
