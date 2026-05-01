# Contributing to Nextflow Telemetry

Thank you for your interest in contributing! The following guidelines will help you get started quickly and ensure a smooth review process.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Reporting Issues](#reporting-issues)

---

## Code of Conduct

Please be respectful and considerate in all interactions. We follow the [Contributor Covenant](https://www.contributor-covenant.org/) code of conduct.

---

## Getting Started

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:

   ```bash
   git clone https://github.com/<your-username>/nextflow_telemetry.git
   cd nextflow_telemetry
   ```

3. Add the upstream remote so you can pull in future changes:

   ```bash
   git remote add upstream https://github.com/seandavi/nextflow_telemetry.git
   ```

---

## Development Setup

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node 18+ with npm (for the React dashboard)
- PostgreSQL (or Docker for the Compose stack)
- [just](https://github.com/casey/just) command runner

### Install dependencies

```bash
uv sync --group dev      # Python dev dependencies
cd frontend && npm install   # Frontend dependencies
```

### Start the development stack

```bash
just up-db      # Start PostgreSQL via Docker Compose
just migrate    # Run Alembic migrations
just run        # Start the API server with hot reload
```

For the frontend:

```bash
cd frontend && npm run dev
```

---

## Making Changes

1. Create a feature branch from `main`:

   ```bash
   git checkout -b feature/my-feature
   ```

2. Follow the project coding style:
   - PEP 8 with 4-space indentation.
   - `snake_case` for functions, variables, and module names.
   - Keep route handlers thin — move logic into `services/`.
   - Use SQLAlchemy Core (non-ORM) for database queries.
   - Prefer logging over `print` statements.

3. Keep commits focused and use an imperative subject line, e.g.:
   - `feature: add retry metrics endpoint`
   - `fix: handle missing trace field in telemetry payload`
   - `docs: update README quickstart`

---

## Testing

Run the full check suite before opening a PR:

```bash
just check      # mypy type-check + pytest
```

Or run individual steps:

```bash
uv run mypy nextflow_telemetry   # type checking
uv run pytest                    # unit/integration tests
```

Minimum checks:

- `GET /health` responds with 200.
- `POST /telemetry` successfully writes an event to Postgres.
- `uv run mypy nextflow_telemetry` reports no errors.

When adding non-trivial logic, please include corresponding tests under the `tests/` directory using the `test_*.py` naming convention.

---

## Submitting a Pull Request

1. Push your branch to your fork:

   ```bash
   git push origin feature/my-feature
   ```

2. Open a pull request against `main` on the upstream repository.

3. Fill in the PR template with:
   - **What changed and why.**
   - **Local verification steps** (commands you ran).
   - **Schema or environment changes** (new `.env` keys, DB migration files, etc.).
   - **Sample request/response** when API behavior changes.

4. A maintainer will review your PR. Please address any requested changes promptly.

---

## Reporting Issues

Please use [GitHub Issues](https://github.com/seandavi/nextflow_telemetry/issues) to report bugs or request features. When filing a bug report, include:

- Steps to reproduce the problem.
- Expected vs. actual behavior.
- Relevant log output or error messages.
- Your environment (OS, Python version, Docker version if applicable).
