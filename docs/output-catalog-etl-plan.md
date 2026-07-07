# Output-catalog ETL — execution plan (#57)

> Status: **plan** — the *how to build it* companion to [`output-catalog-etl-design.md`](./output-catalog-etl-design.md)
> (file→table mechanism, analytical schema, taxonomy) and [`publish-and-catalog-design.md`](./publish-and-catalog-design.md)
> (telemetry plumbing, dedicated + frozen DuckLake, no orchestrator). This doc is the staged build with a
> **concrete acceptance check per phase** — each phase is independently shippable and "done" means the check passes.
> Target pipeline version: **`cmgd_nextflow` 2.2.1** (the active version). Runs on **onclappc02** (same host as the API + Postgres).

## Target picture

Publish to the DuckLake on a **regular cadence as samples complete**, in two tiers:

1. **Internal working lake** — DuckLake catalog in Postgres (separate DB on the same instance), data (parquet) on **R2**.
   Writable; ingested in batches. This is what group-internal users query.
2. **Frozen public lake** — the **same R2 bucket and the same parquet files** as (1); only the catalog and access
   protocol differ. External users get a **frozen DuckDB-file catalog** (`cmgd.duckdb`) whose data references resolve
   over **public HTTPS** (R2 GET, no credentials). No data duplication — the freeze snapshots the *catalog*, not the data.

Internal reads/writes over **S3 (R2)** with keys; external reads over **HTTPS**. One file set, two catalogs.

### One file set, two catalogs — the constraints

Sharing files (vs. a separate export copy) is cheaper and matches the R2 public model (GET-only, no anonymous LIST —
fine, because the catalog enumerates every file by exact key; browsing is never needed). It costs three things:

- **Relative / overridable data paths** are required, so the one file set resolves under both `s3://…` (internal) and
  `https://…` (external). If DuckLake pins absolute `s3://` URIs, the HTTPS client can't read them. **Verify first (Phase 0).**
- **Retention couples to publishing.** Internal compaction / `expire_snapshots` must not delete parquet a published
  frozen catalog still references, or external reads 404. Default rule: **don't expire published snapshots** (parquet is
  cheap, the lake is append-mostly).
- **No hiding by omission.** If a table is internal-only (plausibly the marker tables — ~89% of rows, off the
  low-latency path), you can't exclude it by not-copying. Split public/private **prefixes** in the same bucket and expose
  only the public prefix over HTTPS; the frozen public catalog references only public-prefix files.

DuckDB runs in-process on onclappc02. **No orchestrator** — a systemd timer (or cron) runs a plain `nf-etl` CLI.

### Trigger

A timer runs `nf-etl tick` frequently (default **every 15 min**). Each tick:

1. Computes **backlog** = completed 2.2.1 samples not yet in the lake (watermark query below).
2. Runs an **ingest batch** iff `backlog ≥ INGEST_THRESHOLD` (default **500**) **or** the oldest un-ingested completion
   is older than `MAX_AGE` (default **24 h** — a fallback so a trailing <500 tail still publishes).
3. After a successful ingest, runs **`freeze`** iff it's past the freeze cadence (default **daily**) or a manual flag is set.

Polling, not push: telemetry already *is* the completion event log, so the tick just reads it. (Pub/Sub is the documented
escape hatch if per-sample latency ever matters; not now.)

### Layout the ETL relies on (from the design doc — confirm in Phase 0, do not re-derive)

```
<publish_base>/cmgd_nextflow/2.2.1/<sample_id>/MARK_COMPLETE          # completion sentinel (HEAD gate)
<publish_base>/cmgd_nextflow/2.2.1/<sample_id>/manifest.json         # provenance + read_accounting + db_version
<publish_base>/cmgd_nextflow/2.2.1/<sample_id>/<full_data|rarefied_data>/<step>/<file>.tsv.gz
```

