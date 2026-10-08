# Research: storing and serving cMD pipeline results (Parquet/DuckLake vs Zarr v3 vs TileDB/SOMA vs others)

*Researched 2026-10-07. Inputs: `docs/output-catalog-etl-design.md`, `docs/output-catalog-etl-plan.md`, `docs/publish-and-catalog-design.md`, `docs/data-access.md`, `docs/storage-layout.md`, issues #57 and #152 (fetched from GitHub), and the curatedMetagenomicsNextflow README. `lake/README.md` and `lake/load_metaphlan.sql` are not on main, so I didn't review them. I couldn't run `gh`. I fetched the issues over the web, and the bodies of #153–#158 are short phase stubs that repeat the plan doc.*

## Summary

Keep tidy Parquet in a dedicated DuckLake as the system of record and the main way to query the data. The case is stronger than when the design was written: DuckLake v1.0 shipped on 2026-04-13 with a stable spec, sorted tables, and an official guide for a public read-only lake on R2 over HTTPS. A long table sorted by `sample_id` already behaves like an on-disk CSR matrix, so the sample-subset path does not need a matrix format. TileDB/TileDB-SOMA doesn't fit, mainly because opening an array means LISTing a fragments prefix, which R2 public buckets don't allow. Zarr v3 / AnnData-zarr works as a **derived per-release export** for Python users. It doesn't work as the system of record: appending to sparse data is awkward and R write support is limited. The decision-relevant gap is feature-wise slicing across all samples, and for HUMAnN, the sheer size. Handle those with a feature-sorted copy or per-release matrix exports, and test them in one spike (end of this report).

## 1. Data shapes and sizes

Row counts per sample come from the 12-sample measurement in `output-catalog-etl-design.md` (both branches combined). Bytes assume roughly 4–8 B/row for ZSTD Parquet sorted by sample, with dictionary or integer feature keys. **That byte rate is my inference, not a measurement**, and the spike should measure it.

| Output | Natural shape | Rows/sample | @35k samples | @727k samples |
|---|---|---|---|---|
| `manifest.json` → `qc_metrics` (read accounting, provenance, db versions) | 1 row/sample dimension | 1 | 35k rows, <10 MB | 727k rows, ~100 MB |
| MetaPhlAn rel. abundance (+GTDB derivable by join) + Bracken species/genus | long table, i.e. a sparse sample × clade matrix (~tens of k clades, ~0.5–3% dense) | ~4k | ~140M rows, ~0.5–1 GB | ~2.9B rows, ~12–25 GB |
| MetaPhlAn `marker_abundance` | sparse sample × marker (millions of markers) | ~16.5k | ~580M rows, ~2–5 GB | ~12B rows, ~50–100 GB |
| `marker_presence` | membership set (value column always `true`) | ~15k | ~540M rows, ~1.5–4 GB | ~11B rows, ~45–90 GB |
| `metaphlan_unknown_list` / `viruses_list` | small long tables | tens–hundreds | small | small |
| Resistome (KMA/CARD `card_kma.res`) | long table, ~5k CARD templates | ~140 | ~5M rows | ~100M rows |
| Kraken2 report, FastQC, KneadData logs, KMA aln/fsa/frag, StrainPhlAn `.json`/`.pkl` | per-sample blobs | — | leave in `cmgd-raw`, index by URL | same |
| HUMAnN gene families (2.3.0; full branch only) | extremely sparse sample × UniRef90 (~millions); stratified rows multiply it | **unmeasured**, plausibly 10⁵–10⁶ | plausibly 10¹⁰ rows (tens–hundreds of GB) | plausibly 10¹¹ rows (TB scale) |
| HUMAnN pathways (abundance/coverage) | sparse sample × ~600 pathways, stratified | ~10³–10⁴ | modest | ~10¹⁰ rows |

Takeaways:
- Every profile is sparse. The useful matrix form is CSR (by sample) or CSC (by feature), never dense.
- Markers are about 89% of rows today. Once HUMAnN gene families ship, they will be larger than everything else combined. They set the capacity limits, not the taxonomic profiles.

