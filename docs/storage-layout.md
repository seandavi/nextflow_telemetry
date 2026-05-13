# Storage layout

> Status: **draft** — physical-storage companion to [`publish-and-catalog-design.md`](./publish-and-catalog-design.md). That doc covers the logical publish-path contract and the DuckLake catalog as an abstraction; this one nails down the actual buckets, visibility, lifecycle, and archival model.

## Framing

Three decisions the layout rests on:

1. **All cloud storage moves to Cloudflare R2.** Zero egress to onclappc02 (campus firewall constraint) and to outside readers. GCS was a Cloud-Run-era choice; nothing else points us there now.
2. **Raw pipeline outputs are derived from NCBI.** They are not the source of truth — `cmgd-public` (curated) + Postgres (orchestration/telemetry) are. Raw is durable for a hot window, then archived to cold storage. Worst-case loss is recoverable by re-running the pipeline at NCBI bandwidth + compute cost.
3. **One DuckLake across all data-products projects** (cmgd, omicidx, bioc, …). Schema-per-project in a shared `cdsci-lake` bucket with a shared `cdsci_lake_catalog` Postgres DB. Per-project lakes were considered and rejected: cross-project queries become joins, not federations.

## Buckets

| Bucket          | Visibility           | Purpose                                        | Lifecycle                            |
|-----------------|----------------------|------------------------------------------------|--------------------------------------|
| `cdsci-lake`    | private (read via catalog) | Shared parquet/Iceberg for **all** projects | `ducklake vacuum` only — no bucket TTL |
| `cmgd-raw`      | public-read          | cmgd Nextflow `publishDir` outputs             | hot → cold archive by workflow semver |
| `cmgd-public`   | public-read          | Curated cmgd artifacts (the stable URLs)       | durable; no expire                    |
| `cdsci-backups` | Object Lock, write-only token | Postgres dumps + lake snapshots       | retention policy retained             |

Project-specific buckets follow the `<project>-raw` / `<project>-public` pattern. Future projects (omicidx, bioc) get their own raw + public buckets as needed; they all share `cdsci-lake` and `cdsci-backups`.

## Path schemes

### `cmgd-raw`

The path is the published Nextflow output, as defined in [`publish-and-catalog-design.md`](./publish-and-catalog-design.md):

```
cmgd-raw/<workflow_id>/<workflow_semver>/<sample_id>/[<branch>/]<step_name>/...
```

`workflow_semver` is the workflow's semantic version (e.g. `1.4.0`), **not** the git SHA. The SHA is tracked in `workflow_runs_tbl` for provenance; the path stays on semver so the archive unit aligns with the version release boundary.

### `cmgd-public`

Curated public-facing artifacts. Path layout TBD per artifact (parquet products, TSV summaries, web-facing JSON). The URL stability promise is here, not on `cmgd-raw`.

### `cdsci-lake`

Managed by DuckLake. Catalog metadata in `cdsci_lake_catalog` (Postgres) dictates physical layout; we should not write into this bucket outside the DuckLake client. Schemas:

- `cmgd.*` — curated taxonomic profiles, marker abundances, per-sample QC summaries.
- `omicidx.*` — BioSample / BioProject / SRA metadata.
- `bioc.*` — Bioconductor data products.
- Additional projects get additional schemas.

### `cdsci-backups`

```
cdsci-backups/postgres/<db_name>/<YYYY-MM-DD>/dump.sql.gz
cdsci-backups/lake-snapshots/<YYYY-MM-DD>/manifest.json
```

Write-only token; restore is a manual operator-driven action.

## Archive model for `cmgd-raw`

Raw outputs stay in R2 standard for a **hot window** (default: 90 days from `workflow_version` deactivation; revisit when we have real cost data). After that, a per-(workflow_id, workflow_semver) prefix is archived as a single unit.

Two-phase lifecycle, **not** TTL expire:

- **Hot**: standard R2 storage, public-read URLs work normally.
- **Cold**: prefix relocated to cheaper storage. Postgres tracks the move; URLs under that prefix may become slow or require operator-assisted retrieval. This is an accepted tradeoff — public URLs on `cmgd-raw` are not a stable contract; `cmgd-public` is.

Archival state lives at the workflow-version level, not per-artifact:

- `workflows_tbl.archived_at TIMESTAMPTZ NULL`
- `workflows_tbl.archive_location TEXT NULL` — opaque URI, e.g. `r2://cdsci-cold/...` or `gs://cdsci-archive/...` depending on the chosen cold tier

Cold-tier destination is deliberately abstracted behind `archive_location`. R2 has an Infrequent Access class; GCS Archive and Glacier Deep Archive are cheaper for true cold. We can switch destinations without bucket surgery.

The archive job is a server-side scheduled task in the telemetry API (the process that already owns the workflows table). It runs nightly: for every workflow version that's been inactive past the hot window and has `archived_at IS NULL`, move the prefix and write back `archived_at` + `archive_location`.

## Catalog DB placement

The DuckLake catalog lives in `cdsci_lake_catalog` on the shared Postgres cluster on onclappc02, alongside but separate from per-project operational DBs:

