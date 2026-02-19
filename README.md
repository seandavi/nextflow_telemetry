# Nextflow telemetry

Nextflow can publish events to a web log. This project
acts as a server/api endpoint for those events.

## Data model

The data are modeled very simply as an event stream. Two
types of events are produced:

1. metadata
2. trace

The events are stored in a single postgresql table with 
the following columns:

* id uuid
* run_id uuid
* run_name
* utc_time (timestamp with time zone)
* event (string)
* metadata_ (jsonb, nullable)
* trace (a dict or NULL)

Either metadata OR trace are present, never both.

## Building and Running

Docker compose in this repository runs the Nextflow telemetry API and pgAdmin. The database is expected to be an external/dedicated Postgres service.

*You'll need to supply the following environment variables to the setup:*

* SQLALCHEMY_URI=''
* PGADMIN_DEFAULT_EMAIL=''
* PGADMIN_DEFAULT_PASSWORD=''

You can reference the env_template file here; [env_template](env)

- To start API + pgAdmin:

```
docker compose --profile all up -d 
```
- To start just the Nextflow_Telemetry_Api (external DB only):

```
docker compose --profile api up -d
```

## Command Runner (just)

This repository includes a `justfile` with task-oriented commands and context notes.

```
just help
```

Common workflows:

```
just sync      # install dev dependencies with uv
just run       # run API locally with reload
just check     # run typecheck + tests
just ci        # CI-equivalent local gate (sync --frozen + mypy + pytest)
just up-all    # start API + pgAdmin (external DB)
```

## Testing the API

After the Nextflow_Telemetry_Api container is in health state, to test that it works, run the command;

```
curl -X POST -H "Content-Type: application/json" -d '{"runId": "test123", "runName": "test_run", "event": "test_event", "utcTime": "2024-01-01T00:00:00", "metadata": {"workflow": {}}, "trace": {}}' http://localhost:8000/telemetry
```
With the DB monitor (PGadmin), in the Tables section, the metadata would have been created and success response would be seen in the container logs.

## Metrics Endpoints

Process-level metrics endpoints are available under `/metrics/processes`:

- `/metrics/processes/summary`
- `/metrics/processes/retries`
- `/metrics/processes/resources-by-attempt`
- `/metrics/processes/failures`
- `/metrics/processes/failure-signatures`

Example:

```
curl "http://localhost:8000/metrics/processes/summary?window_days=180&limit=5"
```

## Automated Tests

Run the API test suite locally with:

``` 
uv sync --group dev
uv run pytest
```

Tests are in `tests/` and cover health-check behavior, telemetry ingest path execution, and process-metrics router behavior.