## 2. Options compared

| Criterion | **Parquet + DuckLake (incumbent)** | **Zarr v3 (AnnData-zarr layout)** | **TileDB / TileDB-SOMA** |
|---|---|---|---|
| Daily batch append | Native. Each 500-sample batch is an INSERT and a new snapshot. `merge_adjacent_files` compacts without expiring snapshots; inlining handles tiny writes. | Poor for sparse data. CSR is three 1-D arrays (`indptr/indices/data`), so appending means resizing them and rewriting the last chunk/shard. New features change `var`. In practice you rebuild each release. | Native. Each write is a timestamped fragment. SOMA supports appending obs and var. Fragment consolidation is required to keep opens fast. |
| Sparse-matrix fit | Long COO table. Sorted by sample, it effectively *is* CSR. Feature-wise slices need a second sort order or a full scan of the key column. | Sparse arrays have no native Zarr representation. AnnData defines CSR/CSC on top of 1-D arrays. You pick one orientation per copy. | Best: true 2-D sparse arrays with tiling on both dimensions, so sample and feature slices are both efficient. |
| Serverless HTTPS reads from R2 | Yes, officially supported: "Public DuckLake on Object Storage" names Cloudflare R2. Only known URLs are needed because the catalog lists every file. | Partly. Arrays at known keys can be read over HTTP. Discovering a hierarchy needs LIST or consolidated metadata, and consolidated metadata is "not part of the Zarr format 3 specification". | **No.** The engine LISTs the `__fragments` prefix when opening an array, and R2 public buckets allow no anonymous LIST or S3 API. You'd have to publish read keys or proxy S3 through a Worker. |
| R / Bioconductor | `duckdb` (CRAN) loads the ducklake/httpfs extensions. Converting long → `Matrix::sparseMatrix` → TSE is a few lines. cMD already depends on dplyr/tidyr. | `Rarr` (Bioc) reads v2/v3 and reads sharded arrays, but **can't write sharded arrays** and **can't write to S3**. Remote reads go through paws/S3 calls. `anndataR` (Bioc 3.23) handles zarr and converts to SCE. | `tiledbsoma` R isn't on CRAN or Bioconductor (README: "From R-universe (recommended)"); a source build needs C++20 and has a vendored fmt/spdlog conflict. It does provide `write_soma.SummarizedExperiment`. |
| Python | duckdb, plus Arrow/Polars on raw Parquet. Spark, DataFusion, Trino, and pandas DuckLake clients also exist. | Strongest: zarr-python 3, anndata, scanpy. | Strong: tiledbsoma on PyPI; CELLxGENE Census is the reference deployment. |
| Versioning / snapshots | Catalog snapshots and time travel. The frozen DuckDB-file catalog gives citable releases. `version` is a column. | None built in. Release = an immutable prefix per version. | Timestamp time travel over fragments. Vacuuming after consolidation removes history. |
| Compaction | `merge_adjacent_files`, `rewrite_data_files`, sorted-on-compaction (v1.0). | Not needed: written whole each release. | Fragment and fragment-metadata consolidation plus vacuum, run on a schedule. |
| Licence / vendor risk | MIT. DuckDB Foundation spec. Already used in the cdsci lake. | Open spec. Many implementations (zarr-python, zarrs, Rarr). Low risk. | Core and SOMA are MIT, but TileDB Inc.'s product is now Carrara (Oct 2025), a commercial "omnimodal data platform". The SOMA spec is CZI-governed. Medium risk: R build friction and funding direction. |
| Ops burden (1 maintainer) | Low. Postgres catalog plus a cron CLI, both already planned. | Low if it's a release-time batch export. | Medium–high. Consolidation jobs, a native R build, and R2 access workarounds. |

Other options considered:
- **Lance:** no first-party R client (only a community `lancedb` R wrapper). It's built for vector and random-access AI workloads. Rejected.
- **HDF5 / HDF5Array:** a single file is fine for per-study downloads, but reads over HTTPS from R2 need ROS3 or range-GET support that's uneven across clients. Not better than Parquet here.
- **ExperimentHub objects** (what cMD ships today): still a valid way to *distribute* per-release, per-study TSE `.rds` files, generated from the lake.