- `cdsci_lake_catalog` — DuckLake-managed; do not write directly except through the DuckLake client.
- `nextflow_telemetry` — orchestration / job state / telemetry events.
- `omicidx`, `bioc`, … — per-project operational state.

Each project's API user gets `USAGE` on `cdsci_lake_catalog` plus `CREATE/INSERT/SELECT` on its own schema. Cross-schema reads default to `SELECT`-only so projects can join each other's published tables but can't write into them.

## Discovery & file browsing

R2's public-read mode is **HTTPS GET on known URLs only**. There is no anonymous S3 LIST, no anonymous S3-protocol access of any kind, and R2 does not auto-generate bucket index pages. Public-read on `cmgd-raw` and `cmgd-public` therefore solves *fetch* but not *discovery*. We solve discovery in the application layer.

**Primary mechanism — the artifacts catalog as the browser backend.** Issue #93's `artifacts` table is the source of truth for what has been published. The telemetry API exposes a browse endpoint (`GET /api/published?workflow=...&sample=...&prefix=...`) backed by a Postgres index lookup, returning JSON. A thin HTML view of the same data gives operators and external readers a navigable file tree. Cost is flat regardless of bucket size — no R2 LIST traffic on each browse.

**Secondary mechanism — static index pages in `cmgd-public`.** Optionally, a server-side job can emit `index.html` files at well-known prefixes for SEO and bookmarkable directory URLs. These are generated from the same artifacts catalog and become stale on changes; they're a convenience layer, not the source of truth. Skip until there's demand.

**For `cdsci-lake`** discovery is *table-level*, not file-level. Consumers connect to the DuckLake catalog and run `SHOW SCHEMAS` / `SHOW TABLES IN cmgd` / `DESCRIBE cmgd.taxonomy`. Object-level browsing of the lake bucket is an internal admin task only.

## Event-driven artifact tracking

R2 buckets emit **event notifications** to **Cloudflare Queues** on `object-create` (PutObject / CopyObject / CompleteMultipartUpload) and `object-delete` (explicit DELETE or lifecycle-driven deletion). The telemetry server consumes these via HTTP pull from outside Cloudflare — bearer-token auth, `POST .../messages/pull` (batch up to 100), `POST .../messages/ack` with lease IDs. This is the mechanism that keeps the artifacts catalog (issue #93) in sync with the buckets.

This supersedes the originally-considered "trigger artifact enumeration from the `process_completed` ingest path" design. The event-driven approach:

- **Decouples the catalog from the pipeline.** Workflows don't need to know they're being cataloged. No #57-spec gate on which steps publish — we listen for what actually appears.
- **Catches deletes.** When the archive job moves a retired-workflow-version prefix to cold storage, the lifecycle deletes flow through and we flip `artifacts.archived_at` from the consumer.
- **Catches out-of-band writes.** Manual operator uploads, side-channel copies — all appear in the catalog.
- **Survives server downtime.** Queue retention is 4 days (configurable to 14); unacked messages reappear after visibility timeout. A 30-minute deploy outage loses zero events. A mid-batch crash retries naturally.
- **No LIST traffic.** The create event carries the key, size, and etag; no R2 enumeration cost.

The consumer is a small async task in the telemetry API process: pull → upsert into `artifacts` → ack. The manual `POST /api/admin/reconcile-artifacts` endpoint stays as a backfill / drift-detection tool (e.g. quarterly compare R2 LIST against the table), but it is no longer the primary mechanism.

**Constraints worth noting:**
- 100 event-notification rules per bucket (more than enough — one per publishing convention).
- 5k messages/sec queue throughput cap (way above our publish rate).
- One event delivery = 3 queue operations (write/read/ack) = ~$1.20/M messages on Workers Paid; first 1M ops/month included. cmgd full reprocess at ~600k events is ~$0.72.

## Access tokens

All app/infrastructure secrets — Cloudflare API token, R2 access keys, Cloudflare Queue pull-consumer bearer tokens, Postgres passwords, backup write-only tokens — are stored in **GCP Secret Manager**. Terraform reads them at plan/apply time via `google_secret_manager_secret_version` data sources; app deploys fetch them via `gcloud secrets versions access` and inject into the `.env` consumed by docker-compose. IAM grants are per-secret, scoped to the specific service account that needs access. Naming convention: `cmgd-<purpose>` for app-specific, `cdsci-<purpose>` for shared infra.

Token scopes:

- **`cmgd-raw` / `cmgd-public`**: read tokens are unnecessary (public). Write tokens scoped per-bucket and held by the daemon / publishing pipeline. Stored as `cmgd-r2-write-token` in GCP SM.
- **`cdsci-lake`**: per-project R/W tokens scoped to that project's schema-shaped prefix. Read-only token published to external consumers as needed. Stored as `cmgd-lake-rw-token`, `cmgd-lake-readonly-token`, etc.
- **`cdsci-backups`**: dedicated write-only token. Restore uses a separate, rarely-issued admin token. Object Lock makes accidental deletion impossible during the retention window. Stored as `cdsci-backups-write-token` (the restore admin token is operator-only and not in GCP SM).
