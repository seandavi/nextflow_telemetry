# Self-host on onclappc02

Production compose for the telemetry API + frontend, served via the
existing Traefik on `cancerdatasci.org`. The Postgres backend lives in
the shared `pg_ducklake_18` cluster (see
`monode/infrastructure/compose/pg_ducklake_stack/`), one database per
app — this app owns the `nf_telemetry` database on host port 5432.

## Files

- `docker-compose.yml` — API + frontend, both labelled for Traefik
- `Dockerfile.frontend` — multi-stage build (node → nginx:alpine)
- `nginx.conf` — SPA config with sane caching headers
- `.env.example` — fill in and copy to `.env`

## State machine (current as of 2026-05-11)

| Phase | Status |
|---|---|
| 1. Stable API URL (`nf-telemetry.cancerdatasci.org`) | ✅ done (Cloud Run domain mapping + managed cert) |
| 2. Bootstrap new DB on onclappc02 | ✅ done (role, schema, API+frontend containers up internally) |
| 3. DNS flip Cloud Run → onclappc02 | ⏳ this document's main subject |
| 4. Tear down Cloud Run + Cloud SQL | ⏳ after Phase 3 stabilizes |

## Current backend state on onclappc02

```
pg_ducklake_18         host:5432  → /data/postgres_ducklake/    (shared cluster, db nf_telemetry)
nf_telemetry_api       proxy net  → host.docker.internal:5432/nf_telemetry
nf_telemetry_frontend  proxy net  → built with VITE_API_URL=https://nf-telemetry.cancerdatasci.org
traefik                host net   → routes both hostnames by Docker labels
```

(Note: `pg_duckdb_18` on host port 5433 is a legacy cluster kept up
during the ducklake migration; the API does **not** talk to it.)

Internal smoke (works today):

```sh
curl -sSk -H "Host: nf-telemetry.cancerdatasci.org" https://localhost/health
curl -sSk -H "Host: cmgd.cancerdatasci.org" https://localhost/ | head
```

External DNS still points at Cloud Run, so external `https://nf-telemetry.cancerdatasci.org`
goes to the legacy stack.

## Secrets

This deploy intentionally has **no app-managed secrets files** beyond
`.env` (which is gitignored and only used as a runtime artefact, not a
source of truth). All Postgres credentials live in GCP Secret Manager
under the `cdsci-infra` project. Code, CI, and humans all read from
there — never invent a parallel copy in a config file, a chat thread,
or a password manager note.

| Secret | Role rotated | Consumed by |
|---|---|---|
| `cdsci-postgres-admin-password` | `postgres` superuser on `pg_ducklake_18` | cluster admins; not used by app containers |
| `cdsci-nf-telemetry-db-password` | `nf_telemetry` login role | this deploy's `.env` (composed into `SQLALCHEMY_URI`) |

**Naming convention** (see also the cdsci-infra Terraform):

- `cdsci-*` — shared cluster / cross-app infra secrets.
- `cdsci-<app>-db-password` — per-app Postgres role passwords on the
  shared ducklake cluster. One role per app, one secret per role.

**Reading the password into the runtime `.env`:**

```sh
PW=$(gcloud secrets versions access latest \
  --secret=cdsci-nf-telemetry-db-password --project=cdsci-infra)
# Use $PW to substitute into deploy/onclappc02/.env's SQLALCHEMY_URI.
```

**Rotating** (no application downtime if you reload env + restart cleanly,
but expect ~30s of 503 during the container recreate):

```sh
# 1. Generate + write new version to SM
python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))" \
  | gcloud secrets versions add cdsci-nf-telemetry-db-password \
      --project=cdsci-infra --data-file=-

# 2. Apply to the Postgres role (use psql heredoc; never pass via -c with :'var'):
PW=$(gcloud secrets versions access latest \
  --secret=cdsci-nf-telemetry-db-password --project=cdsci-infra)
PW_ESC=$(printf %s "$PW" | sed "s/'/''/g")
docker exec -i pg_ducklake_18 psql -U postgres -v ON_ERROR_STOP=1 <<SQL
ALTER ROLE nf_telemetry WITH PASSWORD '$PW_ESC';
SQL

# 3. Rewrite deploy/onclappc02/.env with the new $PW, then:
cd deploy/onclappc02 && docker compose up -d --force-recreate nf_telemetry_api
```

