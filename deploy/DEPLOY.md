# Deployment Guide

## Architecture

```
HPC (Anvil)          GCP
──────────           ───────────────────────────────────────────
nf-client daemon  →  Cloud Run (nf-telemetry)  →  Cloud SQL (main instance)
                     Firebase Hosting (frontend)
```

| Component     | Resource                                                              |
|---------------|-----------------------------------------------------------------------|
| API           | Cloud Run service `nf-telemetry`, region `us-central1`               |
| API URL       | https://nf-telemetry-819875667022.us-central1.run.app                |
| Frontend      | Firebase Hosting, project `curatedmetagenomicdata`                   |
| Container registry | Artifact Registry `us-central1-docker.pkg.dev/curatedmetagenomicdata/nextflow-telemetry/` |
| Database      | Cloud SQL instance `main` (Postgres 17), database `cmgd_prod`        |
| Service account | `nextflow-telemetry-api@curatedmetagenomicdata.iam.gserviceaccount.com` |

## Secrets

All secrets are stored in **GCP Secret Manager** under project `curatedmetagenomicdata`.

| Secret name                  | What it contains              | Used by         |
|------------------------------|-------------------------------|-----------------|
| `nextflow-telemetry-db-uri`  | Full `SQLALCHEMY_URI` (asyncpg connection string to Cloud SQL `cmgd_prod`) | Cloud Run via `SQLALCHEMY_URI` env var |

To update the DB URI (e.g. password rotation):
```bash
echo -n "postgresql+asyncpg://user:pass@host/cmgd_prod" | \
  gcloud secrets versions add nextflow-telemetry-db-uri --data-file=- \
  --project=curatedmetagenomicdata
# Then redeploy Cloud Run to pick up the new version:
just deploy-api
```

The local `.env.prod` file mirrors the secret for running migrations locally — keep them in sync.
`.env.prod` is gitignored and must never be committed.

## Deploy API

```bash
just build-api    # builds via Cloud Build, tags image as api:<git-sha>
just deploy-api   # deploys that git-sha-tagged image to Cloud Run
```

Images are tagged with the short git SHA (e.g. `api:7822a49`) rather than `latest`.
This guarantees Cloud Run always creates a new revision on deploy, and makes it trivial
to roll back: `gcloud run services update-traffic nf-telemetry --to-revisions=<rev>=100`.

Cloud Build is used (not local Docker) to ensure the image is always `linux/amd64` — required by Cloud Run.

## Run migrations

Against production:
```bash
export $(grep SQLALCHEMY_URI .env.prod | xargs)
uv run alembic upgrade head
```

Migrations must be run manually before deploying a new API version that adds schema changes.

## Deploy frontend

```bash
cd frontend && npm run build
cd deploy && firebase deploy --only hosting
```

## nf-client on Anvil

The daemon runs on the Anvil HPC head node and talks to the Cloud Run API URL. Config at:
`/anvil/scratch/x-seandavi/nf_worker/client-anvil.yaml`

The `server_url` and `weblog_url` fields must point to the Cloud Run service URL above.

## First-time GCP setup (already done — for reference)

```bash
# Enable APIs
gcloud services enable secretmanager.googleapis.com artifactregistry.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com --project=curatedmetagenomicdata

# Create Artifact Registry repo
gcloud artifacts repositories create nextflow-telemetry \
  --repository-format=docker --location=us-central1 --project=curatedmetagenomicdata

# Create service account
gcloud iam service-accounts create nextflow-telemetry-api \
  --display-name="Nextflow Telemetry API" --project=curatedmetagenomicdata

# Grant service account access to the DB secret
gcloud secrets add-iam-policy-binding nextflow-telemetry-db-uri \
  --member="serviceAccount:nextflow-telemetry-api@curatedmetagenomicdata.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=curatedmetagenomicdata

# Create the secret (first time)
echo -n "postgresql+asyncpg://..." | gcloud secrets create nextflow-telemetry-db-uri \
  --data-file=- --project=curatedmetagenomicdata
```
