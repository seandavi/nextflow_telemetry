# nf-etl runbook (onclappc02)

Operating the output-catalog ETL (`src/nextflow_telemetry/etl/`, #57, #228).
Design: [`output-catalog-etl-design.md`](./output-catalog-etl-design.md); build
plan: [`output-catalog-etl-plan.md`](./output-catalog-etl-plan.md).

## What a tick does

For every v2 registration (`GET /api/workflows`) that has an `OutputSpec`
(`etl/specs.py`):

1. Reads its completed jobs from v2 (`GET /api/workflows/{pk}/jobs?status=completed`,
   keyset-paged) and subtracts the keys already in `etl_ingested`.
2. Ingests iff that backlog is >= `--threshold` (500) or its oldest completion is
   older than `--max-age-hours` (24), at most `--batch` (1000) samples.
3. Per sample: finds `MARK_COMPLETE` under `r2:cmgd-raw/<workflow_id>/<version>/<key>/`,
   then, for `cmgd_nextflow 2.2.1` only, under the legacy GCS base
   `gs1:cmgd-data/results/cMDv4/`. Never LISTs. Unpublished samples stay pending.
4. Parses, replaces the sample's rows for that registration in one DuckLake
   transaction, then writes `etl_ingested`.

Deferred tables (metaphlan markers, HUMAnN gene families: 120k–1.7M rows per
sample in the pilot) are only ingested with `--include-deferred`.

## Registrations with specs

| workflow_id | version | tables beyond the core set |
|---|---|---|
| `cmgd_nextflow` | 2.2.1 | — (core: metaphlan, bracken, resistome, qc_metrics; markers deferred) |
| `cmgd_mpa4.2` | 2.3.0 | — |
| `cmgd_humann3.9` | 2.3.0 | bundle MetaPhlAn (`mpa4.1.1_vJun23`), `humann_pathabundance`, `humann_pathcoverage`, `humann_genefamilies` (deferred) |
| `cmgd_humann4a1` | 2.3.0 | bundle MetaPhlAn (`mpa4.1.1_vOct22`), `humann_pathabundance`, `humann_genefamilies` (deferred); HUMAnN 4 has no pathcoverage |

A registration without a spec is skipped; adding one is an entry in `SPECS`.

## Environment

| variable | value on onclappc02 | used for |
|---|---|---|
| `SQLALCHEMY_URI` | the API's, from `deploy/onclappc02/.env`, host rewritten `@pg_main:` → `@127.0.0.1:` | `etl_ingested` watermark; the lake catalog DSN is derived from it |
| `ETL_LAKE_CATALOG_PG_DB` | `cmgd_lake` | DuckLake catalog database on the same cluster |
| `ETL_LAKE_DATA_PATH` | R2 `s3://…` prefix for the lake parquet (see open question below) | DuckLake data; R2 keys come from the rclone `[r2]` remote |
| `NF_TELEMETRY_URL` | default `https://nf-telemetry.seandavi.workers.dev` | v2 reads (unauthenticated GETs) |
| `ETL_SOURCE_BASE` | default `r2:cmgd-raw` | rclone base for published outputs |

Unset `ETL_LAKE_CATALOG_PG_DB` and the lake falls back to a DuckDB-file catalog at
`ETL_LAKE_CATALOG` (default `/data/cmgd/lake/cmgd_lake.ducklake`), for dev only.

**Open:** the lake's R2 bucket. `storage-layout.md` puts DuckLake data in
`cdsci-lake`; `publish-and-catalog-design.md` wants a dedicated cmgd lake whose
parquet is also served publicly over HTTPS. Pick the bucket/prefix before the
first ingest — moving it later means rewriting the catalog's data path.

## One-time provisioning

1. **Catalog database.** The app role lacks `CREATEDB`, so as the cluster superuser:

   ```sh
   docker exec -it pg_main psql -U postgres -c "CREATE DATABASE cmgd_lake OWNER nf_telemetry;"
   ```

   Do not `CREATE EXTENSION pg_duckdb` in it; DuckLake creates its own tables on
   first attach.
2. **Watermark table.** `etl_ingested` is an alembic migration in the API's
   database (`20260707_c7d8e9fa_etl_ingested`); `nf-etl` also creates it if
   missing. Its `sample_id` column holds the job's sample key (md5 for 2.2.x,
   `RS.…` for readset-keyed registrations).
3. **v2 endpoint.** The ETL needs `GET /api/workflows/{pk}/jobs` (#228), so the
   Worker must be deployed with it first.
4. **Env file** (non-secret settings for the timer):

   ```sh
   mkdir -p ~/.config/nf-etl
   cat > ~/.config/nf-etl/env <<'ENV'
   ETL_LAKE_DATA_PATH=s3://<bucket>/<prefix>/
   ENV
   ```
5. **Smoke test** from a checkout (dry run, no DB or lake writes):

   ```sh
   uv run nf-etl --workflow cmgd_nextflow --version 2.2.1 parse --sample 05f281407e15a03e65eba0dd74f30fae
   set -a; . deploy/onclappc02/.env; set +a
   SQLALCHEMY_URI="${SQLALCHEMY_URI/@pg_main:/@127.0.0.1:}" ETL_LAKE_CATALOG_PG_DB=cmgd_lake uv run nf-etl status
   ```

## Timer

Unit files: [`deploy/onclappc02/nf-etl.service`](../deploy/onclappc02/nf-etl.service)
and [`nf-etl.timer`](../deploy/onclappc02/nf-etl.timer) (every 15 min). The
service runs from `~/Documents/git/nextflow_telemetry`, so it picks up whatever
that checkout has; pull it before expecting new specs.

```sh
mkdir -p ~/.config/systemd/user
ln -sf ~/Documents/git/nextflow_telemetry/deploy/onclappc02/nf-etl.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nf-etl.timer
systemctl --user list-timers nf-etl.timer
journalctl --user -u nf-etl.service -n 50     # each tick's per-registration summary
```

Lingering is already enabled for `davsean`, so user timers run without a login session.

## Re-ingesting

Delete the watermark rows and the next tick re-ingests; writes replace a
sample's rows for its registration, so nothing duplicates:

```sql
DELETE FROM etl_ingested WHERE workflow_id = 'cmgd_humann3.9' AND workflow_version = '2.3.0';
```

To backfill the deferred tables, delete the registration's watermark rows the
same way, then run `nf-etl --workflow … --version … ingest --include-deferred`
(optionally `--limit N`).
