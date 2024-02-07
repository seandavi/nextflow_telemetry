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

## Building

```
docker build --platform=linux/amd64 --tag gcr.io/$PROJECT/$IMAGE --push
```

## Running

You'll need to supply the following environment variables to the setup:

* SQLALCHEMY_URI='postgresql://user:pass@host/dbname'

Then:

```
docker run -p 8000:8000 -e SQLALCHEMY_URI=.... gcr.io/$PROJECT/$IMAGE
```