If the API can't reach the DB (503 from `/health`), the first place to
look is whether `.env` is in sync with the latest secret version.

## First-time setup (already done; recorded for reproducibility)

1. **Database + role**, inside the new shared cluster. Password is
   generated and stored in GCP Secret Manager — *not* chosen ad-hoc:
   ```sh
   # Generate + store in SM (one-time per role):
   python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))" \
     | gcloud secrets create cdsci-nf-telemetry-db-password \
         --project=cdsci-infra --replication-policy=automatic --data-file=-

   # Read it back and create the role + database:
   PW=$(gcloud secrets versions access latest \
     --secret=cdsci-nf-telemetry-db-password --project=cdsci-infra)
   PW_ESC=$(printf %s "$PW" | sed "s/'/''/g")
   docker exec -i pg_ducklake_18 psql -U postgres -v ON_ERROR_STOP=1 <<SQL
   CREATE ROLE nf_telemetry WITH LOGIN PASSWORD '$PW_ESC';
   CREATE DATABASE nf_telemetry OWNER nf_telemetry;
   SQL
   ```
   (Do **not** `CREATE EXTENSION pg_duckdb` in this DB — the app uses plain
   Postgres; pg_duckdb hooks would only get in the way.)

2. **`.env`** — copy `.env.example` to `.env` (already gitignored), fetch
   the password from SM, and substitute it into `SQLALCHEMY_URI`. Shape:
   ```
   postgresql+asyncpg://nf_telemetry:<pw-from-sm>@host.docker.internal:5432/nf_telemetry
   ```

3. **Apply migrations**:
   ```sh
   cd /home/davsean/Documents/git/nextflow_telemetry
   set -a; source deploy/onclappc02/.env; set +a
   uv run alembic upgrade head
   ```

4. **Build + bring up**:
   ```sh
   cd deploy/onclappc02
   docker compose build
   docker compose up -d
   ```

## Cutover — Phase 3 DNS flip

The new stack is running internally; DNS flip is the only thing
between Cloud Run and onclappc02.

### Pre-flip checks (no external impact)

```sh
# API serves /health correctly via Traefik (Host header simulates DNS):
curl -sSk -H "Host: nf-telemetry.cancerdatasci.org" https://localhost/health

# Frontend serves the SPA:
curl -sSk -H "Host: cmgd.cancerdatasci.org" https://localhost/ | head

# DB connection clean from inside the API container:
docker exec nf_telemetry_api curl -sS http://localhost:8000/api/admin/stats
```

### Heads-up: Traefik ACME rate-limit

The moment the API container came up with Traefik labels, Traefik started
trying to provision a Let's Encrypt cert via the TLS-ALPN challenge.
Validation goes wherever DNS points the hostname — currently Cloud Run —
so the validator sees Cloud Run's cert instead of Traefik's ACME
challenge cert and returns 403. After 5 failures within an hour Traefik
is rate-limited by Let's Encrypt for ~1 hour. **This is not a problem**
for the cutover — once DNS flips, the next retry succeeds. But:

- Don't restart Traefik or recycle the containers before the flip; each
  attempt extends the rate-limit window.
- After the flip, allow up to a minute for Traefik to retry against the
  new (correct) endpoint. The error log clears, a real cert lands, and
  `https://nf-telemetry.cancerdatasci.org/health` returns 200.

### Flip steps (in Cloudflare DNS, DNS-only / grey cloud)

1. `nf-telemetry.cancerdatasci.org`:
   - Remove: `CNAME → ghs.googlehosted.com` (Cloud Run managed cert path)
   - Add: `A → 140.226.4.71`
2. `cmgd.cancerdatasci.org`:
   - Set: `A → 140.226.4.71`
   - (If currently a Firebase Hosting CNAME, remove that.)

