# Accessing the cMD data (consumer guide)

> **Audience:** researchers and data scientists who want to *use* the curated metagenomic (cMD) profiles — not run the pipeline. If you want to query taxonomic profiles, markers, resistome, or per-sample QC across studies, start here.
>
> **Status:** the public artifact is published by the ETL's frozen-lake step (issue #57, Phase 5). Until that lands, the URLs below are placeholders marked _(pending)_. The **schema and access patterns are stable** — this guide is written against them.

## What the data is

A periodically-refreshed, read-only snapshot of the outputs of the `cmgd_nextflow` pipeline: per-sample microbial **taxonomic profiles**, **strain markers**, **antimicrobial-resistance (resistome)** calls, and **QC/provenance**, harmonized across many studies. It is published as a **frozen DuckLake** — one DuckDB catalog file plus Parquet data, all read over plain **HTTPS** (no credentials, no accounts).

Two equivalent ways in:

| You want… | Use |
|---|---|
| SQL across the whole dataset, joins, one connection | **Attach the frozen catalog** (below) |
| A single known Parquet file (or S3-credentialed bulk access) | **Read a Parquet URL** directly |

Everything is DuckDB-native, but the Parquet files are readable by any Arrow/Parquet client.

## Quick start — attach the catalog

You need [DuckDB](https://duckdb.org) ≥ 1.0 (CLI, Python, R, or Node). Nothing else.

```sql
-- DuckDB CLI or any client
INSTALL httpfs; LOAD httpfs;
ATTACH 'https://data.cmgd.cancerdatasci.org/cmgd.duckdb' AS cmgd (READ_ONLY);   -- (pending)
SHOW TABLES;                    -- see the tables below
SELECT * FROM cmgd.taxonomic_profile LIMIT 5;
```

That single file is a **catalog** — the actual data is fetched from the shared Parquet store over HTTPS on demand (range GETs), so attaching is instant and you only download the columns/rows your query touches.

Python:

```python
import duckdb
con = duckdb.connect()
con.sql("INSTALL httpfs; LOAD httpfs;")
con.sql("ATTACH 'https://data.cmgd.cancerdatasci.org/cmgd.duckdb' AS cmgd (READ_ONLY)")  # (pending)
df = con.sql("SELECT * FROM cmgd.taxonomic_profile WHERE study_name = 'ArtachoA_2021'").df()
```

R (via `duckdb`/`DBI`):

```r
con <- DBI::dbConnect(duckdb::duckdb())
DBI::dbExecute(con, "INSTALL httpfs")   # one statement per call in the R client
DBI::dbExecute(con, "LOAD httpfs")
DBI::dbExecute(con, "ATTACH 'https://data.cmgd.cancerdatasci.org/cmgd.duckdb' AS cmgd (READ_ONLY)")  # (pending)
df <- DBI::dbGetQuery(con, "SELECT * FROM cmgd.qc_metrics LIMIT 100")
```

## Quick start — read Parquet directly

If you don't want the catalog, you can read Parquet straight from HTTPS — but with one constraint: **plain HTTPS can't list a directory**, so DuckDB's `httpfs` can't expand a `**/*.parquet` glob over `https://`. Two honest options:

- **A single known file** — read one explicit URL directly:
  ```sql
  INSTALL httpfs; LOAD httpfs;
  SELECT clade_name, relative_abundance
  FROM read_parquet('https://data.cmgd.cancerdatasci.org/parquet/taxonomic_profile/version=2.2.1/method=metaphlan/data_type=full_data/part-0.parquet')  -- (pending, one file)
  WHERE rank = 'species' LIMIT 20;
  ```
- **Many files / whole tables** — **attach the catalog** instead (top of this page). The catalog *enumerates* every Parquet file, so DuckDB never needs to list a directory — this is the supported way to query across files over HTTPS. (If you have S3/R2 credentials for the bucket, globbing works over the `s3://` endpoint, where LIST is available.)

The Parquet is partitioned by `workflow` / `version` / `method` / `data_type`, so once files are enumerated (via the catalog) those filters prune to the relevant files.

## The tables

Every row carries the identity keys: **`sample_id`** (the join key — `md5` of the sorted run accessions), **`study_name`**, and **`run_ids`** (e.g. `SRR…;SRR…`).

**Fact tables** additionally carry **`workflow`**, **`version`** (the pipeline version — *filter this if you don't want to aggregate across versions*), and **`data_type`** (`full_data` = all reads, `rarefied_data` = 1M-read subsample). The two **dimension tables** (`qc_metrics`, `taxon`) do **not** carry `data_type`: `qc_metrics` is exactly one row per sample, `taxon` is one row per (taxon, `db_version`).

| table | kind | one row per | columns beyond the identity keys |
|---|---|---|---|
| `taxonomic_profile` | fact | sample × method × taxon × `data_type` | `method` (`metaphlan`\|`bracken`), **`clade_name`** (label as the profiler reported it), `rank`, `ncbi_taxid`, `sgb_id` (metaphlan only), `relative_abundance` |
| `marker_abundance` | fact | sample × marker × `data_type` | `marker_name`, `value` |
| `marker_presence` | fact | sample × present marker × `data_type` | `marker_name` (membership — a row exists iff the marker is present) |
| `resistome` | fact | sample × AMR gene × `data_type` | `gene` (CARD reference), `template_coverage`, `template_identity`, `depth`, `score` (from KMA/CARD `card_kma.res`) |
| `qc_metrics` | **dimension** | sample | `reads_raw`, `reads_decontaminated`, `bases_raw`, `bases_decontaminated`, `reads_surviving_fraction`, `bases_surviving_fraction`, `metaphlan_index` (reference DB version), `pipeline_version`, `git_commit` |
| `taxon` | **dimension** | taxon × `db_version` | `taxon_key`, **`db_version`**, `ncbi_taxid`, `sgb_id`, `ncbi_species`, `rank`, `genus`, `family`, `phylum` |

**`method` values.** In the current `2.2.1` snapshot, `method` is `metaphlan` or `bracken` — the pipeline does not run gtdb, so there is no `gtdb` method here. (Earlier design notes mention a unified metaphlan/bracken/gtdb table; gtdb applied to older pipeline versions. If a future version reintroduces it, it appears as another `method` value in the same table.)

**Two name columns, on purpose.** `taxonomic_profile.clade_name` is the label *exactly as the profiler reported it* (metaphlan's SGB lineage, bracken's binomial). For a canonical / cross-method name, or to roll up to `genus`/`family`/`phylum`, join the **`taxon`** dimension. `metaphlan` rows carry both `sgb_id` and `ncbi_taxid` (join on `sgb_id` for the most precise match); `bracken` rows carry `ncbi_taxid` only.

**Joining `taxon` correctly.** `taxon` has one row per taxon *per `db_version`*, so join on **both** the taxon key **and** `db_version`, or rows fan out. A sample's `db_version` is its `qc_metrics.metaphlan_index`. If a snapshot has a single `db_version` (common), filter `taxon` to that one value once and forget it.

**Per-sample QC/provenance lives once in `qc_metrics`** (not repeated on every clade row) — join it on `sample_id` for read depth or the reference DB version.

## Worked examples

First, discover what's in the snapshot:

```sql
SELECT DISTINCT study_name FROM cmgd.taxonomic_profile ORDER BY 1;   -- studies
SELECT DISTINCT version    FROM cmgd.taxonomic_profile;              -- pipeline versions present
```

Find the `sample_id` for a run accession you care about (the join key isn't the SRR — look it up):

```sql
SELECT DISTINCT sample_id, run_ids
FROM cmgd.qc_metrics
WHERE run_ids LIKE '%SRR19065012%';
```

Top 10 species in a study (metaphlan, full-read, one pipeline version):

```sql
SELECT clade_name, avg(relative_abundance) AS mean_rel_ab
FROM cmgd.taxonomic_profile
WHERE study_name = 'ArtachoA_2021'
  AND method = 'metaphlan' AND rank = 'species'
  AND data_type = 'full_data'
  AND version = '2.2.1'                          -- pin the version, or you aggregate across versions
GROUP BY clade_name ORDER BY mean_rel_ab DESC LIMIT 10;
```

One sample's profile joined to canonical taxonomy — note the `taxon` join includes `db_version` (from `qc_metrics.metaphlan_index`) and uses `sgb_id`, the precise key metaphlan carries:

```sql
WITH s AS (
  SELECT metaphlan_index AS db_version
  FROM cmgd.qc_metrics
  WHERE sample_id = '04835269dd64216afea75569f36f2f6c'
)
SELECT p.relative_abundance, t.ncbi_species, t.genus, t.phylum
FROM cmgd.taxonomic_profile p
JOIN cmgd.taxon t
  ON t.sgb_id = p.sgb_id
 AND t.db_version = (SELECT db_version FROM s)   -- both keys, or rows fan out
WHERE p.sample_id = '04835269dd64216afea75569f36f2f6c'
  AND p.method = 'metaphlan' AND p.data_type = 'full_data'
ORDER BY p.relative_abundance DESC;
```

Sample count and mean read depth per study (`qc_metrics` is one row per sample, so `count(*)` is the sample count and the read depths are unambiguous — no `data_type` to mix):

```sql
SELECT study_name, count(*) AS n_samples, avg(reads_decontaminated) AS mean_depth
FROM cmgd.qc_metrics GROUP BY study_name ORDER BY n_samples DESC;
```

## Two things to know before you compute

1. **Abundances are not directly comparable across methods.** `metaphlan` relative abundance is marker/genome-size-normalized (a percent); `bracken` `relative_abundance` is a read-count fraction. The `method` column tells you which. Matching a *taxon* across methods (via `taxon`) unifies *who*, not *how much*.
2. **`data_type` matters.** `full_data` uses all reads; `rarefied_data` is a 1M-read subsample (for depth-controlled comparisons). Pick one — don't mix them in an aggregate. Most analyses want `full_data`.

## Citing / reproducibility

The published catalog is a **frozen point-in-time snapshot** — "the cMD lake as of `<date>`" is a concrete, immutable artifact. Record the snapshot date (or catalog URL, which is versioned) in your methods so your analysis is reproducible. Newer snapshots add samples and may add columns under a new pipeline `version`; historical rows keep their shape (filter on `version` if you need a fixed schema).

## Relationship to `curatedMetagenomicData`

This is the successor publishing path to the experimental `curatedMetagenomicDataETL`. The public Parquet URLs keep the same shape existing consumers expect; the frozen DuckLake is the new, richer entry point. If you use the Bioconductor `curatedMetagenomicData` package, that surface continues to work — this guide is for direct SQL/Parquet access to the current pipeline outputs.

## Getting help

- Schema / design: `docs/output-catalog-etl-design.md`.
- Something missing or a URL 404s: open an issue referencing #57.