`<sample_id>` is `md5(sorted(SRR))`, already the telemetry sample key — so **every path is reconstructable from
`(workflow_id, version, sample_id)`; the ETL never LISTs GCS.**

## Component

One in-repo module: `src/nextflow_telemetry/etl/` (reuses `db.py` / `config.py`). CLI `nf-etl` with subcommands
`parse` · `ingest` · `tick` · `freeze` · `status`. DuckDB / fsspec deps go in an **optional dependency group**
(`etl`) so the API image stays lean. Fetch file bytes via the already-working `gs1:` remote (`rclone cat`) or `gcsfs` —
decided in Phase 1; both reuse existing creds.

### Watermark (idempotency)

New table in telemetry Postgres:

```
etl_ingested(sample_id, workflow_id, workflow_version,
             ingested_at timestamptz, row_counts jsonb, lake_snapshot bigint,
             PRIMARY KEY (sample_id, workflow_id, workflow_version))
```

`pending` = completed jobs `LEFT JOIN etl_ingested` where the join is NULL:

```sql
SELECT j.sample_id
FROM jobs j
WHERE j.status = 'completed' AND j.workflow_id = 'cmgd_nextflow' AND j.workflow_version = '2.2.1'
  AND NOT EXISTS (SELECT 1 FROM etl_ingested e
                  WHERE e.sample_id = j.sample_id
                    AND e.workflow_id = j.workflow_id AND e.workflow_version = j.workflow_version)
ORDER BY j.completed_at;
```

Restart-safe, re-run-safe, and the source of the backlog count. Mirrors the existing `sweep_run_incomplete` reconcile pattern.

---

## Phases (each with its completion check)

### Phase 0 — Ground truth & credentials
Prove the environment before writing engine code.
- Confirm the exact `<publish_base>` for 2.2.1 from the **deployed client config** (not a guess).
- Pull one *known-completed* 2.2.1 sample (from the watermark query) and diff its `rclone lsf -R` against the expected
  spec inventory: `manifest.json`, `MARK_COMPLETE`, `full_data/` + `rarefied_data/`, the metaphlan/bracken/gtdb/resistome step files.
- Prove creds from onclappc02: `rclone lsd r2:` and `rclone lsd gs1:` succeed; psql `SELECT 1`; DuckDB `httpfs` reads a test
  parquet from R2 via an R2 `CREATE SECRET`.

**✅ Done when:** a captured listing of a real 2.2.1 sample matches the spec inventory; `nf-etl status` (stub) prints the
count of completed 2.2.1 samples from Postgres; a one-liner has DuckDB list an R2 bucket and read a probe parquet back.

### Phase 1 — 2.2.1 `OutputSpec` + parsers (offline)
Implement `SPECS[('cmgd_nextflow','2.2.1')]` + parser functions + the `commented_tsv` helper + the manifest parser
(per the ETL doc). No DB, no lake.

**✅ Done when:** `nf-etl parse --sample <id> --dry-run` prints per-logical-table row counts and columns that match
hand-inspection of the raw files; `tests/test_etl_parsers.py` passes on a committed tiny fixture and asserts the three
transforms that forced code over YAML — percent→fraction unit normalization, degenerate-presence collapse, and taxid
extraction from the clade lineage.

### Phase 2 — Internal DuckLake, one sample end-to-end
Stand up the internal lake (Postgres catalog DB + R2 data path), create tables with the design's partitioning
(`workflow / version / method / data_type`), ingest **one** sample, write its `etl_ingested` row.

**✅ Done when:** `SELECT count(*) FROM cmgd_lake.taxonomic_profile WHERE sample_id=<id>` equals the Phase-1 count;
the R2 lake prefix contains parquet; **re-running the same sample is a no-op** (counts unchanged, no duplicate rows);
`ducklake_snapshots()` shows the new snapshot.

### Phase 3 — Batch engine + MARK_COMPLETE gate + reconcile
`nf-etl ingest` processes the whole pending set in a batch: HEAD-gate each sample on `MARK_COMPLETE`, parse, write,
one transaction, then insert `etl_ingested`.