Both DNS-only (grey cloud) per the campus firewall constraints
documented in `monode/infrastructure/compose/NETWORK_CONSTRAINTS.md`.

### Post-flip verification

```sh
# DNS shows the new IP:
dig +short nf-telemetry.cancerdatasci.org   # → 140.226.4.71
dig +short cmgd.cancerdatasci.org           # → 140.226.4.71

# Cert issued by Let's Encrypt (not the Traefik default):
echo | openssl s_client -servername nf-telemetry.cancerdatasci.org \
  -connect nf-telemetry.cancerdatasci.org:443 2>/dev/null | \
  openssl x509 -noout -subject -issuer

# Endpoint healthy:
curl -sS https://nf-telemetry.cancerdatasci.org/health
```

### What clients see at flip time

- **Nextflow weblog POSTs from compute nodes** start landing on the new
  DB immediately on TTL expiry. Events reference `run_id` and `task_id`
  values that the new DB has never seen — they'll either be accepted as
  new run-lifecycle / process events (the events endpoint is lenient) or
  fail at FK boundaries depending on schema. Some loss of in-flight
  telemetry is expected and was accepted as the cost of the fresh-start
  strategy.
- **nf-client daemons** (Alpine, Anvil) will start calling the new DB.
  `POST /api/dispatch/batch` returns empty (no workflows registered yet
  → no pending jobs → nothing to claim). Daemons keep heartbeating
  cleanly; no new work starts until production workflows are registered
  in the new DB.

### Production data bootstrap (after flip)

The new DB is empty. To accept real work:

1. Register production workflows via `POST /api/workflows`:
   ```sh
   curl -X POST https://nf-telemetry.cancerdatasci.org/api/workflows \
     -H 'Content-Type: application/json' \
     -d '{
       "workflow_id": "cmgd_nextflow",
       "version": "1.6.0",
       "repository_url": "https://github.com/seandavi/curatedMetagenomicsNextflow",
       "revision": "main",
       "profile": "anvil",
       "max_retries": 3
     }'
   ```
2. Register production sample sets (BioProject manifests via the
   existing import paths).
3. Hit `POST /api/admin/reconcile-jobs` to create the cross-product of
   pending jobs.

## Phase 4 — Cloud Run + Cloud SQL teardown

Once the new stack has held production for some quiet period:

1. Delete the Cloud Run domain mapping (releases the `ghs.googlehosted.com`
   target):
   ```sh
   gcloud beta run domain-mappings delete \
     --domain nf-telemetry.cancerdatasci.org \
     --region us-central1 --project curatedmetagenomicdata
   ```
2. Delete the Cloud Run service:
   ```sh
   gcloud run services delete nf-telemetry \
     --region us-central1 --project curatedmetagenomicdata
   ```
3. Delete the Cloud SQL instance backing it. **Snapshot first** if you
   want to keep the historical data; snapshot storage is tiny vs. the
   running instance.

### Rollback (any time before Phase 4)

Flip the Cloudflare DNS back:
- `nf-telemetry.cancerdatasci.org` → `CNAME ghs.googlehosted.com` (DNS-only)
- `cmgd.cancerdatasci.org` → whatever the previous Firebase target was

The Cloud Run service + Cloud SQL stay healthy on idle as long as you
haven't run Phase 4. Any writes that landed on onclappc02 between the
two flips will be on the new DB only — pause workflows first if that
matters.

## Operational notes

- **Migrations** run from the workstation (this host), not the API
  container. `uv run alembic upgrade head` with the right `SQLALCHEMY_URI`.
- **Pulling the latest API** is `docker compose build nf_telemetry_api &&
  docker compose up -d nf_telemetry_api`. ~30s downtime; clients retry.
- **Frontend updates** are baked at build time; rebuild only if the API
  URL changes or the bundle is stale.
- **Backups** are not configured at this layer. Tracked separately in
  issue #84 (pg_basebackup + WAL archiving at the shared cluster level).
- **Logs**: `docker compose logs -f nf_telemetry_api` for app logs;
  `docker logs traefik` for routing + cert issues.
