# 0014. Run the control plane on Cloud Run and PlanetScale Postgres

- **Status:** Proposed
- **Date:** 2026-11-11
- **Deciders:** Sean Davis

## Context

[0013](0013-postgres-control-plane-edge-event-sink.md) moves the control plane
from the v2 Worker to one Python service on one Postgres database, and put both
on onclappc02. This ADR changes where they run. It amends 0013 decisions 1, 2 and
7 and its Consequences. Everything else in 0013 stands: the event routes and the
edge sink, the pull ingester, deadlines as columns plus one sweep, history in R2,
auth, and migrating jobs and runs.

What changed:

- **onclappc02 can't open outbound TCP 5432 or 6432.** Measured 2026-11-11: the
  campus firewall blocks them, and 443 is open. Nothing on onclappc02 can talk to
  a hosted Postgres over the Postgres protocol.
- **Dispatch would sit on a single host.** 0013 accepted this ("onclappc02 now
  affects dispatch") and made backups with a tested restore (#84) a precondition
  for cutover. A managed database and a managed service remove that risk instead
  of backing it up.

What the code does today:

- **The service runs several loops in-process.** `lifespan` in
  `src/nextflow_telemetry/main.py` starts the history warm-up (`event_archive.warm()`,
  about 90 s for the first load, #413), the deadline sweep, the R2 event ingester
  and the daily retired-job archive. The last three are off unless
  `DEADLINE_SWEEP_ENABLED`, `EVENT_INGEST_ENABLED` and `RETIRED_ARCHIVE_ENABLED`
  are set (`config.py`). The Dockerfile runs one uvicorn worker.
- **One database holds everything.** The service and `nf-meta` both read
  `SQLALCHEMY_URI`, and one Alembic history (`migrations/env.py`, one `metadata`)
  covers every table.
- **`nf-meta` uses four tables.** `nf-meta sources` and the ZOE loader and linker
  go through `SourceCatalog` (`meta/catalog/store.py`). It touches
  `metadata_sources`, `metadata_source_assets`, `metadata_source_snapshots` and
  `metadata_source_events`, and nothing else in Postgres. The other loads (`cmd`,
  `olm`, `xref`) use the lake.
- **One foreign key joins the two groups.** `submissions.source_snapshot_*`
  references `metadata_source_snapshots` through the composite
  `fk_submissions_source_snapshot`, added by M1
  (`migrations/versions/20261030_fa0b1c2d_control_plane_schema.py`). The
  `ck_submissions_source_snapshot` check requires all three columns or none. No
  application code writes these columns yet.
- **`nf-etl` already talks to the control plane over HTTPS.** It reads
  registrations and completed jobs from `NF_TELEMETRY_URL` (`etl/v2.py`,
  `etl/cli.py`), not from Postgres.
- **`nf-client` already has an `event_url` separate from `server_url`**
  (`packages/nf_client/src/nf_client/config.py`).
- **`deploy/cloudrun.yaml` is the retired May 2026 deployment** (us-central1,
  Cloud SQL, `maxScale: 4`). It is not a starting point for this one.
- **Production still talks to the Worker.** M8 and M9 haven't happened
  (`README.md`, "API reference").

## Decision

We will run the control plane on **PlanetScale Postgres and Cloud Run**, and keep
the source catalog on `pg_main`.

1. **Hosting.**
   - **Database:** PlanetScale Postgres, database `nf-telemetry`, region
     `gcp-us-east4`, billed through Cloudflare. It has to be created from the
     Cloudflare dashboard (Storage & databases → Postgres & MySQL → Create
     PlanetScale database), because `pscale database create` fails without a card
     on the PlanetScale org. Start at PS-5, single node.
   - **Service:** the existing Python service (M1–M6), unchanged, on Cloud Run in
     GCP project `curatedmetagenomicdata`, region `us-east4`. `min=max=1`
     instance with CPU always allocated, because the loops above run in-process
     and must not be throttled or run twice. Deploys set the three `*_ENABLED`
     flags. Cloud Run allows unauthenticated invocation, because GETs are public
     and writes carry a bearer token checked by the service (0013 decision 6).
   - **Secrets:** GCP Secret Manager in `cdsci-infra` stays the source of truth.
   - **Origin URL:** daemons and the dashboard use the Cloud Run `run.app` URL.
     A custom domain is not part of this work. This replaces 0013 decision 7's
     stable origin hostname; the edge hostname and `event_url` parts of decision 7
     stand.
2. **The source catalog stays on `pg_main`.**
   - `metadata_sources`, `metadata_source_assets`, `metadata_source_snapshots` and
     `metadata_source_events` stay in `pg_main`, next to the lake and `nf-meta`.
   - The control plane, submissions, users and curated tables move to PlanetScale.
   - `submissions.source_snapshot_*` stay as plain columns. The cross-database
     foreign key `fk_submissions_source_snapshot` is dropped, and nothing replaces
     it at the database level. `ck_submissions_source_snapshot` (all three or none)
     stays. Snapshots are immutable and content-addressed
     (`metadata_source_snapshots.sha256`), so a recorded
     `(source_id, asset_id, version)` stays valid. A writer must take the triple
     from `nf-meta sources show`, and a dangling pointer is found by comparing the
     two over HTTPS. Nothing writes the columns today, so no existing behaviour
     changes.
   - 0013 decision 1 ("one database") becomes two, split by who can reach them.
3. **Operator commands that need the database run as Cloud Run Jobs** from the
   same image, not on onclappc02: `alembic upgrade`, `v2_import` and `v2_replay`.
4. **What still depends on onclappc02 after cutover:** the lake catalog and
   `pg_main`, `nf-etl` (over HTTPS to the new origin), `nf-meta`, publishing, and
   the dashboard container. **Dispatch does not.**
5. **Backups.**
   - PlanetScale's own backups cover the control plane. Retention and the restore
     procedure are confirmed against PlanetScale's documentation before cutover.
   - The nightly restic job on onclappc02 can't reach PlanetScale, so it doesn't
     cover the control plane. It continues to cover what stays on onclappc02,
     and its scope is confirmed to include `pg_main` before cutover.
   - #84 is redone for PlanetScale: a restore is tested before M8.
6. **The M7 rehearsal (#377) is re-run against PlanetScale and Cloud Run**
   before M8, including the import, the replay and the shadow period.

## Alternatives considered

- **Stay on onclappc02 (0013 as written).** It leaves a single host on the
  dispatch path, with backups still unconfigured at that layer (#84).
- **Rewrite the API as a Worker on Hyperdrive.** It would throw away M3–M5. The
  service and its loops already exist and are tested against Postgres.
- **Cloudflare Containers.** They sleep when idle. That is a poor fit for the
  in-process deadline sweep and ingester, and for the ~92 s history warm-up.
- **Move the source catalog to PlanetScale too.** It would keep the foreign key,
  but `nf-meta` runs next to the lake on onclappc02 and could not reach it.
- **Ask IT to open outbound 5432.** It would remove the split. Revisit if the
  catalog and the control plane need transactional joins, or if the split costs
  more than the exception. Until then it depends on an external team.
- **Send catalog reads and writes through the service over HTTPS.** `nf-meta`
  would stop using SQLAlchemy directly and gain an API client. That is more than
  one foreign key justifies.

## Consequences

- **Dispatch no longer depends on onclappc02.** An outage there stops publishing,
  loads and the dashboard container, not claims.
- **Two databases, one Alembic history.** `fa0b1c2d` creates the foreign key and
  `e9fa0b1c` creates the catalog tables. Applied to PlanetScale, the chain would
  create empty `metadata_source*` tables there. A follow-up migration drops the
  foreign key, and how catalog migrations on `pg_main` stay separate from control
  plane migrations is settled in that change.
- **Copying existing data is open.** `users`, `submissions` and the curated tables
  on `pg_main` must reach PlanetScale. `v2_import` reads a local directory, and
  `just v2-export` copies the export with rclone, so a Cloud Run Job needs another
  way to get that directory. Both are settled in the M7 plan.
- **Cloud Run can restart the one instance.** The sweep's catch-up rule (0013
  decision 4) covers the gap. A rollout can briefly overlap two instances, which
  0013's idempotent transitions and `SKIP LOCKED` tolerate.
- **Cross-project secrets.** The Cloud Run service account needs access to
  `cdsci-infra` secrets. How the service reads them is settled at implementation.
- **The origin URL is now `run.app`.** Changing it later means updating every
  daemon config and rebuilding the dashboard. The dashboard's baked-in API origin
  (`FRONTEND_API_URL`) and the service's `CORS_ORIGINS` change with it.
- **A single PS-5 node has no standby.** That is accepted for the scale in 0013
  (3 daemons, about 76k jobs). Revisit if availability matters more.
- **Docs.** `deploy/onclappc02/README.md` and `deploy/DEPLOY.md` describe the
  onclappc02 stack. They are rewritten when the cutover (M8) happens, not now.
- Status changes: 0013 is **amended by 0014**. It stays Accepted.

## References

- This ADR: #434. Plan and tracking: #381 (M7 #377, M8 #378, M9 #379, M10 #380).
- Amends: [0013](0013-postgres-control-plane-edge-event-sink.md).
- Code: `src/nextflow_telemetry/main.py` (lifespan), `src/nextflow_telemetry/meta/catalog/store.py`,
  `src/nextflow_telemetry/db.py`,
  `src/nextflow_telemetry/migrations/versions/20261030_fa0b1c2d_control_plane_schema.py`.
- Issues: #84, #413.
- Constraint: onclappc02 firewall, outbound 5432 and 6432 blocked, 443 open (measured 2026-11-11).
