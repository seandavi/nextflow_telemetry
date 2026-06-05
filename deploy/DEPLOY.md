# Deployment Guide

> **Production is self-hosted on `onclappc02` (Docker Compose + Traefik).**
> The canonical, maintained runbook is **[`onclappc02/README.md`](onclappc02/README.md)** —
> topology, secrets, first-time setup, routine deploy, migrations, rollback.
> The Cloud Run / Cloud SQL / Firebase path below is **retired (May 2026)** and kept
> only for historical reference.

## Current production (summary)

```
HPC (Alpine/Anvil)        onclappc02 (140.226.4.71)
──────────────────        ─────────────────────────────────────────────
nf-client daemon   →  Traefik  →  nf_telemetry_api (compose)  →  pg_main (shared cluster, db nf_telemetry)
                                  nf_telemetry_frontend (nginx)
```

Routine deploy, from a checkout on `onclappc02`:

```bash
git checkout main && git pull
just deploy-onclappc02     # fetch secrets → docker compose build → up -d (recycle API)
curl -sS https://nf-telemetry.cancerdatasci.org/health   # verify 200
```

Migrations are **not** run by that target — run them from the host first (use
`@127.0.0.1` since `pg_main` only resolves inside Docker). See the runbook.

Secrets live in **GCP Secret Manager** (`cdsci-infra`); `.env` and `.env.secrets` are
derived/gitignored. See the runbook's Secrets section for the table and rotation steps.

---

## Historical: Cloud Run + Cloud SQL + Firebase (retired May 2026)

The original GCP stack. Retained for rollback context only while the GCP resources may
still exist (teardown status tracked in `onclappc02/README.md`). The `just build-api`,
`deploy-api`, and `deploy-frontend` recipes, `cloudrun.yaml`, and `cloudbuild.yaml` belong
to this path.

| Component | Resource |
|-----------|----------|
| API | Cloud Run service `nf-telemetry`, region `us-central1` |
| Frontend | Firebase Hosting, project `curatedmetagenomicdata` |
| Container registry | Artifact Registry `us-central1-docker.pkg.dev/curatedmetagenomicdata/nextflow-telemetry/` |
| Database | Cloud SQL `main` (Postgres 17), database `cmgd_prod` |
| DB secret | `nextflow-telemetry-db-uri` (GCP SM) → Cloud Run `SQLALCHEMY_URI` |

```bash
just build-api    # Cloud Build → Artifact Registry, tag api:<git-sha>
just deploy-api   # gcloud run deploy nf-telemetry with that image
just deploy-frontend   # vite build + firebase deploy
```

Roll back a Cloud Run revision (while it exists):
`gcloud run services update-traffic nf-telemetry --to-revisions=<rev>=100`.
