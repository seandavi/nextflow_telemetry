# Deployment Guide

The API runs on **Cloud Run** (GCP project `curatedmetagenomicdata`, region `us-east4`)
against **PlanetScale Postgres** (database `nf-telemetry`, `gcp-us-east4`, PS-5 single node,
created from the Cloudflare dashboard). It follows ADR-0014 (amends ADR-0013). Daemons and the
dashboard use the service's `run.app` URL as the origin; there is no custom domain.

```
HPC daemons ─┐
dashboard ───┼──▶ Cloud Run  nf-telemetry  (min=max=1, CPU always allocated)  ──▶ PlanetScale  nf-telemetry
Worker ──────┘            │                                                            (control plane, submissions,
 (R2 event sink)          └──▶ R2 (task logs, event archive, retired-job archive)       users, curated tables)

onclappc02:  pg_main (source-catalog tables only) · nf-meta · nf-etl · lake · dashboard container
```

The dashboard container and the lake tooling still run on `onclappc02`; see
[`onclappc02/README.md`](onclappc02/README.md) for those. The API is no longer deployed there.

The service must stay at one instance: it runs the deadline sweep, the R2 event ingester, the
history warm-up and the daily retired-job archive in-process.

## Image

`.github/workflows/image.yml` builds the repository `Dockerfile` and pushes
`us-east4-docker.pkg.dev/curatedmetagenomicdata/nextflow-telemetry/api:<full commit sha>` on every
push to `main`, authenticating with workload identity federation (no JSON key). It needs two
repository **variables** (an operator creates them; nothing is a secret):

| Variable | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>` |
| `GCP_IMAGE_PUSHER_SERVICE_ACCOUNT` | service account the provider may impersonate, with `roles/artifactregistry.writer` on the `nextflow-telemetry` repository |

The provider's attribute condition should be limited to `seandavi/nextflow_telemetry`.

## Configuration

Secrets come from **GCP Secret Manager in `cdsci-infra`** (the source of truth) and are mounted as
Cloud Run secret environment variables; the infrastructure definition lives in monode.

| Env var | Source | Notes |
|---|---|---|
| `SQLALCHEMY_URI` | GSM secret holding the PlanetScale URI (name set in monode) | `postgresql://<user>:<password>@<host>:5432/<db>?ssl=require`. PlanetScale requires TLS and asyncpg takes `ssl=require`; **`sslmode=require` fails** (`connect() got an unexpected keyword argument 'sslmode'`). Direct port 5432 only: if the pooled port 6432 (PgBouncer) is ever used, the engine needs `statement_cache_size=0` in `connect_args`; that is not configured. |
| `DISPATCH_TOKEN` | `cdsci-nf-telemetry-v2-api-token` | Bearer token for every control-plane write. |
| `V2_API_TOKEN` | `cdsci-nf-telemetry-v2-api-token` | Submission write-through to the v2 Worker, until it is retired. |
| `OPERATOR_TOKEN` | `cmgd-api-operator-token` | Operator/CI auth for `POST /submissions`. |
| `OAUTH_CLIENT_ID` | `cancerdatasci-oauth-client-id` | Login. The service's `run.app` `/auth/callback` must be added to the OAuth client's redirect URIs. |
| `OAUTH_CLIENT_SECRET` | `cancerdatasci-oauth-client-secret` | |
| `SESSION_SECRET` | `cmgd-api-session-secret` | |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | `cdsci-r2-account-id`, `cdsci-r2-access-key-id`, `cdsci-r2-secret-access-key` | Event archive, task-log blobs, retired-job archive, event ingester. |

Plain (non-secret) variables:

| Env var | Value |
|---|---|
| `BLOB_STORE_URL` | `r2://nf-telemetry` (required; the service refuses to start without it) |
| `OAUTH_REDIRECT_URI` | `<run.app url>/auth/callback` |
| `CORS_ORIGINS` | the dashboard origin(s), comma-separated (default `*`) |
| `FRONTEND_URL` | where to send the user after login |
| `DEADLINE_SWEEP_ENABLED`, `RETIRED_ARCHIVE_ENABLED`, `EVENT_INGEST_ENABLED` | `1` at cutover (they default to `0`) |
| `EVENT_INGEST_URL` | `r2://nf-telemetry` (required when the ingester is enabled) |
| `V2_API_URL` | defaults to the Worker's `/api`; leave unset until the Worker is retired |

