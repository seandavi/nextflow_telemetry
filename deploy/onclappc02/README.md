# Self-host on onclappc02

Production compose for the telemetry API + frontend, served via the
existing Traefik on `cancerdatasci.org`. Postgres lives in the existing
`pg_duckdb` container (see `monode/infrastructure/compose/pg_and_duckdb`).

## Files

- `docker-compose.yml` — API + frontend services, both labelled for Traefik
- `Dockerfile.frontend` — multi-stage build (node → nginx:alpine)
- `nginx.conf` — SPA config with sane caching headers
- `.env.example` — fill in and copy to `.env`

## First-time setup

1. **Postgres DB + user**, inside the existing pg_duckdb container:
   ```sh
   docker exec -it pg_duckdb_18 psql -U postgres
   ```
   ```sql
   CREATE USER nf_telemetry WITH PASSWORD '<choose-one>';
   CREATE DATABASE nextflow_telemetry OWNER nf_telemetry;
   GRANT ALL PRIVILEGES ON DATABASE nextflow_telemetry TO nf_telemetry;
   ```

2. **Cloudflare DNS** for both hostnames (DNS-only, grey cloud — required
   on this network):
   - `nf-telemetry.cancerdatasci.org` → `140.226.4.71`
   - `cmgd.cancerdatasci.org` → `140.226.4.71`

3. **`.env`** — copy `.env.example` and fill the password into `SQLALCHEMY_URI`.

4. **Apply migrations** against the new empty DB, then **load data**.
   See the cutover plan below.

5. **Bring up the stack**:
   ```sh
   docker compose build           # ~2 min first time
   docker compose up -d
   docker compose logs -f nf_telemetry_api
   ```
   Traefik should auto-discover both services within a few seconds. First
   request to each hostname triggers Let's Encrypt cert issuance (~5s).

## Cloud Run → onclappc02 cutover

Goal: zero-downtime DNS swap. The host changes underneath; the public
URL stays the same.

### Phase 1 — stable the URL (do this whenever)

The current API URL is the GCP-issued `nf-telemetry-819875667022.us-central1.run.app`.
This is sprinkled across pipeline configs and nf-client configs on the
clusters. Before swapping hosts, get everyone pointing at a stable
hostname *we own* so the second swap is just DNS.

1. In Cloud Run, add a **custom domain mapping** for
   `nf-telemetry.cancerdatasci.org` → the `nf-telemetry` service.
   Cloud Run will instruct you to add a CNAME (or A record) at your
   DNS provider; do that in Cloudflare (DNS-only).
2. Wait for the cert in Cloud Run to go green (a few minutes).
3. Verify: `curl https://nf-telemetry.cancerdatasci.org/health` returns
   the existing service.
4. Update everywhere the old URL appears:
   - `seandavi/curatedMetagenomicsNextflow` `nextflow.config`:
     `params.api_url`, the weblog hook URL
   - nf-client configs on Anvil + Alpine (the `server_url` /
     `weblog_url` fields in `client-*.yaml`)
   - the frontend build's `VITE_API_URL` (rebuild + redeploy Firebase
     Hosting once; this becomes irrelevant after Phase 3 since the
     frontend moves on-prem)
5. Run a sample dispatch end-to-end through the new hostname to confirm.

After Phase 1 the only thing pinning you to GCP is the DNS record.

### Phase 2 — replicate data to onclappc02

1. Set up the DB + user (above).
2. Run migrations against the new DB so the schema is current:
   ```sh
   cd /home/davsean/Documents/git/nextflow_telemetry
   SQLALCHEMY_URI='postgresql+asyncpg://nf_telemetry:<pw>@localhost:5432/nextflow_telemetry' \
     uv run alembic upgrade head
   ```
3. `pg_dump` from Cloud SQL and `pg_restore` into the new DB. Easiest
   path: through the Cloud SQL proxy:
   ```sh
   # On a workstation with gcloud:
   gcloud sql connect cmgd-prod ...                # discover host:port via the proxy
   pg_dump -h <proxy-host> -U postgres -Fc cmgd_prod \
     --no-owner --no-acl > cmgd_prod.dump

   # On the server:
   docker cp cmgd_prod.dump pg_duckdb_18:/tmp/
   docker exec -it pg_duckdb_18 pg_restore \
     -U nf_telemetry -d nextflow_telemetry \
     --no-owner --role=nf_telemetry /tmp/cmgd_prod.dump
   ```
4. Spot-check that row counts match Cloud SQL.

### Phase 3 — flip DNS

1. Bring up the local stack with `docker compose up -d`. Traefik issues
   a cert as soon as the first request lands; we'll get it after the
   DNS flip.
2. In Cloudflare, change `nf-telemetry.cancerdatasci.org` from the Cloud
   Run target to an A record for `140.226.4.71`. DNS-only.
3. Same for `cmgd.cancerdatasci.org` — point at `140.226.4.71`. (If you
   want to keep the Firebase Hosting URL alive as a fallback, leave it
   alone; the new hostname is the canonical one.)
4. Within seconds the cluster nodes' POSTs land on Traefik → API
   container → local Postgres. Watch logs for the first heartbeat.
5. Once confident (an hour? a day?), turn off Cloud Run and Cloud SQL.

### Rollback

If anything goes wrong post-flip, flip the CNAME back at Cloudflare.
The Cloud Run service and Cloud SQL stay healthy on idle until you
delete them. Any writes that landed on onclappc02 between flip and
rollback would be lost — pause the workflow first if that matters.

## Operational notes

- **No backups configured here.** Postgres backups should be handled at
  the pg_duckdb container layer (WAL archive → MinIO?). That's separate
  from this stack.
- **Migrations** run from the workstation, not the API container — same
  pattern as today. `uv run alembic upgrade head` with the right
  `SQLALCHEMY_URI`.
- **Pulling the latest API** is `docker compose build nf_telemetry_api &&
  docker compose up -d nf_telemetry_api`. ~30s downtime; weblog clients
  retry, but if you're paranoid pause the workflow first.
- **Frontend updates** are baked at build time. If the API URL changes,
  rebuild the frontend image; otherwise `git pull && docker compose
  build nf_telemetry_frontend && docker compose up -d nf_telemetry_frontend`.
