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
	@echo "Data and dispatch"
	@echo "  just seed          Register samples + workflow from ArtachoA_2021_sample.tsv."
	@echo "  just daemon        Run nf-client daemon: claim+run batches until no pending jobs."
	@echo ""
	@echo "Container workflows"
	@echo "  just up-all        Start API + pgAdmin via docker compose profile 'all' (external DB)."
	@echo "  just up-api        Start API profile only (uses SQLALCHEMY_URI from .env)."
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
	uv run uvicorn nextflow_telemetry.main:app --reload --host 0.0.0.0 --port 8000 --reload-dir src

# Validate local API + DB connectivity.
health:
	curl -sS http://localhost:8000/health | uv run python -m json.tool

# Post sample telemetry event for smoke testing ingest path.
sample-post:
	curl -sS -X POST \
	  -H "Content-Type: application/json" \
	  -d '{"runId":"test123","runName":"test_run","event":"test_event","utcTime":"2024-01-01T00:00:00","metadata":{"workflow":{}},"trace":{}}' \
	  http://localhost:8000/telemetry | uv run python -m json.tool

# Run Alembic migrations against the local DB (SQLALCHEMY_URI from env).
migrate:
	uv run alembic upgrade head

# Run automated tests.
test:
	uv run pytest

# Static type checks.
typecheck:
	uv run mypy src/nextflow_telemetry

# Fast pre-commit quality gate.
check: typecheck test

# CI-equivalent local gate for reproducible verification.
ci:
	uv sync --group dev --frozen
	uv run mypy nextflow_telemetry
	uv run pytest

# Start postgres only (for local dev without full stack).
up-db:
	docker compose --profile db up -d

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

# Seed samples + workflow from the ArtachoA_2021 TSV and reconcile jobs.
seed:
	uv run python scripts/seed_from_tsv.py

# Run nf-client daemon: claim and run batches of 10 until no pending jobs remain.
daemon:
	uv run nf-client daemon --config client-local.yaml --batch-size 10

# ── Cloud deployment ──────────────────────────────────────────────────────────
# Set GCP_PROJECT and REGION env vars (or export them) before using these.

GCP_PROJECT := env_var_or_default("GCP_PROJECT", "curatedmetagenomicdata")
REGION      := env_var_or_default("REGION", "us-central1")
AR_REPO     := "nextflow-telemetry"
IMAGE       := REGION + "-docker.pkg.dev/" + GCP_PROJECT + "/" + AR_REPO + "/api"

# Build and push the API image to Artifact Registry.
build-api:
	gcloud builds submit --tag {{IMAGE}}:latest --project {{GCP_PROJECT}} .

# Deploy the Cloud Run service from deploy/cloudrun.yaml (run build-api first).
deploy-api:
	gcloud run services replace deploy/cloudrun.yaml \
	  --region {{REGION}} \
	  --project {{GCP_PROJECT}}

# Build frontend and deploy to Firebase Hosting.
deploy-frontend:
	cd frontend && npm run build
	firebase deploy --only hosting

# Run Alembic migrations against a target DB.
# Usage: SQLALCHEMY_URI=postgresql://... just migrate-prod
migrate-prod:
	uv run alembic upgrade head