Everything else (`CLAIM_TTL_MINUTES`, `LIVENESS_MINUTES`, `EVENTS_ARCHIVE_URL`, ...) has a default in
`src/nextflow_telemetry/config.py`. `SESSION_COOKIE_DOMAIN` stays empty (host-only cookie) on a
`run.app` origin.

Cloud Run flags that matter: `--port 8000` (the image listens on 8000, not Cloud Run's default
`$PORT`), `--min-instances 1 --max-instances 1`, `--no-cpu-throttling`, `--memory 4Gi --cpu 2`,
startup/liveness probe `GET /health`.

## Memory sizing

The history tier (`services/event_archive.py`) loads every `*.ndjson.gz` under
`EVENTS_ARCHIVE_URL` into an in-memory DuckDB at startup, then reads only new files.

Measured 2026-11-11 with the image built from this branch, `docker run --cpus 2`, against a copy
of the real archive (42,837 objects, 619 MB on disk; the archive had ~28k objects when the plan was
written and grows daily), read from local disk rather than R2:

| `--memory` | warm-up | peak RSS (`VmHWM`) | cgroup peak | steady state (anon) |
|---|---|---|---|---|
| 8g | 32 s | 2.85 GB | 2.88 GB | 2.04 GB |
| 4g | 32 s | 3.08 GB | 3.11 GB | 1.96 GB |
| 2g | 36 s | 2.13 GB | 2.15 GB (the limit) | 1.39 GB |

2 GiB survived only because DuckDB spilled and the kernel dropped page cache; it ran at the limit
and is not a safe size. **4 GiB leaves ~0.9 GB above the measured peak**; revisit when the archive
doubles. DuckDB sizes itself from the cgroup, so the memory limit must be set on the service. It also
sizes its thread pool from the visible CPUs: an unconstrained run on a 64-core host failed in the
load with `OutOfMemoryException` at 4g, so the CPU limit matters as well.

## Deploy

1. Merge to `main`; wait for the `image` workflow to push `api:<sha>`.
2. Run migrations for the new image **first** (see Jobs): `alembic upgrade head`.
3. Roll the service:

   ```bash
   just deploy-api <sha>      # gcloud run services update nf-telemetry --image ...api:<sha>
   curl -sS https://<run.app url>/health
   ```

   The first request after a deploy waits on, or retries, the ~30 s history warm-up; the logs show
   `event_archive.warmed`.

## Rollback

Code: `just deploy-api <previous sha>`. With min=max=1 there is no traffic split to manage.

Schema: migrations are forward-only in practice; `alembic downgrade <rev>` (as a job) is available, but
check the revision's downgrade before relying on it. For data, PlanetScale's own backups apply (see ADR-0014).

## Jobs

Operator commands that need the database run as **Cloud Run Jobs from the same image** (PlanetScale
is not reachable from `onclappc02`: outbound 5432/6432 is blocked). They are confirmed to start from
the image with only `SQLALCHEMY_URI` set (`BLOB_STORE_URL` is not needed):

| Job command | Args |
|---|---|
| `alembic` | `upgrade head` |
| `python` | `-m nextflow_telemetry.v2_import <snapshot_dir> [--replace]` |
| `python` | `-m nextflow_telemetry.v2_replay <before_dir> <after_dir> <events_dir>` |

```bash
gcloud run jobs execute <job> --region us-east4 --project curatedmetagenomicdata --wait \
  --args=upgrade,head                # job defined with --command=alembic
```

`v2_import` and `v2_replay` read **local directories** (a `v2-export` copy and the events directory),
so a job running them needs those mounted (for example a GCS bucket volume) or copied in before the
command. `v2_replay` refuses the production database name; `v2_import --replace` is for scratch and
staging databases only.

## Source-catalog tables

The `metadata_source*` tables and `nf-meta` stay on `pg_main` (ADR-0014). The service never reads
them and submissions carries `source_snapshot_*` as plain columns with no foreign key (revision
`7c4d2e90`). Alembic revision `e9fa0b1c` still creates the four empty tables in any database it
migrates, including PlanetScale; they are unused there. `nf-meta` keeps its own `SQLALCHEMY_URI`
pointing at `pg_main`.
