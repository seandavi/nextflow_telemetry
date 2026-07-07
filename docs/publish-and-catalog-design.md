# Publish & catalog design

> Status: **draft** — supersedes the experimental [`curatedMetagenomicDataETL`](https://github.com/seandavi/curatedMetagenomicDataETL) approach. Read alongside [`sample-metadata-design.md`](./sample-metadata-design.md): that doc is about sample *inputs* and harmonization; this one is about pipeline *outputs* and the analytical catalog. For how published files become catalog *tables* (the file→table ETL, analytical schema, and taxonomy harmonization), see [`output-catalog-etl-design.md`](./output-catalog-etl-design.md).

## Framing

Three observations the design rests on:

1. **The Nextflow pipeline's `publishDir` layout is fully deterministic from operational state telemetry already owns.**
    ```
    ${publish_base_dir}/${workflow.manifest.name}/${workflow.manifest.version}/${meta.sample}/[${branch}/]${step_name}/
    ```
    Every component is in `workflows` (`workflow_id`, `version`) or `samples` (`sample_id`) today. `jobs_tbl`'s `UniqueConstraint(sample_id, workflow_id, workflow_version)` is the publish-path identity. No new "analyses" entity is needed in the operational schema — it's already there, implicitly.

2. **Telemetry is the operational source of truth.** It knows what samples exist, which jobs have completed, and (with one new column) where each job published its output. It can be the SoT for pipeline outputs in the catalog too — no separate ETL needs to discover them by globbing S3.

3. **The group's analytical surface is moving toward DuckLake (DuckDB v1.0).** Postgres-backed catalog over parquet (and other) files in object storage. Several sibling lakes live on it — omicidx, bugsigdb, PMC fulltext each publish their own; pipeline outputs (this design) get a **dedicated cmgd lake** (see [Location](#location)).

What this rules out:
- A separate ETL pipeline that globs S3 to discover what got produced (today's `curatedMetagenomicDataETL`).
- A `sample_id_map.csv` artifact carried independently of telemetry.
- Promoting taxonomic/marker counts into telemetry's Postgres schema. They live in object storage; the catalog points at them.
- Routing pipeline-output cataloging through a workflow orchestrator (Dagster / Prefect / Airflow). The dependency graph is one edge (completion → register); telemetry already sits on the source side of it, so scheduling is plain scripts, not an orchestrator.

What stays in scope:
- A clear contract for the publish path.
- A small change to telemetry's schema and HTTP surface so the catalog can be populated event-driven from completion.
- A stable public artifact at known URLs for external consumers (a frozen DuckDB-catalog snapshot over the shared parquet, read via HTTPS).

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
│  DuckLake — dedicated cmgd lake                             │
│  - Postgres catalog; full snapshot history                 │
│  - manifest + table metadata managed by DuckDB               │
│  - fed by a thin file→table ETL (see output-catalog doc)    │
│  sibling lakes (separate, ATTACHed):                          │
│  - omicidx (BioSample/BioProject/SRA)                        │
│  - bugsigdb                                                  │
│  - PMC fulltext                                              │
└──────────────────┬──────────────────────────────────────────┘
                   │ periodic freeze (script)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Frozen published DuckLake                                     │
│  - DuckDB-file catalog, ATTACH-able (no keys)           │
│  - same R2 parquet, over public HTTPS (no keys)  │
│  - frozen cmgd.duckdb IS the catalog now                     │
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

**A dedicated cmgd DuckLake — not co-tenanted.** The working catalog DB is Postgres (same instance as telemetry, separate database), which keeps operational and analytical schemas from contaminating each other and avoids cross-cluster joins. But the *lake* is cmgd-only: pipeline outputs get their **own** DuckLake catalog, separate from omicidx/bugsigdb/PMC, **specifically so its snapshot history supports full time travel** without being churned by other tenants' publish cadences. DuckLake's time travel is a property of the catalog — a shared catalog interleaves every tenant's snapshots into cmgd's history; a dedicated one keeps "the catalog as of date X" meaningful for cmgd alone.

### Source format

TSV.gz is the on-disk SoT (the Nextflow pipeline's `publishDir` output). **Correction:** these are not loaded zero-transform — a thin, deterministic file→table ETL is required (comment headers, per-tool unit differences, differing grain). See [`output-catalog-etl-design.md`](./output-catalog-etl-design.md), which owns the file→table mechanism.

### Logical tables

**The analytical schema is owned by [`output-catalog-etl-design.md`](./output-catalog-etl-design.md).** In brief: tables split by `(grain, datatype)`, **not** one-per-file — e.g. a single `taxonomic_profile` unifies metaphlan/bracken/gtdb (with a `method` discriminator) rather than three separate tables, marker tables are their own grain (and dominate row count ~89%), and per-sample provenance/QC lives once in a `qc_metrics` dimension. Each fact table joins to the catalog's identity columns (`sample_id`, `study_name`, `run_ids`). The file→table mapping mechanism lives in the [spec layer](#output-spec-layer), whose format that doc defines.

### Sibling lakes

- **omicidx** — BioSample / BioProject / SRA metadata. Already used by `scripts/load_bioproject.py` (read from a parquet file). Its own lake, separately published.
- **bugsigdb** — curated microbial signatures.
- **PMC fulltext** — pulled for harmonization downstream (see [`sample-metadata-design.md`](./sample-metadata-design.md)).

Each is its **own** lake, not co-tenanted with cmgd (see [Location](#location)). Cross-lake joins on accession (`run_id`, `study_id`, etc.) stay first-class via DuckDB `ATTACH` — one query spanning several lakes — without interleaving their snapshot histories into cmgd's time travel.

## Output spec layer

Telemetry stores `(workflow_id, version, sample_id)` and a deterministic publish prefix; it does **not** store which files exist under that prefix or how to parse them. Nextflow's `-with-weblog` reporter carries trace events but no file manifest, and the `trace` JSONB column has nothing publish-related in it. So between the publish prefix and a DuckLake logical table there's an irreducible mapping step: a per-workflow declaration of "these are the files we expect, and this is how to read them."

### Shape

**The spec format and the file→table engine are defined in [`output-catalog-etl-design.md`](./output-catalog-etl-design.md)** — do not duplicate them here. In brief: a per-`(workflow_id, version)` registry of Python `OutputSpec` dataclasses (`path, table, parser, tags`) in `src/nextflow_telemetry/etl/`, not YAML. The reason it's code and not declarative config: the transforms the forensics turned up (normalizing percent↔fraction, parsing a taxid out of a lineage string, collapsing degenerate presence rows) can't be expressed as `read_csv` options — parsers are plain `bytes -> Iterable[dict]` functions. Adding a workflow version = one registry entry (+ a parser only for a genuinely new file shape).

### File discovery

Never LIST — the trigger reconstructs every publish path from telemetry's `(workflow_id, version, sample_id)` keys and gates ingestion on a HEAD of the `MARK_COMPLETE` object. The [ETL doc's "Trigger" section](./output-catalog-etl-design.md) owns this; the `/api/published` endpoint below is the same event-driven primitive exposed over HTTP. (Globbing 500k publish prefixes is a non-starter, which is why it isn't done.)

### Spec authoring & drift

Bootstrap the first spec by pulling a handful of completed samples and inspecting interactively; validate the parsers against a held-out batch before shipping. Once one spec exists as ground truth, two agent-assisted, HITL-on-output steps are worth scripting: **onboarding** (a sniffer proposes a new version's specs by structural similarity, opens a draft PR) and **drift detection** (a scheduled job re-parses a held-out batch and flags parser/schema failures). Both would run as a separate Agent-SDK job with API credentials, not this repo's runtime.

### Schema evolution

When MetaPhlAn versions bump and column shapes change, **version is the dimension we use**: a new registry entry for the new `(workflow_id, version)`, never a mutation of the old one. `version` is a partition key on the fact tables (see the ETL doc's partitioning: `workflow / version / method / data_type`), so historical rows keep their shape and consumers filter — or opt into a latest-version view — rather than silently reading column-drifted data. This resolves open question #3.

## Published artifact: frozen DuckLake

The **working** cmgd lake is live and writable (Postgres catalog, ingesting on every completion). The **published** artifact is a periodic **frozen snapshot** of it that uses a **DuckDB file as the metadata catalog** instead of Postgres. Crucially, the freeze **does not copy the data** — internal and external share the **same R2 bucket and the same parquet files**; only the catalog and access protocol differ (internal reads/writes over **S3 (R2)** with keys; external reads over **public HTTPS**, no credentials). The freeze snapshots the *catalog*, rewriting its data references to the public HTTPS base, and publishes the resulting `cmgd.duckdb`. See the execution plan's [shared-file constraints](./output-catalog-etl-plan.md) for what this couples.

Why frozen-DuckDB-catalog over the old "just parquet" plan:
- **Self-contained catalog + versioned.** Consumers get the whole schema and every table in one `ATTACH` at a known point in time — not a bag of parquet prefixes they must know the layout of. (Data is read from the shared bucket over HTTPS; no Postgres, no keys.)
- **No live-catalog dependency.** External users never touch the Postgres catalog or need it up.
- **Reproducible.** "The published lake as of 2026-07-01" is a concrete, immutable snapshot — re-runnable analyses cite it, not a moving target.
- **No data duplication.** The published parquet *is* the lake parquet, at its public HTTPS address — there's no separate `cmgd-export` COPY to keep in sync.

The `cmgd.duckdb` lands at the existing public entry point (`https://minio.cancerdatasci.org/cmgd-export/cmgd.duckdb` or the R2 public base) — now the frozen catalog itself, not a hand-built shim. Requires DuckLake's data paths to be relative / data_path-overridable so one file set resolves under both `s3://` and `https://` (verify first), and internal compaction must not delete parquet a published snapshot references.

## What replaces `curatedMetagenomicDataETL`

The repo's three jobs all become obsolete:

| Today | Target |
|---|---|
| Glob `s3://gs-cmgd-mirror/**` + parse `split(file, '/')[7]` for `sample_id` | Telemetry catalog write at completion; `/api/published` for on-demand discovery |
| Convert TSV.gz to partitioned parquet | Thin file→table ETL parses TSV.gz into DuckLake catalog tables (ETL doc); the public artifact is a periodic frozen-DuckLake snapshot |
| Maintain `sample_id_map.csv` (4.6 MB checked in) | DuckLake view over `samples ⨝ curated_sample_annotations` |

Migration:

1. This repo gains `workflows.publish_base_uri` + `/api/published` + the catalog-write hook.
2. DuckLake catalog stood up with one tenant (pipeline outputs).
3. Frozen-catalog publish runs alongside the existing ETL output for a transition window (the shared parquet also carries the legacy public URLs).
4. Cross-check generated parquet against the existing public artifact; once verified equivalent, deprecate `curatedMetagenomicDataETL` and archive the repo. The public URL doesn't change.

## Open questions

1. ~~**DuckLake's representation of TSV.gz as a logical table**~~ Resolved by the file→table ETL: TSV.gz is **parsed** to rows and **written** to catalog tables (the ETL doc), not read in place — so DuckLake's non-parquet-input question is moot. The remaining detail (parquet write target, partition layout) is owned by the ETL doc.
2. **Catalog-write idempotency key**: probably `(workflow_id, workflow_version, sample_id)` — the same composite as `jobs_tbl`.
3. ~~**Schema evolution on MetaPhlAn version bumps**~~ Resolved: `version` is a **partition key** on one logical table (not version-suffixed tables) — a new registry entry per `(workflow_id, version)`, historical rows keep their shape, consumers filter or opt into a latest-version view. See [Schema evolution](#schema-evolution). Avoids silent column drift across years of data.
4. ~~**Branch dimension**~~ Resolved: `full_data` / `rarefied_data` is a `data_type` discriminator **column** (and a partition key), not a separate table — same grain and datatype, just all-reads vs subsample. See the ETL doc.
5. **Catalog-write failure mode**: synchronous (blocks completion-write but logged) vs. async (fire-and-forget with reconcile). Position above is async. Worth pushback if there's a reason completion shouldn't be observable until cataloged.
6. **Publisher location**: the file→table ETL lives in-repo at `src/nextflow_telemetry/etl/` (per the ETL doc), with DuckDB/GCS deps in an optional dependency group so the API image stays lean. The frozen-DuckLake export job (below) is a small piece of that module (or a sibling script), not a separate package. Scheduling stays plain scripts (cron / docker-compose); no orchestrator.
7. **HUMAnN outputs**: not yet emitted by the pipeline (`skip_humann=true`). When that flips, the catalog gains gene-family / pathway tables. Reach-ready under this design — same publish-prefix → register pattern, just new step names.
8. **Per-execution-site publish locations**: anvil → S3, google profile → GCS. Today's design assumes one location per (workflow, version). If multi-site execution is real, `publish_base_uri` moves to `workflow_runs`. Defer until it bites.

## Suggested next steps

Smallest first. Each independently shippable.

1. **Migration: `workflows.publish_base_uri`** (trivial). One column. Backfill the existing workflow.
2. **`GET /api/published`** (small). Read-only, joins existing tables. Useful pre-DuckLake.
3. **DuckLake catalog DB scaffolding** (medium). New database, DuckDB-managed schema, connection settings.
4. **Catalog-write hook on completion + reconcile sweep** (small). Mirrors existing service patterns.
5. **First output spec** (small). Pull 5–10 completed samples from the active `cmgd_nextflow` version, author its `OutputSpec` registry entry + parsers in `src/nextflow_telemetry/etl/` interactively, validate against a held-out 10-sample batch. (Spec format & engine: the ETL doc.)
6. **Logical tables / views over pipeline outputs** (small per dataset, driven by #5).
7. **Frozen-catalog publish** (small). Snapshot the catalog to `cmgd.duckdb` with HTTPS data paths, scheduled — no data copy (same shared parquet).
8. **Deprecate `curatedMetagenomicDataETL`** (after #7 is verified against the existing public artifact).

That's the full telemetry-side roadmap for catalog publishing.

## What this design explicitly does NOT do

- No `analyses_tbl` in telemetry. The implicit `(jobs.completed × samples × workflows)` is sufficient.
- No taxonomic / marker / functional tables in telemetry's Postgres.
- The ingestion path has a thin, deterministic file→table transform (superseding the earlier "no transform" framing — see [`output-catalog-etl-design.md`](./output-catalog-etl-design.md)); the periodic public freeze is a catalog snapshot (no data rewrite — same shared parquet, over HTTPS), a separate, later step.
- No workflow orchestrator (Dagster / Prefect / Airflow) anywhere in this design. Scheduling is plain scripts (cron / docker-compose), restart-safe via the ETL watermark. The dependency graph is a single edge (completion → register); an orchestrator would be pure overhead.
- No coupling between telemetry uptime and catalog availability. Catalog write failure is async and recoverable.
- No incremental machinery. The public freeze is a full catalog snapshot (no data copy) — simple and cheap at our scale.
- No Iceberg. DuckDB writes to Iceberg are not yet first-class; DuckLake is the chosen format.
- No per-file telemetry. Nextflow's weblog doesn't carry publishDir info and we don't add a side-channel to capture it; the spec layer is how we know what files exist.

If a feature belongs in the analytical catalog, it doesn't belong in telemetry's Postgres schema. That's the line.
