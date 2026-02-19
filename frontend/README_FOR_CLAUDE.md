# Frontend Bootstrap Guide (React)

This branch (`claude/frontend-react`) is reserved for building a React frontend for telemetry dashboards.

## Goal
Build a frontend that supports:
- Near-real-time dashboard cards (recent run/process status).
- Process reliability views (failures, retry rates, failure signatures).
- Resource usage views by process and attempt.
- Filtering by `window_days`, `limit`, and minimum sample thresholds.

## Backend Endpoints to Use
- `GET /health`
- `GET /metrics/processes/summary`
- `GET /metrics/processes/retries`
- `GET /metrics/processes/resources-by-attempt`
- `GET /metrics/processes/failures`
- `GET /metrics/processes/failure-signatures`

Use query params where applicable:
- `window_days` (e.g., 7, 30, 180)
- `limit`
- `min_samples`

## Recommended Stack
- React + TypeScript
- Vite
- React Router (if multi-page views are desired)
- TanStack Query for API data fetching/caching
- Recharts (or ECharts) for charts

## Suggested Directory Layout
- `frontend/app/` -> React app root
- `frontend/app/src/api/` -> typed fetch wrappers for metrics endpoints
- `frontend/app/src/components/` -> reusable UI components
- `frontend/app/src/pages/` -> dashboard/reporting screens
- `frontend/app/src/types/` -> response interfaces

## Minimum Deliverables (v1)
1. A main dashboard page showing:
   - Summary cards from `/metrics/processes/summary`
   - Top failing processes
   - Top retried processes
   - Top failure exit codes
2. A retries page using `/metrics/processes/retries`.
3. A resources page using `/metrics/processes/resources-by-attempt`.
4. A failures page using `/metrics/processes/failures` and `/failure-signatures`.
5. Shared global filters (`window_days`, `limit`, `min_samples`) reflected in URL params.

## UX Notes
- Show loading, empty, and error states for each panel.
- Keep units explicit (percentages, GB, counts).
- Indicate latest data timestamp from API responses.

## Validation
- App runs locally with one command (`npm run dev` or `pnpm dev`).
- Basic lint/typecheck scripts available.
- README in `frontend/app/` includes setup and run steps.
