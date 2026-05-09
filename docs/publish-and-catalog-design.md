# Publish & catalog design

> Status: **draft** — supersedes the experimental [`curatedMetagenomicDataETL`](https://github.com/seandavi/curatedMetagenomicDataETL) approach. Read alongside [`sample-metadata-design.md`](./sample-metadata-design.md): that doc is about sample *inputs* and harmonization; this one is about pipeline *outputs* and the analytical catalog.

## Framing

Three observations the design rests on:

1. **The Nextflow pipeline's `publishDir` layout is fully deterministic from operational state telemetry already owns.**
    ```
    ${publish_base_dir}/${workflow.manifest.name}/${workflow.manifest.version}/${meta.sample}/[${branch}/]${step_name}/
    ```
    Every component is in `workflows` (`workflow_id`, `version`) or `samples` (`sample_id`) today. `jobs_tbl`'s `UniqueConstraint(sample_id, workflow_id, workflow_version)` is the publish-path identity. No new "analyses" entity is needed in the operational schema — it's already there, implicitly.

2. **Telemetry is the operational source of truth.** It knows what samples exist, which jobs have completed, and (with one new column) where each job published its output. It can be the SoT for pipeline outputs in the catalog too — no separate ETL needs to discover them by globbing S3.

3. **The group's analytical surface is moving toward DuckLake (DuckDB v1.0).** Postgres-backed catalog over parquet (and other) files in object storage. Multiple producers will share it: omicidx, bugsigdb, PMC fulltext (Dagster-managed); pipeline outputs (this design).

What this rules out:
- A separate ETL pipeline that globs S3 to discover what got produced (today's `curatedMetagenomicDataETL`).
- A `sample_id_map.csv` artifact carried independently of telemetry.
- Promoting taxonomic/marker counts into telemetry's Postgres schema. They live in object storage; the catalog points at them.
- Routing pipeline-output cataloging through Dagster. The dependency graph is one edge (completion → register); telemetry already sits on the source side of that edge.

What stays in scope:
- A clear contract for the publish path.
- A small change to telemetry's schema and HTTP surface so the catalog can be populated event-driven from completion.
- A stable public parquet artifact at known URLs for external consumers.

## Two layers

```
┌─────────────────────────────────────────────────────────────┐
│  telemetry server (this repo) — operational SoT             │
│  Postgres                                                    │
│   - samples, workflows, jobs, workflow_runs, MARK_COMPLETE   │
│   - + workflows.publish_base_uri                             │
│  HTTP                                                        │
│   - + GET /api/published                                     │
│  On completion → write to catalog DB (idempotent, async)     │
└──────────────────┬──────────────────────────────────────────┘
                   │ catalog write
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  DuckLake catalog (DuckDB v1.0)                             │
│  - same Postgres instance, separate database                 │
│  - manifest + table metadata managed by DuckDB               │
│  - reads pipeline outputs (TSV.gz) directly — no transform   │
│  Other tenants (Dagster-published):                          │
│  - omicidx (BioSample/BioProject/SRA)                        │
│  - bugsigdb                                                  │
│  - PMC fulltext                                              │
└──────────────────┬──────────────────────────────────────────┘
                   │ read access (group internal, ATTACH)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Public parquet artifact                                     │
│  - periodic COPY from DuckLake views, full replace           │
│  - stable URLs at s3://cmgd-export/<dataset>/study_name=…/  │
│  - optional ATTACH-able cmgd.duckdb shim                     │
└─────────────────────────────────────────────────────────────┘
```

## Telemetry-side changes

Three concrete deltas, each independently shippable.

### 1. `workflows.publish_base_uri`

```python
ALTER TABLE workflows
    ADD COLUMN publish_base_uri text;     -- e.g. 's3://gs-cmgd-mirror'
```

Per `(workflow_id, version)`, nullable. NULL means "this workflow's outputs aren't cataloged" (test workflows, ad-hoc runs). Nullable rather than NOT NULL because the column has to backfill onto historical workflow rows that predate the catalog.

Per-execution-site overrides (anvil → S3, google profile → GCS) are deferred. If they materialize as a real concern, the column moves to `workflow_runs`. For now we assume one publish location per (workflow, version).

### 2. `GET /api/published`

```
GET /api/published?workflow_id=cmgd_nextflow&version=1.6.0&since=2026-05-01
```

Joins `jobs` (status=`completed`) × `samples` × `workflows`, returns:

```json
{
  "items": [
    {
      "sample_id": "abc123…",
      "run_ids": "SRR1;SRR2",
      "sample_name": "…",
      "study_name": "…",
      "workflow_id": "cmgd_nextflow",
      "workflow_version": "1.6.0",
      "publish_prefix": "s3://gs-cmgd-mirror/cmgd_nextflow/1.6.0/abc123…",
      "completed_at": "…"
    }
  ],
  "next_since": "…"
}
```

Useful even before DuckLake exists — it's a structured replacement for the current ETL's `glob() + split(file, '/')[7]` discovery path. Optional `?expand_steps=true` returns one row per `(sample × step)` with the full per-step prefix; the step list is a constant in code keyed by `workflow_id`.

### 3. On-completion catalog write

A new `CatalogPublisher` service called from the `MARK_COMPLETE → completed` transition. Connects to the catalog DB (separate connection string in `Settings`). Idempotent on `(workflow_id, version, sample_id)`. Failure to write does **not** roll back the completion — telemetry's operational state is authoritative; catalog drift is recoverable.

Reconcile path: a periodic sweep finds completed jobs whose catalog rows are missing and registers them. Same pattern as `sweep_run_incomplete()`. Means a catalog DB outage causes no data loss, only delayed visibility.

## DuckLake catalog

### Location

Same Postgres instance as telemetry, separate database. Avoids cross-cluster joins; keeps the operational and analytical schemas from contaminating each other (different consumers, different uptime profiles, different schema-evolution cadences).

### Source format

TSV.gz, read directly via DuckDB. **No conversion step.** The Nextflow pipeline's `publishDir` output is the on-disk SoT.

### Logical tables

DuckLake exposes one logical table per dataset (`marker_abundance`, `marker_presence`, `marker_rel_ab_w_read_stats`, `metaphlan_unknown_list`, `metaphlan_viruses_list`, future: `humann_genefamilies`, `humann_pathabundance`). Each is a DuckDB read over the union of TSV.gz files at the registered prefixes, joined to the catalog's identity columns (`sample_id`, `study_name`, `run_ids`).

### Other tenants

- **omicidx** — BioSample / BioProject / SRA metadata. Already used by `scripts/load_bioproject.py` (read from a parquet file). Dagster-published.
- **bugsigdb** — curated microbial signatures.
- **PMC fulltext** — pulled for harmonization downstream (see [`sample-metadata-design.md`](./sample-metadata-design.md)).

All share the same DuckLake. Joins on accession (`run_id`, `study_id`, etc.) are first-class because every tenant publishes into the same catalog.

## Public parquet artifact

Group-internal users hit DuckLake. External consumers want stable parquet URLs without DuckLake / DuckDB-version dependencies. We keep that surface:

- A periodic job (cron / Dagster sensor / cron-equivalent) runs:
  ```sql
  COPY (SELECT * FROM ducklake.marker_abundance)
  TO 's3://cmgd-export/marker_abundance/'
  (FORMAT PARQUET, PARTITION_BY 'study_name', COMPRESSION 'zstd');
  ```
  Full replace per dataset. No incremental machinery — at our scale a periodic full rewrite is simple and the right tradeoff vs. the operational complexity of incremental.
- Stable URLs preserved: `s3://cmgd-export/<dataset>/study_name=…/` continues to be the public address (same shape `curatedMetagenomicDataETL` already publishes).
- Optional `cmgd.duckdb` ATTACH-able shim at `https://minio.cancerdatasci.org/cmgd-export/cmgd.duckdb`, kept for backward compatibility with the existing public consumer entry point.

This is the only surviving "ETL"-shaped step in the design, and it's a single `COPY` per dataset on a schedule.

## What replaces `curatedMetagenomicDataETL`

The repo's three jobs all become obsolete:

| Today | Target |
|---|---|
| Glob `s3://gs-cmgd-mirror/**` + parse `split(file, '/')[7]` for `sample_id` | Telemetry catalog write at completion; `/api/published` for on-demand discovery |
| Convert TSV.gz to partitioned parquet | DuckLake reads TSV.gz directly; public parquet is a periodic full-replace `COPY` |
| Maintain `sample_id_map.csv` (4.6 MB checked in) | DuckLake view over `samples ⨝ curated_sample_annotations` |

Migration:

1. This repo gains `workflows.publish_base_uri` + `/api/published` + the catalog-write hook.
2. DuckLake catalog stood up with one tenant (pipeline outputs).
3. Public parquet generator runs alongside the existing ETL output for a transition window.
4. Cross-check generated parquet against the existing public artifact; once verified equivalent, deprecate `curatedMetagenomicDataETL` and archive the repo. The public URL doesn't change.

## Open questions

1. **DuckLake's representation of TSV.gz as a logical table**: needs verification. DuckLake v1.0 is parquet-native; non-parquet inputs may need wrapping (a DuckDB view that reads via `read_csv`, registered as a logical table). Worst case we shim TSV.gz → parquet at registration time; that's a per-sample one-time cost, still no global ETL pass.
2. **Catalog-write idempotency key**: probably `(workflow_id, workflow_version, sample_id)` — the same composite as `jobs_tbl`.
3. **Schema evolution on MetaPhlAn version bumps**: if `marker_abundance` columns change, do we tag rows with the producing version, or partition logical tables per version so consumers opt in? Lean toward the latter — a `marker_abundance_v4` / `marker_abundance_v5` split with a union view. Avoids silent column drift across years of data.
4. **Branch dimension**: the `full_data` / `rarefied_data` path component currently splits each step's output. Expose as a partition column on the logical table, or as separate logical tables (`marker_abundance` vs `marker_abundance_rarefied`)? Lean partition column — it's a small dimension and consumers will want both reachable.
5. **Catalog-write failure mode**: synchronous (blocks completion-write but logged) vs. async (fire-and-forget with reconcile). Position above is async. Worth pushback if there's a reason completion shouldn't be observable until cataloged.
6. **Public parquet generator location**: in this repo (sibling to `nf_client`), in a separate package, or as a Dagster asset on the existing instance? Lean in this repo for now (`packages/cmgd_publish/`?) — small enough that a separate repo is overhead. Migrating to Dagster later is mechanical if the catalog grows complex enough.
7. **HUMAnN outputs**: not yet emitted by the pipeline (`skip_humann=true`). When that flips, the catalog gains gene-family / pathway tables. Reach-ready under this design — same publish-prefix → register pattern, just new step names.
8. **Per-execution-site publish locations**: anvil → S3, google profile → GCS. Today's design assumes one location per (workflow, version). If multi-site execution is real, `publish_base_uri` moves to `workflow_runs`. Defer until it bites.

## Suggested next steps

Smallest first. Each independently shippable.

1. **Migration: `workflows.publish_base_uri`** (trivial). One column. Backfill the existing workflow.
2. **`GET /api/published`** (small). Read-only, joins existing tables. Useful pre-DuckLake.
3. **DuckLake catalog DB scaffolding** (medium). New database, DuckDB-managed schema, connection settings.
4. **Catalog-write hook on completion + reconcile sweep** (small). Mirrors existing service patterns.
5. **Logical tables / views over pipeline outputs** (small per dataset).
6. **Public parquet generator** (small). One `COPY` per dataset, scheduled.
7. **Deprecate `curatedMetagenomicDataETL`** (after #6 is verified against the existing public artifact).

That's the full telemetry-side roadmap for catalog publishing.

## What this design explicitly does NOT do

- No `analyses_tbl` in telemetry. The implicit `(jobs.completed × samples × workflows)` is sufficient.
- No taxonomic / marker / functional tables in telemetry's Postgres.
- No transform step in the ingestion path. TSV.gz stays as-is; the only conversion is the periodic public-parquet rewrite.
- No Dagster integration in this repo. The existing group Dagster owns SRA/biosample metadata; pipeline outputs are catalog-side via telemetry directly.
- No coupling between telemetry uptime and catalog availability. Catalog write failure is async and recoverable.
- No incremental public-parquet machinery. Full replace is simple and cheap at our scale.
- No Iceberg. DuckDB writes to Iceberg are not yet first-class; DuckLake is the chosen format.

If a feature belongs in the analytical catalog, it doesn't belong in telemetry's Postgres schema. That's the line.
