# 0011. Results storage and publication

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** Sean Davis

## Context

The pipeline writes per-sample outputs to `cmgd-raw`
([0008](0008-object-storage-on-r2.md)), one prefix per registration
([0010](0010-registrations-are-bundles.md)). `nf-etl` parses them into tables.
Researchers need those tables over the internet without accounts, from SQL,
Python and R, plus per-study files they can just download.

Forces:

- **Scale.** The target is 200–400k samples. Taxonomic profiles are about 4k
  rows per sample, markers about 30k. HUMAnN gene families are 10^5–10^6 rows
  per sample, more than everything else combined
  ([research note](../research/results-storage-and-access.md)).
- **R2 public access is GET on known keys.** No anonymous LIST and no anonymous
  S3 API, so a reader must find every file from an index.
- **One lake.** cdsci-lake already runs a shared DuckLake (`lake.<schema>.*`)
  with a producer contract (its ADR-0011) and a release builder,
  `publish_release`: immutable full-snapshot releases with a frozen read-only
  DuckLake catalog, manifests, checksums, `releases.json` and `latest.json`
  (its ADR-0025).
- **Bioconductor users** expect a `TreeSummarizedExperiment` per study, as
  curatedMetagenomicData ships today.

## Decision

1. **Everything except HUMAnN gene families goes into the shared DuckLake** as
   `lake.cmgd.*` through `cdsci.lake`: MetaPhlAn (the main pass and each
   HUMAnN bundle's own pass), Bracken, resistome, QC, markers and HUMAnN
   pathways. Tables are sorted by (study, sample, feature). (#234)
2. **Public releases are Parquet on `cmgd-public`**, served at
   `https://cmgd-public.cancerdatasci.org`. `nf-etl publish` builds one dataset
   per registration, named `<workflow_id>-<version>`. Each release is built by
   `publish_release` from one lake snapshot, into a local store on onclappc02.
   A separate `nf-etl publish --sync` step uploads it. Release ids are UTC build
   dates. A root `index.json` lists every dataset and its latest release.
3. **Each release carries per-study artifacts** under `studies/`: a wide
   species matrix (`metaphlan_species.tsv.gz`), long Parquet per profile and
   `qc.tsv`, listed with size and sha256 in `studies/index.json`. (#229)
4. **Gene families are per-sample downloads**: the files the pipeline already
   wrote to `cmgd-raw`, listed per release and per study in
   `genefamilies/<study>.json` (key, URL, size, sha256, rows; sha256 computed at
   ingest, which reads every file anyway), with `genefamilies/index.json` over
   the studies and `index.tsv` over all files. They are not in the lake.
   Download URLs use `https://cmgd-raw.cancerdatasci.org`.
6. **cmgd's own JSON indexes** (root `index.json`, `studies/index.json`,
   `genefamilies/*.json`) carry `spec_version` (the cmgd index spec, 1.0,
   documented in `docs/data-access.md`) and use cdsci-lake's `size`/`sha256`
   naming.
5. **Recorded future option for gene families:** TileDB-SOMA hosted on AWS Open
   Data, if the sponsorship enquiry succeeds. The measured volumes from #234
   feed that enquiry. S3 there supports LIST and anonymous reads, which removes
   TileDB's blocker on R2.

## Alternatives considered

- **A dedicated cmgd DuckLake, frozen by rewriting `data_path`** (`nf-etl
  freeze`, `publish-and-catalog-design.md`). Rejected: a second lake to run,
  and the frozen catalog pointed at the working lake's files, so compacting the
  lake could break a published release. `publish_release` copies the data into
  each release, and the release can be checked against its own manifest.
- **Gene families in the lake.** Rejected for now: at 10^10–10^11 rows they
  would be most of the lake's storage and maintenance for a table that few
  people query across samples.
- **Zarr dense matrices** (AnnData-zarr per release). Rejected as the system of
  record: the profiles are 0.5–3 % dense, Zarr has no native sparse arrays, and
  R cannot write sharded Zarr or write to S3. It stays possible as a derived
  export if Python users ask.
- **TileDB / TileDB-SOMA on R2.** Rejected: opening an array LISTs its
  fragments, and R2 public buckets allow no anonymous LIST or S3 API. It would
  need published read keys or a proxy. Also the R package is not on CRAN or
  Bioconductor.
- **One dataset for all registrations.** Rejected: registrations differ in
  tables and units (HUMAnN 3.9 vs 4), and are released on their own schedules.

## Consequences

- Readers attach a frozen catalog over HTTPS with DuckDB, or read Parquet and
  TSV directly. `docs/data-access.md` documents this, and its snippets are
  tested against a local release; `scripts/smoke_public_data.py` runs them
  against the live site.
- Every release is a full copy of its registration's rows. Storage grows with
  the number of releases kept; prune with the dataset contract's `keep_last`
  when it matters.
- `publish_release` writes one Parquet file per table. At 200–400k samples the
  biggest tables become single multi-GB files. Partitioned or multi-file
  releases are a cdsci-lake change.
- `publish_release` has no hook for extra artifacts, so `studies/` and
  `genefamilies/` are written next to the release, not listed in its manifest.
  Their index files carry sizes and checksums, and an `artifacts` list chains
  the other index files to `studies/index.json`, which itself has no checksum
  until cdsci-lake#134.
- The root `index.json` lists every dataset built in the local store; `sync`
  uploads it last, so sync every dataset you build.
- Each release rebuilds every study's files. Rebuilding only changed studies
  waits until that is too slow.
- The public licence is CC0-1.0, set in one place (`etl/publish.py`).
- `nf-etl freeze` is removed.

## References

- Issues #229, #230, #234; monode#52 (`cmgd-public` bucket and domain).
- cdsci-lake ADR-0011 (producer contract), ADR-0025 (versioned datasets),
  `docs/design/scientific-publication-platform.md` §3.3, §4, §5.
- [`docs/research/results-storage-and-access.md`](../research/results-storage-and-access.md).
