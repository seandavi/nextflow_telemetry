# Performance Plan

## Target Endpoint Query Patterns
These SQL paths are currently the slowest and are the focus of optimization:
- `/metrics/processes/summary` (cards + top lists)
- `/metrics/processes/retries`
- `/metrics/processes/resources-by-attempt`
- `/metrics/processes/failures`
- `/metrics/processes/failure-signatures`

## Baseline (before index migration)
Measured with `EXPLAIN (ANALYZE, BUFFERS)` against production data (`window_days=180`):
- `summary_cards`: ~11.06s
- `summary_top_failures`: ~3.39s
- `retries_by_process`: ~5.36s
- `resources_by_attempt`: ~15.50s
- `failure_signatures`: ~1.10s

Common issue: parallel sequential scans over `telemetry` dominate cost.

## Optimization Strategy
1. Add targeted indexes for the shared filter path:
   - `event = 'process_completed'`
   - `utc_time >= now() - interval ...`
2. Add expression indexes on JSONB keys used by grouping/filtering:
   - `trace->>'process'`, `trace->>'status'`, `trace->>'attempt'`, `trace->>'exit'`
3. Apply indexes with `CREATE INDEX CONCURRENTLY` to minimize write blocking.
4. Re-run identical `EXPLAIN ANALYZE` queries and compare execution times.

## Success Criteria
- Clear reduction in p95 latency for heavy metrics endpoints.
- Query plans shift away from full-table seq scans for core paths.
