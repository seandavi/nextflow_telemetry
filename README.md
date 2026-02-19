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

* id integer
* run_id 
* run_name
* timestamp (datetime)
* event (string)
* metadata (a dict or NULL)
* trace (a dict or NULL)

Either metadata OR trace are present, never both.

## Building and Running

Setting up the project has been simplified using the docker compose technology which sets up the Nextflow_Teletry_Api, a Postgres DB and PGadmin to monitor said database.

*You'll need to supply the following environment variables to the setup:*

* POSTGRES_DB=''
* POSTGRES_HOST=''
* POSTGRES_USER=''
* POSTGRES_PASSWORD=''

You can reference the env_template file here; [env_template](env)

- To set up all three;

```
docker compose --profile all up -d 
```
- To set up just the Nextflow_Telemetry_Api assuming one has their own custom database and doesn't need to monitor the DB;

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
just up-all    # start API + DB + pgAdmin
```

## Testing the API

After the Nextflow_Telemetry_Api container is in health state, to test that it works, run the command;

```
curl -X POST -H "Content-Type: application/json" -d '{"runId": "test123", "runName": "test_run", "event": "test_event", "utcTime": "2024-01-01T00:00:00", "metadata": {"workflow": {}}, "trace": {}}' http://localhost:8000/telemetry
```
With the DB monitor (PGadmin), in the Tables section, the metadata would have been created and success response would be seen in the container logs.

## Automated Tests

Run the API test suite locally with:

``` 
uv sync --group dev
uv run pytest
```

The tests are in `tests/test_api.py` and cover health-check behavior and telemetry ingest path execution.