**✅ Done when:** seeding a backlog of K completed samples and running `ingest` writes K `etl_ingested` rows and lake
counts = Σ per-sample; a sample **missing MARK_COMPLETE** is left pending and gets picked up on the next run once the
object appears; an induced mid-batch failure leaves **no half-ingested sample** (watermark and lake agree).

### Phase 4 — Trigger + cadence (the 500 gate)
`nf-etl tick` implements the backlog/threshold/age logic and the conditional freeze; install a **systemd timer**
(or cron) on onclappc02.

**✅ Done when:** with backlog < 500 a tick is a logged no-op (`backlog 137 < 500, skipping`); when backlog crosses 500
the tick ingests and advances the watermark; `systemctl list-timers` shows the unit firing on schedule; `nf-etl status`
prints last-tick, backlog, last-ingest, last-freeze.

### Phase 5 — Frozen public catalog (same files, HTTPS)
`nf-etl freeze`: snapshot the internal lake's **catalog** into a frozen **DuckDB file** (`cmgd.duckdb`) whose data-file
references point at the **same R2 parquet over public HTTPS** — not a data copy. Publish the DuckDB file at the existing
public entry point; the data files are already in place from ingest. Requires the relative/overridable data paths and
the retention rule from [the constraints above](#one-file-set-two-catalogs--the-constraints).

**✅ Done when:** from a **clean client with no Postgres and no R2 keys**, `ATTACH`ing the published `cmgd.duckdb` and
querying returns the same row counts as the internal lake, with every data read served over **HTTPS GET**; a re-freeze
produces a new immutable catalog snapshot while the prior one stays readable (time travel preserved).

### Phase 6 — Observability & staleness alert (thin)
Log every tick; a deliberately-stuck ingest (backlog growing for > T hours with no successful ingest) surfaces via
`nf-etl status` and a log-level alert — same shape as the `heartbeat-watchdog`. Update the design docs' "next steps".

**✅ Done when:** a forced stall shows up in `status` and emits the alert line; docs updated; the timer has run
unattended for a full cadence cycle (samples complete → appear in the internal lake within one tick → daily freeze
updates the public artifact).

---

## Credentials & config (all present on onclappc02)
- **Postgres** — reuse `SQLALCHEMY_URI` from the app env; the catalog DB is a separate database on the same instance.
- **R2 internal (S3)** — DuckDB `CREATE SECRET (TYPE s3, ENDPOINT <r2>, KEY/SECRET …)` for ingest + internal reads; keys
  already available (rclone `r2:`).
- **R2 external (HTTPS)** — the same bucket's public HTTPS base for the frozen catalog's data reads; **GET-only, no LIST**
  (fine — the catalog enumerates every file). Matches the R2 public-access model.
- **GCS read** — reuse the working `gs1:` rclone remote (or `gcsfs`) to fetch source files; no new cred wiring.
- Tunables in `config.py`: `INGEST_THRESHOLD=500`, `MAX_AGE=24h`, `TICK=15m`, `FREEZE_CADENCE=daily`,
  `ETL_CATALOG_URI`, `ETL_LAKE_DATA_PATH` (one R2 bucket/prefix), `ETL_PUBLIC_HTTPS_BASE`, `ETL_PUBLISH_BASE`.

## Open confirmations (close in Phase 0)
- Exact `<publish_base>` for 2.2.1 (from deployed client config).
- **Relative / overridable data paths** in DuckLake — the load-bearing assumption of the shared-file model (one parquet
  set served over both `s3://` and `https://`). Verify before anything else.
- Whether any table is **internal-only** (e.g. the ~89% marker rows) → public/private prefix split within the bucket.
- Retention: internal compaction / `expire_snapshots` must not delete parquet a published frozen catalog references.
- The single R2 bucket/prefix for the shared lake data (per `storage-layout.md`; dedicated cmgd lake, not a shared schema).