## 3. Findings

1. **Claim:** DuckLake v1.0 is production-ready with a backward-compatibility guarantee. It ships in DuckDB v1.5.2 (2026-04-13) and adds sorted tables, bucket partitioning, and inlining on by default (threshold 10 rows). **Sources:** [DuckLake v1.0](https://ducklake.select/2026/04/13/ducklake-10/). **Support:** direct. **Confidence:** high. *Implication (my inference):* issue #152 used DuckDB 1.4.4, so pin ≥1.5.2. Also replace the freeze's hand-written `UPDATE ducklake_metadata SET value=…` data_path rewrite with the documented public-lake pattern, or at least retest it against v1.0.
2. **Claim:** A read-only DuckLake can be served from R2 over public HTTPS with no authentication, using a DuckDB-file catalog whose data path is the `https://` URL. **Sources:** [Public DuckLake on Object Storage](https://ducklake.select/docs/stable/duckdb/guides/public_ducklake_on_object_storage.html). **Support:** direct. **Confidence:** high.
3. **Claim:** R2 public buckets can't list contents. **Sources:** [R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/) ("public buckets do not let you list the bucket contents"); repo `storage-layout.md`. **Support:** direct. **Confidence:** high. Anonymous S3-API access being unavailable comes from the repo docs and project memory. The Cloudflare page I fetched doesn't say it explicitly.
4. **Claim:** TileDB lists the `__fragments` prefix and reads every fragment's metadata to open an array, and consolidation is the mitigation. **Sources:** [TileDB forum](https://forum.tiledb.com/t/what-could-affect-array-opening-time/594), [TileDB consolidation docs](https://documentation.cloud.tiledb.com/academy/structure/arrays/foundation/key-concepts/compute/consolidation/). **Support:** direct. **Confidence:** medium-high. That this makes TileDB unusable on R2 public HTTPS is my inference, and I haven't tested it.
5. **Claim:** The `tiledbsoma` R package is distributed via R-universe or conda rather than CRAN or Bioconductor. **Sources:** [TileDB-SOMA R README](https://github.com/single-cell-data/TileDB-SOMA/blob/main/apis/r/README.md); the CRAN page returns 404. **Support:** direct. **Confidence:** high.
6. **Claim:** TileDB Inc.'s commercial focus is the Carrara platform. **Sources:** [TileDB Carrara announcement, 2025-10-07](https://www.tiledb.com/blog/introducing-carrara). **Support:** direct for the product, my interpretation for the risk. **Confidence:** medium.
7. **Claim:** Rarr reads Zarr v2/v3 and sharded arrays but can't write sharded arrays or to S3. Its remote access uses paws S3 calls. **Sources:** [Rarr features](https://huber-group-embl.github.io/Rarr/articles/features.html), [Rarr remote vignette](https://huber-group-embl.github.io/Rarr/articles/S3.html). **Support:** direct. **Confidence:** high. I haven't verified whether paws anonymous GETs work against an R2 custom domain.
8. **Claim:** Zarr has no native sparse arrays. AnnData encodes CSR/CSC as `indptr/indices/data` 1-D arrays. Consolidated metadata isn't in the Zarr v3 spec. **Sources:** [AnnData on-disk format](https://anndata.scverse.org/en/stable/fileformat-prose.html), [xarray Zarr tutorial (zarr-python warning)](https://tutorial.xarray.dev/intermediate/intro-to-zarr.html), [zarr-specs #371](https://github.com/zarr-developers/zarr-specs/issues/371). **Support:** direct. **Confidence:** high.
9. **Claim:** `anndataR` is in Bioconductor 3.23 (v1.2.2). It reads and writes h5ad and zarr and converts to SingleCellExperiment. **Sources:** [anndataR](https://bioconductor.org/packages/release/bioc/html/anndataR.html). **Support:** direct. **Confidence:** high.
10. **Claim:** cMD on Bioconductor (3.20.0, BioC 3.23) returns TreeSummarizedExperiment objects through ExperimentHub/AnnotationHub and already imports dplyr/tidyr/purrr. **Sources:** [curatedMetagenomicData](https://bioconductor.org/packages/release/data/experiment/html/curatedMetagenomicData.html). **Support:** direct. **Confidence:** high. *My inference:* a DuckDB-backed tidy query → `sparseMatrix` → TSE fits the package's current code paths, and swapping ExperimentHub for `duckdb` is a small import change.

## 4. Recommendation (hybrid)

**System of record: tidy Parquet in the dedicated cmgd DuckLake.** Keep the schema from `output-catalog-etl-design.md`, with these changes:
- **Integer feature keys:** `taxon_key`, `marker_key`, and later `uniref_key`, joined to dimension tables. They shrink the files and make matrix exports trivial.
- **Sort by `(sample_id, feature_key)`** with `ALTER TABLE … SET SORTED BY`. This makes study/sample subsets range reads.
- **Partition only by `version / data_type`**, as already planned.
- **Keep markers and HUMAnN gene families in separate tables.** Consider publishing them only as the per-release exports below, not in the low-latency public catalog.

**Derived per-release exports in `cmgd-public`, generated by `nf-etl freeze`:**
1. **Feature-sorted copies** of the big fact tables, sorted by `(feature_key, sample_id)` and registered as a second table, e.g. `taxonomic_profile_metaphlan_by_feature`. Users can then pull "taxon X across all samples" over HTTPS without scanning everything. Build these per release, not per batch.
2. **Optional AnnData-zarr per release** (one per profile type, CSR, sharded, written with zarr-python) for Python and scverse users. Only build it if someone asks; the Parquet is already readable from Python.
3. **R/Bioconductor:** a thin `cMD` accessor (in the package or a companion package): DuckDB `ATTACH` to the frozen catalog → filter by study/sample metadata → `Matrix::sparseMatrix(i, j, x)` → `TreeSummarizedExperiment` with `rowTree` built from the `taxon` dimension. ExperimentHub can keep serving per-study TSE `.rds` snapshots built from the same release, for offline use.

**ETL flow:**
1. Telemetry watermark: completed jobs not yet in `etl_ingested`.
2. HEAD `MARK_COMPLETE` on R2 `cmgd-raw/cmgd_nextflow/<ver>/<sample>/`.
3. GET known paths from the `OutputSpec`.
4. Parse into tidy rows with feature keys resolved against the version-scoped dimensions. Unknown features get appended to the dimension tables.
5. One DuckLake transaction per 500-sample batch, then write the `etl_ingested` row.
6. Nightly `merge_adjacent_files`.
7. For each release (pipeline version or monthly): rewrite into a frozen catalog with an HTTPS data path, build the feature-sorted tables and optional zarr/TSE exports, and publish an immutable `releases/<date>/` prefix.
8. Never expire snapshots that a published release references.

**Rejected:**
- **TileDB-SOMA:** it can't do anonymous LIST on R2, R distribution is awkward, it needs a consolidation job, and the vendor is moving toward a commercial platform. Revisit only if feature-wise random access over HTTPS fails the spike *and* R2 stops being the host.
- **Zarr as the system of record:** no clean incremental sparse append, and the R write path is incomplete.

## 5. First prototype (one spike)

**Spike: "metaphlan + marker_abundance at cMD scale, served from R2 over HTTPS."** Build the two tables from the ~591 available 2.2.1 samples. Replicate them with synthetic `sample_id`s up to 35k to test scale. Store them with integer keys and sample sort. Also build a feature-sorted copy and a CSR AnnData-zarr of the metaphlan species matrix. Publish as a frozen public DuckLake on R2.

Acceptance criteria, measured from a clean laptop with no keys, using DuckDB ≥1.5.2 and R:
- **(a) Size:** record bytes/row per table. Extrapolate to 727k and to HUMAnN, and replace the estimates in §1.
- **(b) Study subset:** one study (~200 samples) of species abundance → TSE in R in under 10 s, with under 50 MB transferred (log with `httpfs` stats).
- **(c) Feature slice:** one species across all 35k samples in under 15 s using the feature-sorted table. Compare against the sample-sorted table and the zarr CSC/CSR read.
- **(d) Batch append:** a 500-sample batch plus `merge_adjacent_files` in under 5 min. Published releases still read correctly afterwards.
- **(e) Reproducibility:** re-attaching an older release's catalog returns identical results.

If (c) fails for markers or gene families, scope a per-release CSC zarr export for those tables only.

## Contradictions

- `storage-layout.md` says "one shared `cdsci-lake`", while `publish-and-catalog-design.md` and the ETL docs say "dedicated cmgd DuckLake". The newer docs win. The project memory `project-cmgd-independent-ducklake` agrees with the dedicated lake.
- Issue #57's body says "no transform" and "YAML specs"; the newer docs supersede that (thin parsers, Python `OutputSpec`).
- `publish-and-catalog-design.md` still describes one unified `taxonomic_profile` table with a `method` column, while the ETL design and `data-access.md` use separate per-method tables. Fix the publish doc.

## Missing evidence

- HUMAnN 4 gene-family rows per sample for cMD-type samples. This is the largest sizing unknown, so measure it on the first 2.3.0 samples.
- Real Parquet bytes/row, which spike (a) covers.
- Whether Rarr/paws anonymous reads and TileDB's `vfs.s3` work against an R2 public custom domain. Neither has been tested; the TileDB conclusion rests on the LIST requirement.
- Whether DuckLake v1.0 removes the need for the data_path metadata rewrite found in #152. Retest it.
- `lake/load_metaphlan.sql` and `lake/README.md` aren't on main, so I didn't review them.

## Sources

**Kept:**
- DuckLake v1.0 release (https://ducklake.select/2026/04/13/ducklake-10/): spec stability, sorted tables, inlining, clients.
- Public DuckLake on Object Storage (https://ducklake.select/docs/stable/duckdb/guides/public_ducklake_on_object_storage.html): serving from R2 over HTTPS.
- Merge adjacent files (https://ducklake.select/docs/stable/duckdb/maintenance/merge_adjacent_files.html): compaction without expiring snapshots.
- Cloudflare R2 public buckets (https://developers.cloudflare.com/r2/buckets/public-buckets/): no listing.
- TileDB forum on array open time (https://forum.tiledb.com/t/what-could-affect-array-opening-time/594) and consolidation docs (https://documentation.cloud.tiledb.com/academy/structure/arrays/foundation/key-concepts/compute/consolidation/): LIST requirement and consolidation.
- TileDB-SOMA R README (https://github.com/single-cell-data/TileDB-SOMA/blob/main/apis/r/README.md): R distribution and build.
- TileDB Carrara announcement (https://www.tiledb.com/blog/introducing-carrara): vendor direction.
- Rarr features (https://huber-group-embl.github.io/Rarr/articles/features.html) and remote vignette (https://huber-group-embl.github.io/Rarr/articles/S3.html): R Zarr capabilities.
- AnnData on-disk format (https://anndata.scverse.org/en/stable/fileformat-prose.html): how sparse data is encoded.
- zarr-specs #371 and the xarray Zarr tutorial: status of consolidated metadata in v3.
- Bioconductor pages for anndataR and curatedMetagenomicData: client maturity and the current cMD dependencies.
- Repo design docs and issues #57/#152: the incumbent design and the Phase 0 findings.

**Deprioritised:** TileDB Cloud API spec (about the hosted product, not relevant); LanceDB docs (vector-DB focus); `github-wiki-see` mirrors of the HUMAnN wiki (no per-sample row counts); PyPI tiledbsoma history (not needed).

## Next steps

1. Run the spike in §5.
2. Measure HUMAnN 2.3.0 output sizes on 10 real samples before designing the gene-family storage.
3. Retest the frozen-catalog `data_path` handling on DuckLake v1.0.
