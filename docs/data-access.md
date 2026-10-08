# Accessing the cMD data (consumer guide)

> **Audience:** researchers who want to *use* the curatedMetagenomicData (cMD)
> profiles, not run the pipeline.
>
> **Status:** the site is `https://cmgd-public.cancerdatasci.org`. Its first
> releases go up once the `cmgd-public` bucket (monode#52) exists; until then
> the URLs below return 404. Every code block on this page is run by the test
> suite against a local copy of a release (`tests/test_data_access_docs.py`), and
> `scripts/smoke_public_data.py` runs them against the live site.

## What is published

Everything is plain files over HTTPS: no accounts, no credentials. Each pipeline
**registration** (a pipeline version plus its configuration, ADR-0010) is its own
**dataset**, named `<workflow_id>-<version>`:

| dataset | what it holds |
|---|---|
| `cmgd_nextflow-2.2.1` | pipeline 2.2.1: MetaPhlAn 4.2.2 (vJan25), Bracken, resistome, markers, QC; samples keyed by the md5 `sample_id` |
| `cmgd_mpa4.2-2.3.0` | pipeline 2.3.0 without HUMAnN; samples keyed by readset id (`RS.…`) |
| `cmgd_humann3.9-2.3.0` | as above, plus HUMAnN 3.9: its own MetaPhlAn pass, pathway tables, gene-family downloads |
| `cmgd_humann4a1-2.3.0` | as above with HUMAnN 4.0.0a1 (no pathway coverage) |

A dataset is published as **releases**. A release is an immutable full snapshot,
named by its UTC build date (`2026-10-08`; a second release that day is
`2026-10-08.2`). `latest.json` names the newest release and `releases.json`
lists them all. To cite the data, give the dataset and the release id.

```text
https://cmgd-public.cancerdatasci.org/
  index.json                          every dataset and its latest release
  <dataset>/
    latest.json                       {"release": "2026-10-08", ...}
    releases.json                     every release, oldest first
    <release>/
      manifest.json                   tables, row counts, schema digests, licence
      catalog.ducklake                read-only DuckLake catalog over the tables below
      README.md                       table and column documentation
      tables/<table>/schema.json      columns, types, units, descriptions
      tables/<table>/files.json       data files with size, sha256, row counts
      tables/<table>/data/part-00000.parquet
      studies/index.json              per-study downloads: studies, sample counts, files
      studies/<study>/metaphlan_species.tsv.gz, metaphlan.parquet, bracken.parquet,
                      resistome.parquet, pathways.parquet (HUMAnN datasets), qc.tsv
      genefamilies/index.json         HUMAnN datasets only: studies, each with its file below
      genefamilies/<study>.json       the study's per-sample gene-family files
      genefamilies/index.tsv          every gene-family file in one table
```

Nothing on the site can be listed (it is an R2 bucket served over HTTPS), so the
JSON indexes above are how you find files ([Index files](#index-files)).

## Find the datasets and the current release

`index.json` at the site root lists every dataset with its newest release
(`latest_release`) and the path of its `latest.json`. The examples on this page
use release `2026-10-08`. Replace it with the current release from
`latest.json`:

```bash
curl -sSf https://cmgd-public.cancerdatasci.org/index.json
curl -sSf https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/latest.json
```

## SQL with DuckDB

You need [DuckDB](https://duckdb.org) 1.5.2 or newer (CLI, Python, R, …).
Attaching the release's catalog is instant: DuckDB fetches only the parts of the
Parquet files a query touches.

```sql
INSTALL ducklake; LOAD ducklake;
INSTALL httpfs; LOAD httpfs;
ATTACH 'https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/2026-10-08/catalog.ducklake' AS cmgd (
  TYPE DUCKLAKE,
  DATA_PATH 'https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/2026-10-08/',
  OVERRIDE_DATA_PATH,
  READ_ONLY);
SELECT table_name FROM duckdb_tables() WHERE database_name = 'cmgd' ORDER BY table_name;
```

`DATA_PATH` must be the release's own URL (with `OVERRIDE_DATA_PATH`): the
catalog stores file paths relative to it.

Studies, sample counts and mean read depth (`qc_metrics` has one row per sample):

```sql
SELECT study_name, count(*) AS n_samples, avg(reads_decontaminated) AS mean_reads
FROM cmgd.qc_metrics
GROUP BY study_name
ORDER BY n_samples DESC;
```

Find a sample by run accession:

```sql
SELECT sample_key, study_name, run_ids
FROM cmgd.qc_metrics
WHERE run_ids LIKE '%ERR866581%';
```

Mean species abundance in one study. Use the main MetaPhlAn pass
(`humann_bundle IS NULL`) on all reads (`data_type = 'full_data'`). A species
missing from a sample has no row, so divide by the study's sample count rather
than using `avg`:

```sql
SELECT clade_name,
       sum(relative_abundance) / (SELECT count(*) FROM cmgd.qc_metrics
                                  WHERE study_name = 'ZellerG_2014') AS mean_percent
FROM cmgd.taxonomic_profile_metaphlan
WHERE study_name = 'ZellerG_2014' AND rank = 'species'
  AND data_type = 'full_data' AND humann_bundle IS NULL
GROUP BY clade_name
ORDER BY mean_percent DESC
LIMIT 10;
```

Without the catalog, read one table's Parquet file directly. `files.json` lists
every file of a table; today there is one per table.

```sql
SELECT count(*) AS n_samples
FROM read_parquet('https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/2026-10-08/tables/qc_metrics/data/part-00000.parquet');
```

## Python

Look up the current release, attach it, and query. DuckDB is the only
dependency: it also fetches the JSON indexes and files. (The site's firewall
rejects Python's default `urllib` user agent; `requests`, `httpx` and DuckDB are
fine.) `.fetchall()` returns tuples; `.df()` (pandas) and `.pl()` (polars) work
too if you have them installed.

```python
import json

import duckdb

BASE = "https://cmgd-public.cancerdatasci.org"
DATASET = "cmgd_nextflow-2.2.1"

con = duckdb.connect()
con.execute("INSTALL ducklake; LOAD ducklake; INSTALL httpfs; LOAD httpfs;")


def fetch_json(url):
    return json.loads(con.execute("SELECT content FROM read_text(?)", [url]).fetchone()[0])


release = fetch_json(f"{BASE}/{DATASET}/latest.json")["release"]
url = f"{BASE}/{DATASET}/{release}"
con.execute(f"ATTACH '{url}/catalog.ducklake' AS cmgd "
            f"(TYPE DUCKLAKE, DATA_PATH '{url}/', OVERRIDE_DATA_PATH, READ_ONLY)")
rows = con.execute(
    "SELECT sample_key, clade_name, relative_abundance "
    "FROM cmgd.taxonomic_profile_metaphlan "
    "WHERE study_name = ? AND rank = 'species' AND data_type = 'full_data' "
    "AND humann_bundle IS NULL",
    ["ZellerG_2014"],
).fetchall()
print(len(rows), "species rows")
```

## R: a TreeSummarizedExperiment

Needs the CRAN packages `DBI`, `duckdb`, `Matrix` and `jsonlite`, and
Bioconductor's `TreeSummarizedExperiment`. This builds a sparse species ×
samples matrix of relative abundance (percent) for one study, with the study's
QC table as `colData`:

```r
library(DBI)
library(duckdb)
library(Matrix)
library(TreeSummarizedExperiment)

base <- "https://cmgd-public.cancerdatasci.org"
dataset <- "cmgd_nextflow-2.2.1"
release <- jsonlite::fromJSON(paste0(base, "/", dataset, "/latest.json"))$release
url <- paste0(base, "/", dataset, "/", release)

con <- dbConnect(duckdb())
for (sql in c("INSTALL ducklake", "LOAD ducklake", "INSTALL httpfs", "LOAD httpfs")) {
  dbExecute(con, sql)
}
dbExecute(con, sprintf(
  "ATTACH '%s/catalog.ducklake' AS cmgd (TYPE DUCKLAKE, DATA_PATH '%s/', OVERRIDE_DATA_PATH, READ_ONLY)",
  url, url))

study <- "ZellerG_2014"
long <- dbGetQuery(con, "
  SELECT sample_key, clade_name, relative_abundance
  FROM cmgd.taxonomic_profile_metaphlan
  WHERE study_name = ? AND rank = 'species' AND data_type = 'full_data'
    AND humann_bundle IS NULL", params = list(study))
qc <- dbGetQuery(con, "SELECT * FROM cmgd.qc_metrics WHERE study_name = ? ORDER BY sample_key",
                 params = list(study))
dbDisconnect(con, shutdown = TRUE)

species <- sort(unique(long$clade_name))
abundance <- sparseMatrix(
  i = match(long$clade_name, species),
  j = match(long$sample_key, qc$sample_key),
  x = long$relative_abundance,
  dims = c(length(species), nrow(qc)),
  dimnames = list(species, qc$sample_key))
tse <- TreeSummarizedExperiment(
  assays = list(relative_abundance = abundance),
  colData = S4Vectors::DataFrame(qc, row.names = qc$sample_key))
print(tse)
```

## Per-study downloads

Each release has ready-made files per study. `studies/index.json` lists every
study with its sample count and, per file, its `path` (relative to the release
URL), `size` and `sha256`; `file_descriptions` says what each file holds.

- `metaphlan_species.tsv.gz`: species × samples. One row per MetaPhlAn species
  clade (`clade_name`), one column per `sample_key`, relative abundance in
  percent from the main MetaPhlAn pass on all reads (`full_data`). `0` means not
  detected. Every sample in `qc.tsv` has a column: a sample with no
  species-level rows is kept as an all-zero column, not dropped.
- `metaphlan.parquet`, `bracken.parquet`, `resistome.parquet`,
  `pathways.parquet` (HUMAnN datasets): the study's rows of the release tables,
  in long form.
- `qc.tsv`: one row per sample with read counts and `run_ids`.

```bash
REL=https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/2026-10-08
curl -sSf "$REL/studies/index.json" -o studies-index.json
curl -sSfO "$REL/studies/ZellerG_2014/metaphlan_species.tsv.gz"
curl -sSfO "$REL/studies/ZellerG_2014/qc.tsv"
```

In R, read the downloaded matrix with
`read.delim("metaphlan_species.tsv.gz", row.names = 1, check.names = FALSE)`.
In Python:

```python
studies = fetch_json(f"{url}/studies/index.json")["studies"]
zeller = next(s for s in studies if s["study_name"] == "ZellerG_2014")
print(zeller["n_samples"], "samples:", [f["name"] for f in zeller["files"]])
species = con.read_csv(f"{url}/studies/ZellerG_2014/metaphlan_species.tsv.gz", sep="\t")
print(species.shape)
```

## HUMAnN gene families

Gene families are too large for the tables (10^5 to 10^6 rows per sample). Each
sample's HUMAnN table is a separate download. In the HUMAnN datasets,
`genefamilies/index.json` lists the studies (`study_name`, `n_samples`,
`n_files`) with the `path`, `size` and `sha256` of each study's own index,
`genefamilies/<study>.json`. That file lists the study's files: `study_name`,
`sample_key`, `readset_id`, `humann_bundle`, `branch`, `key` (the object key in
the `cmgd-raw` bucket), `url`, `size`, `sha256` (of the gzipped file, computed
when it was ingested) and `rows` (gene-family rows in the file). `index.tsv`
holds every file of the release in one table with the same columns. Values are
HUMAnN's unnormalized output in the bundle's units.

The download base is `https://cmgd-raw.cancerdatasci.org` (public read, no
listing). `url` is the base joined with `key`; a release built without the base
configured carries `url: null` and only `key`.

```sql
SELECT study_name, sample_key, size, rows, url
FROM read_csv('https://cmgd-public.cancerdatasci.org/cmgd_humann3.9-2.3.0/2026-10-08/genefamilies/index.tsv', delim = '\t')
ORDER BY size DESC
LIMIT 5;
```

```python
import hashlib

GF = f"{BASE}/cmgd_humann3.9-2.3.0"
gf_release = fetch_json(f"{GF}/latest.json")["release"]
gf_studies = fetch_json(f"{GF}/{gf_release}/genefamilies/index.json")["studies"]
artacho = next(s for s in gf_studies if s["study_name"] == "ArtachoA_2021")
mine = fetch_json(f"{GF}/{gf_release}/{artacho['path']}")["files"]
print(len(mine), "gene-family files")
for f in mine[:2]:
    if f["url"]:  # null if the release was built without the cmgd-raw base
        data = con.execute("SELECT content FROM read_blob(?)", [f["url"]]).fetchone()[0]
        assert len(data) == f["size"] and hashlib.sha256(data).hexdigest() == f["sha256"]
        with open(f"{f['sample_key']}_genefamilies.tsv.gz", "wb") as out:
            out.write(data)
```

## Index files

The JSON indexes this site adds to cdsci-lake's release files follow the **cmgd
index spec**, versioned by the `spec_version` each one carries (`"1.0"` today).
A reader should check the major version: a change that breaks readers bumps it.
This is separate from the `spec_version` of cdsci-lake's own files
(`manifest.json`, `files.json`, `releases.json`, `latest.json`: spec 2.0).

| file | holds |
|---|---|
| `index.json` (site root) | `datasets`: `id`, `workflow_id`, `version`, `latest_release`, `latest` (path of `latest.json`), `updated_at`; plus `updated_at` |
| `studies/index.json` | `dataset`, `file_descriptions`, `studies` (`study_name`, `n_samples`, `files`: `name`, `path`, `size`, `sha256`), `artifacts` |
| `genefamilies/index.json` | `dataset`, `raw_base_url`, `description`, `studies` (`study_name`, `n_samples`, `n_files`, `path`, `size`, `sha256`), `artifacts` |
| `genefamilies/<study>.json` | `dataset`, `study_name`, `raw_base_url`, `files` (see above) |

Conventions, shared with cdsci-lake's `files.json`: sizes are `size` (bytes),
checksums `sha256` (hex). `path` values in a release are relative to the release
URL; in the root index, relative to the site root. `url` values are absolute.

`artifacts` lists the release's other index files with `size` and `sha256`:
`studies/index.json` lists `genefamilies/index.json`, which lists
`genefamilies/index.tsv`. They are there because a release's `manifest.json`
cannot list files outside its tables yet (cdsci-lake#134); `studies/index.json`
itself and the root `index.json` have no checksum anywhere.

`studies/index.json` is one file for all studies (about 1 kB per study). The
gene-family index is split per study because it has one entry per sample.

## The tables

Every row carries the identity columns: **`sample_key`** (the id the sample was
published under: a readset id `RS.…` for 2.3.0 datasets, the md5 `sample_id`
for 2.2.1), `sample_id`, `readset_id`, **`study_name`**, `run_ids`
(`SRR…;SRR…`), `workflow_id` and `version`. Fact tables also carry
**`data_type`**: `full_data` (all reads) or `rarefied_data` (a 1M-read
subsample). Each release's `README.md` and `tables/<table>/schema.json` document
every column.

| table | one row per | main columns |
|---|---|---|
| `taxonomic_profile_metaphlan` | sample × `data_type` × MetaPhlAn pass × clade | `clade_name` (as reported), `rank`, `ncbi_taxid`, `sgb_id`, `relative_abundance` (percent), `coverage`, `estimated_reads`, `metaphlan_profile`, `humann_bundle` |
| `taxonomic_profile_bracken` | sample × `data_type` × taxon | `clade_name`, `rank`, `ncbi_taxid`, `fraction_total_reads` (0–1), `estimated_reads` |
| `resistome` | sample × `data_type` × CARD template | `gene`, `template_coverage`, `template_identity`, `depth`, `score` |
| `marker_abundance` | sample × `data_type` × marker | `marker_name`, `value` |
| `marker_presence` | sample × `data_type` × present marker | `marker_name` |
| `qc_metrics` | sample | `reads_raw`, `reads_decontaminated`, `bases_raw`, `bases_decontaminated`, surviving fractions, `metaphlan_index`/`metaphlan_profile`, `humann_bundle`, `pipeline_version`, `git_commit` |
| `humann_pathabundance` | sample × pathway × stratum (HUMAnN datasets) | `pathway`, `stratum` (NULL = community total), `abundance`, `humann_bundle` |
| `humann_pathcoverage` | sample × pathway × stratum (HUMAnN 3.9) | `pathway`, `stratum`, `coverage` |

Things to know before you compute:

1. **Abundances are not comparable across methods.** MetaPhlAn
   `relative_abundance` is a percent; Bracken `fraction_total_reads` is a 0–1
   read fraction. Both are stored in their native units.
2. **Pick one `data_type`.** Don't mix `full_data` and `rarefied_data` in an
   aggregate. Most analyses want `full_data`.
3. **Pick one MetaPhlAn pass.** In HUMAnN datasets, the bundle's own MetaPhlAn
   pass (`humann_bundle` set, `full_data` only) sits next to the main pass
   (`humann_bundle IS NULL`).
4. **HUMAnN units depend on the bundle** (3.9: RPK-based; 4.0.0a1: CPM).

## Licence

The data is released under CC0-1.0 (recorded in every release's
`manifest.json` and table schemas).

## Getting help

Design: [ADR-0011](adr/0011-results-storage-and-publication.md). A missing file or
a broken snippet: open an issue in `seandavi/nextflow_telemetry`.
