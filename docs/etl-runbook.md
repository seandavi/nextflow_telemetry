# nf-etl runbook (onclappc02)

Operating the output-catalog ETL (`src/nextflow_telemetry/etl/`, #57, #228).
Design: [`output-catalog-etl-design.md`](./output-catalog-etl-design.md); build
plan: [`output-catalog-etl-plan.md`](./output-catalog-etl-plan.md).

## What a tick does

nf-etl is a producer into the shared cdsci DuckLake (cdsci-lake ADR-0011): its
tables are `lake.cmgd.*`, its ledger rows are `ops` source `cmgd` with
`writer = nextflow_telemetry`, and its snapshots are authored
`nextflow_telemetry:cmgd`. For every v2 registration (`GET /api/workflows`) that
has an `OutputSpec` (`etl/specs.py`):

1. Reads its completed jobs from v2 (`GET /api/workflows/{pk}/jobs?status=completed`,
   keyset-paged) and subtracts the sample keys already in `lake.cmgd.qc_metrics`
   for that registration (one qc row per ingested sample; the lake is its own
   done-set).
2. Ingests iff that backlog is >= `--threshold` (500) or its oldest completion is
   older than `--max-age-hours` (24), at most `--limit` (1000) samples.
3. Per sample (8 in parallel): finds `MARK_COMPLETE` under
   `r2:cmgd-raw/<workflow_id>/<version>/<key>/`, then, for `cmgd_nextflow 2.2.1`
   only, under the legacy GCS base `gs1:cmgd-data/results/cMDv4/`. Never LISTs.
   Unpublished samples stay pending.
4. Per `--batch-size` (500) samples: one `ops.run` (one `lake_ops.run` row), one
   insert per table in one attributed snapshot, sorted by
   (study_name, sample_key, feature), then `ops.set_watermark(source='cmgd',
   key='<workflow_id>/<version>')` with `{samples, last_completed_at}`. The
   watermark is informational; step 1 decides what is pending.

HUMAnN gene families are not loaded: each sample's file stays in `cmgd-raw` and
gets a row in `lake.cmgd.humann_genefamilies_files` (`sample_key`, `readset_id`,
`workflow_id`, `version`, `humann_bundle`, `branch`, `key` relative to the
bucket, `size` in bytes, `sha256` hex, `rows`) for the public download index (#229).

## Registrations with specs

| workflow_id | version | tables beyond the core set |
|---|---|---|
| `cmgd_nextflow` | 2.2.1 | — (core: metaphlan, bracken, resistome, qc_metrics, marker_abundance, marker_presence) |
| `cmgd_mpa4.2` | 2.3.0 | — |
| `cmgd_humann3.9` | 2.3.0 | bundle MetaPhlAn (`mpa4.1.1_vJun23`), `humann_pathabundance`, `humann_pathcoverage`, `humann_genefamilies_files` |
| `cmgd_humann4a1` | 2.3.0 | bundle MetaPhlAn (`mpa4.1.1_vOct22`), `humann_pathabundance`, `humann_genefamilies_files`; HUMAnN 4 has no pathcoverage |

A registration without a spec is skipped; adding one is an entry in `SPECS`.

## Environment

The lake connection is cdsci-lake's `lake_connect`, configured by its own
`CU_OPENALEX_*` settings (env vars or a `.env` in the working directory).

| variable | value on onclappc02 | used for |
|---|---|---|
| `CU_OPENALEX_LAKE_BACKEND` | `postgres` (set in `nf-etl.service`) | the shared lake: catalog DB `lake` on `CU_OPENALEX_LAKE_PG_HOST` (default `100.74.53.55`), data `r2://cdsci-lake/`. Unset means `local`: a DuckDB-file catalog under `CU_OPENALEX_STORAGE_BASE_URI` (default `file://./data`), for dev and tests only |
| `CU_OPENALEX_DUCKDB_TEMP_DIRECTORY`, `CU_OPENALEX_DUCKDB_MEMORY_LIMIT` | optional, in `~/.config/nf-etl/env` | DuckDB spill dir / memory cap for a large backfill |
| `NF_TELEMETRY_URL` | default `https://nf-telemetry.seandavi.workers.dev` | v2 reads (unauthenticated GETs) |
| `ETL_SOURCE_BASE` | default `r2:cmgd-raw` | rclone base for published outputs |

**Secrets.** None live in files. With the `postgres` backend cdsci-lake reads the
catalog password and the R2 account id and keys from GCP Secret Manager
(project `cdsci-infra`: `cdsci-postgres-admin-password`, `cdsci-r2-account-id`,
`cdsci-r2-access-key-id`, `cdsci-r2-secret-access-key`) by shelling out to
`gcloud`, so the user running nf-etl needs a gcloud login with access to them.
Check it with `gcloud secrets versions access latest --secret=cdsci-r2-account-id
--project=cdsci-infra >/dev/null && echo ok`. Reading `cmgd-raw` still uses the
rclone `[r2]` / `[gs1]` remotes.

## One-time provisioning

1. **v2 endpoint.** The ETL needs `GET /api/workflows/{pk}/jobs` (#228), so the
   Worker must be deployed with it first.
2. **Schema, tables, source.** The first write-mode connection creates
   `lake.cmgd` and its tables (`CREATE ... IF NOT EXISTS`, `SET SORTED BY`) and
   registers the `cmgd` source; every later connection re-registers it
   (idempotent). `nf-etl status` is enough:

   ```sh
   CU_OPENALEX_LAKE_BACKEND=postgres uv run nf-etl status
   ```
3. **Smoke test** from a checkout. `parse` reads one sample and touches no lake;
   a local-backend ingest exercises the whole write path on scratch disk:

   ```sh
   uv run nf-etl --workflow cmgd_nextflow --version 2.2.1 parse --sample 05f281407e15a03e65eba0dd74f30fae
   CU_OPENALEX_LAKE_BACKEND=local CU_OPENALEX_STORAGE_BASE_URI=file:///data/davsean/tmp/nf-etl-smoke \
     uv run nf-etl --workflow cmgd_nextflow --version 2.2.1 ingest --limit 3
   ```
4. **Retire the `cmgd_lake` prototype database.** The pre-#234 nf-etl wrote a
   dedicated DuckLake catalog `cmgd_lake` (~38 MB) on `pg_main`; nothing reads it now. As
   the cluster superuser, dump it, keep the dump, then drop it (the parquet it
   points at is left alone):

   ```sh
   mkdir -p /data/davsean/backups
   docker exec pg_main pg_dump -U postgres -Fc cmgd_lake > /data/davsean/backups/cmgd_lake-$(date +%F).dump
   docker exec -i pg_main pg_restore -l < /data/davsean/backups/cmgd_lake-$(date +%F).dump | head   # readable
   docker exec -it pg_main psql -U postgres -c "DROP DATABASE cmgd_lake;"
   ```

   The API database's `etl_ingested` table (alembic `20260707_c7d8e9fa`) is no
   longer written either; it stays until a migration drops it.

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

Delete a registration's rows from `lake.cmgd.qc_metrics` (the done-set) and the
next tick re-ingests them. A batch deletes its samples' rows for that
registration in every table before inserting, so nothing duplicates and other
registrations sharing a sample key are untouched:

```sql
DELETE FROM lake.cmgd.qc_metrics WHERE workflow_id = 'cmgd_humann3.9' AND version = '2.3.0';
```

## Volumes

`nf-etl volumes` prints measured sizes per table and registration as markdown:
rows/sample, bytes/row (live parquet file bytes over record counts, so rows
still inlined in the catalog are left out), bytes/sample, and extrapolations to
200k and 400k samples; gene families come from their files index. The latest
measurement is in [`data-volumes.md`](./data-volumes.md).
