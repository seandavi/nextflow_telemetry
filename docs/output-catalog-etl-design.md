# Output catalog ETL & taxonomy harmonization

> Status: **draft / exploration** — companion to [`publish-and-catalog-design.md`](./publish-and-catalog-design.md) and [`storage-layout.md`](./storage-layout.md). Those cover the publish-path contract, event-driven registration, buckets, and the DuckLake decision. This doc covers the layer they defer: **how a sample's published files become catalog tables**, the **analytical table schema**, and **taxonomy harmonization** across profilers. Grounded in forensics on real `cmgd_nextflow` 2.0.7 output in `gs://cmgd-data/...` (July 2026). No code committed yet — this records possibilities and details.

## Why a transform layer is required

An earlier framing had DuckLake reading pipeline outputs (TSV.gz) directly, with no transform. Forensics on real 2.0.7 output showed that's too optimistic (and `publish-and-catalog-design.md` now defers to this doc on the point). A **light transform layer is required**, for reasons that are all in the data:

- Output TSVs carry **multi-line `#` comment headers** (mpa DB version, command line, read counts) before the real table; the header row itself is often a `#` line. A raw `read_csv` doesn't get clean columns.
- **Units differ by tool.** metaphlan/gtdb relative abundance is a percent; bracken's `fraction_total_reads` is a 0–1 fraction. Same logical column, different scale.
- **Grain and datatype differ by file** (clade-level float profiles vs marker-level bool/float tables) — they can't share one table.
- **Taxon identifiers are not comparable across tools** without a crosswalk (see harmonization below).

So the catalog is fed by a small, deterministic ETL, not a zero-transform view. The transform is thin and table-driven (below), but it is not nothing.

## Ground truth: what 2.0.7 publishes

Publish path (matches the contract in the catalog doc):

```
${publish_base_dir}/${manifest.name}/${manifest.version}/${meta.sample}/[full_data|rarefied_data/]<step>/<files>
# e.g. cMDv4/cmgd_nextflow/2.0.7/02ce1bab.../full_data/metaphlan_markers/marker_rel_ab_w_read_stats.tsv.gz
```

- `meta.sample` is `md5(sorted(SRR accessions))`, **computed at insert time** and stored as the telemetry sample key — so it is both the folder name and the DB join key. No hashing or `sampleinfo.txt` parsing in the ETL.
- Two computation branches per sample: **`full_data/` and `rarefied_data/`** (rarefied = recomputed on a 1M-read subsample, seed 42).
- **`MARK_COMPLETE`** at the sample root (`<sample_id> <UTC timestamp>`) is the **authoritative completion sentinel** — present iff the full output set (manifest + all branch files) is durably written.
- **`manifest.json`** at the sample root is already emitted and is rich — it is the ETL's backbone:
  - `provenance.input_ids` (the SRRs), `pipeline_name`, `pipeline_version`, `git_commit`, `run_name`, `command_line`
  - `parameters.metaphlan_index` (the reference DB version, e.g. `mpa_vJan25_CHOCOPhlAnSGB_202503`), `skip_humann/skip_gtdb/skip_rarefied` flags
  - `read_accounting` (raw + decontaminated read/base counts, surviving fractions) — a pre-structured QC record
  - `software_versions` for every tool

Output inventory (per branch unless noted): metaphlan (`marker_rel_ab_w_read_stats`, `metaphlan_unknown_list`, `metaphlan_viruses_list`, `marker_abundance`, `marker_presence`, strainphlan `metaphlan.json`), kraken/**bracken** (`bracken.species/genus`, `kraken2.report`), **gtdb** (`gtdb_profile`), **resistome** (`rgi_bwt.*`), `fastqc`. Every step dir also dumps Nextflow control files (`.command.*`) — noise the ETL must not ingest.

File-format specifics that shaped the parsers:
| file | header | key columns | notes |
|---|---|---|---|
| `marker_rel_ab_w_read_stats.tsv.gz` | `#`-commented | clade_name, clade_taxid (NCBI taxid **lineage**), relative_abundance (%), coverage, est_reads | rows at every rank incl. `t__SGB` leaves |
| `bracken.species.txt.gz` | normal header row | name (bare binomial), taxonomy_id (NCBI), fraction_total_reads | fraction 0–1, **not** percent; far more taxa reported |
| `gtdb_profile.tsv.gz` | `#`-commented | clade_name (`;`/`d__` lineage), relative_abundance (%) | **no taxid**; GTDB renames (Firmicutes→Bacillota, etc.) |
| `marker_abundance.tsv.gz` | `#`-commented | marker_name, value (float) | marker grain, tens of thousands/sample |
| `marker_presence.tsv.gz` | `#`-commented | marker_name, value | **always present=true** — a membership list, see below |
| `manifest.json` | JSON | — | source of qc_metrics + provenance |

## The mapping mechanism: spec + parsers + engine

The part that varies per workflow version is small and declarative; the machinery (fetch, parse, attach columns, write, watermark) is identical across every file and version. Keep them apart.

- **`OutputSpec`** — a **frozen dataclass** (`path, table, parser, tags`). Data + a parser reference, not logic. The registry `SPECS[(name, version)] = [OutputSpec, ...]` is the *only* thing that changes when a version adds/renames/moves a file. Dataclass, **not pydantic**: this is developer-authored config, not untrusted input at a trust boundary, so validation buys nothing (pydantic stays reserved for request/response models per repo convention).
- **Parsers** — plain functions `bytes -> Iterable[dict]` of file-native fields. A shared `commented_tsv(raw, columns, coerce, comment, skip_header)` helper does the work; each tool's parser is a thin wrapper calling it with different columns. **Composition, not inheritance** — no `BaseParser`; the engine's uniform `out.parser(...)` call *is* the polymorphism, dispatched through a function reference. A genuinely new file shape is one new function.
- **Engine** — one generic `process(sample_ids)`, written once:
  1. `MARK_COMPLETE` existence gate (HEAD on a known path, never LIST); absent → not ready, requeue.
  2. Look up `(workflow, version)` → spec; unknown version → dead-letter, never guess.
  3. For each `OutputSpec`: fetch the file (missing → skipped branch, e.g. `skip_gtdb`, tolerated); run the parser; the engine attaches the **common columns** (`sample_id, workflow, version` + the spec's `tags`, e.g. `method`, `data_type`); route rows to `out.table`.
  4. One transaction per sample (or per batch); mark ingested in the watermark.

**Convergence with the manifest:** if the pipeline adds an `outputs: [{logical_type, path, row_count}]` array to `manifest.json`, the *path* half of each spec comes from the manifest, the spec collapses to a tiny `logical_type → (table, parser)` map, new tools are picked up with no spec edit, and row counts reconcile for free. This is the single highest-leverage pipeline change; everything works without it, so it's optional.

## Table design: split by `(grain, datatype)`, not by tool

**Rule: a table's identity is its `(grain, datatype)`, not the tool that produced it.** Same shape → one table with a discriminator column; different grain or datatype → separate table.

| table | grain | populated from | discriminators | notes |
|---|---|---|---|---|
| `taxonomic_profile_metaphlan` | clade | metaphlan | `data_type` | native percent + coverage/estimated_reads |
| `taxonomic_profile_bracken` | clade | bracken | `data_type` | native read-count fraction + estimated_reads |
| `marker_abundance` | marker | metaphlan marker_abundance | `data_type` | float value |
| `marker_presence` | marker | metaphlan marker_presence | `data_type` | **degenerate** — see below |
| `resistome` | AMR gene | rgi_bwt | `data_type` | wide, own shape |
| `qc_metrics` | sample | `manifest.read_accounting` | — | one row/sample (dimension) |

- **Separate per-method tables** (`taxonomic_profile_metaphlan`, `taxonomic_profile_bracken`), *not* one unified table. (Supersedes the earlier single-table-with-`method`-column proposal.) The deciding factor: the abundance columns carry **different value interpretations** — metaphlan `relative_abundance` is a percent, bracken `fraction_total_reads` is a read-count fraction — and mixing interpretations in one column is a footgun. Separation also lets each table keep its **native units** (no lossy %→fraction normalization) and its **method-specific columns** (metaphlan `coverage`/`estimated_reads`; bracken `estimated_reads`) without null sprawl. Cross-method comparison is a deliberate `UNION`/join, not an accidental `avg()` over mixed semantics.
- **Do NOT denormalize provenance onto every clade row.** At 270–1,200 clade rows/sample × 500k samples, repeating `accessions`/`db_version`/read counts is enormous waste. Per-sample provenance lives once in `qc_metrics` (the dimension), joined on `sample_id`.
- **`marker_presence` is degenerate as published** — every row is `present=true` (the metaphlan presence table only emits present markers). Presence is encoded by *row existence*; the boolean column carries no information. Store it as `(sample_id, marker_name, data_type)` membership and drop the boolean. (Lesson: can't pick the column — or know you don't need it — until you've seen the values.)
- **full/rarefied is a column (`data_type`), not a table.** Same grain, same datatype — just computed on all reads vs a subsample. It's a discriminator, and an excellent partition key (below).

### Scale reality (empirical, 12 samples)

| table | rows (12 samples) | extrapolated @ 500k |
|---|---|---|
| marker_abundance | 197,482 | ~8B |
| marker_presence | 184,065 | ~8B |
| taxonomic_profile_metaphlan + _bracken | 47,560 | ~2B |
| resistome | 1,643 | ~70M |
| qc_metrics | 12 | 500k |

**Markers are ~89% of all rows.** They are strain-analysis inputs, not "by-microbe" query fodder, so they belong in their own tables and probably **off the low-latency path** — a separate, coarser, or lazily-built store. This is the dominant capacity-planning fact.

## Physical layout: partitioning

Partition the fact tables by **`workflow / version / data_type`** — all low-cardinality, all commonly filtered (method is now the table, not a partition column). A handful of partitions per version. Coarse and healthy.

- **Never partition by `sample_id` or `clade_name`.** High cardinality → 500k partitions → the small-files catastrophe this whole effort exists to avoid. `sample_id` is a **sort/clustering key within** partitions, so min-max stats prune files by sample without a directory explosion.
- Order the hierarchy to match query patterns and the bucket layout; partition coarse, sort fine, **compact many files per partition** (DuckLake handles this).
- Physically this means near-method-separated files under a logical single table — the reason keeping it one table is free.

## Trigger: event-driven, never list

**Listing 500k directories is a non-starter** (GCS LIST pages ~1k objects; the deep prefix tree is millions of objects). But listing is never needed, because **telemetry already holds the keys** — `(name, version, sample_id)` reconstructs every path.

- **Primary: DB-driven pull.** Telemetry is already the completion event log (weblog → `completed` on `MARK_COMPLETE`). The ETL queries "completed since watermark, not yet ingested" (indexed) and GETs known paths. Zero new infra, reuses the captured event. A watermark / `pending_ingest` marker lives in telemetry Postgres.
- **Escape hatch: GCS object-finalize → Pub/Sub** on the `MARK_COMPLETE` write. Genuinely loose-coupled, but new infra to learn what the DB already knows. Justified only if the ETL must be owned/deployed separately, needs per-sample push latency, or full producer/consumer decoupling. **We own the emit point**, so the telemetry app can publish to Pub/Sub later without burning the bridge.
- **Durability race:** telemetry `completed` fires when pipeline *logic* finishes, possibly before `publishDir` has flushed to object storage. The `MARK_COMPLETE` *object* is the last thing written = the true "files are here" marker. Gate ingestion on a cheap existence check of that object (HEAD, not LIST); requeue with backoff if absent.
- Keep the trigger behind an `ingest(sample_id)` seam so the source is swappable. **Start as a scheduled docker job** (`nf-etl process`, cron/compose on onclappc02, restart-safe via watermark); promote to a polling daemon (mirroring the nf-client pull-mode daemon) only if latency demands it. The ETL is an **in-repo module** (`src/nextflow_telemetry/etl/`, reusing `db.py`/`config.py`), **not** an API endpoint; keep DuckDB/GCS deps in an optional dependency group so the API image stays lean.

## Taxonomy harmonization

The profilers speak different nomenclatures. There is not one crosswalk — there are two problems with different difficulty.

**1. metaphlan ↔ gtdb — same underlying calls, join on the SGB.** gtdb_profile is the metaphlan run *relabeled* to GTDB via MetaPhlAn's shipped **SGB→GTDB** map, then abundances of SGBs collapsing to one GTDB taxon are summed. Deterministic, 1:1 at the SGB (species-level genome bin) level. Join key is the **SGB id** (metaphlan's `t__SGB####` level), not any name (gtdb output has no taxid and GTDB renames taxa). Empirical: for one sample, 77 metaphlan SGB leaves (Σ 32.14) ↔ 77 gtdb species (Σ 32.14) — identical count and total.

**2. bracken ↔ everything — independent classifier, join on NCBI taxid.** bracken is kraken2/NCBI; both bracken (`taxonomy_id`) and metaphlan (terminal element of `clade_taxid` lineage) carry NCBI taxid. But coverage is partial: of 433 metaphlan species taxids, only **232 (53%)** exact-join bracken's — metaphlan SGBs often have no species-level NCBI taxid (uncharacterized bins). Handle at species taxid where present, **fall back to genus**, mark the rest unmatched. Unmatched SGBs are biology, not a bug.

**What we use:**
- **NCBI taxonomy backbone** (`taxdump` nodes.dmp/names.dmp) — the lingua franca both metaphlan and bracken speak; resolves any taxid → canonical name, rank, lineage.
- **MetaPhlAn's SGB↔GTDB↔NCBI mapping** (ships with the mpa DB) — authoritative metaphlan↔gtdb bridge, and it assigns each SGB its NCBI taxid (filling some bracken gaps).
- Optionally **GTDB's own taxonomy** for GTDB-native genus/family rollups.

**Form: a `taxon` dimension table**, star-schema.
- One row per taxon node, **versioned by `db_version`** (`mpa_vJan25_CHOCOPhlAnSGB_202503` — exactly why the ETL captures it; a new mpa release redefines SGBs, so the crosswalk is version-scoped).
- Columns: surrogate `taxon_key` + native ids per system (`sgb_id, ncbi_taxid, ncbi_species, ncbi_lineage, gtdb_species, gtdb_lineage`) + `rank` + rollups (`genus, family, phylum`).
- Fact tables store only the **method-native key they have** (metaphlan/bracken → `ncbi_taxid`, metaphlan also `sgb_id`; gtdb → `gtdb_lineage`/`sgb_id`). Queries join `taxonomic_profile_metaphlan ⋈ taxon ON (db_version, sgb_id)` (or `taxonomic_profile_bracken ⋈ taxon ON (db_version, ncbi_taxid)`).
- Built **once per db_version** as its own small step (parse mpa metadata + taxdump), never per sample. Tiny next to the facts.

**Two caveats to state up front:**
1. **Identity harmonization ≠ abundance harmonization.** Matching a taxon across methods does not make the *numbers* comparable — metaphlan rel_ab is marker/genome-size-normalized, bracken is read-count fraction. The crosswalk unifies *who*, not *how much*.
2. **Coverage is partial** (the 53% above); the dimension enables graceful species→genus fallback, not a perfect equijoin.

## Possibility: drop the gtdb pipeline step, derive it by join

Because gtdb_profile is a deterministic relabel of the metaphlan SGB leaves (evidence above), the `gtdb` process can be **removed from the pipeline** and GTDB materialized as `SGB → gtdb_lineage` join + `SUM(relative_abundance)` rollup against the `taxon` dimension. Saves per-sample compute; gtdb becomes a view/derived table rather than a stored, separately-ingested one (though gtdb is small — ~2.8k rows/sample — so the storage saving is minor; the real win is dropping the pipeline step and the second ingest path).

**Guardrail — validate before deleting.** We still have the stored `gtdb_profile` files, so validation is free: reproduce GTDB from the `t__` rows + the SGB→GTDB map and **diff against the stored files** to N decimals on a few dozen samples. Only then remove the step. Edge cases where a reimplementation drifts: `UNCLASSIFIED` handling, renormalization, and SGBs with no GTDB assignment.

## Possibility: Greengenes2 (and any other target taxonomy)

The pattern generalizes: **any target taxonomy reachable by a crosswalk from a key we already hold (SGB, NCBI taxid, or GTDB lineage) becomes a dimension column + post-hoc join, not a pipeline step.**

For GG2 specifically, GG2's genome-side taxonomy is **GTDB-aligned**, so the tractable route is a **second hop: SGB → GTDB → GG2** (a GTDB↔GG2 label crosswalk), or more robustly `SGB → representative-genome-accession → GG2 feature` (GG2 is built on genomes with accessions; SGBs have representative-genome accessions).

**Two honest limits:**
1. **The crosswalk artifact must exist and be version-matched.** GTDB↔GG2 / genome→GG2 mapping is very likely reachable but is **unverified** — hold it to the same standard as gtdb (confirm the file, then validate) before promising a clean join.
2. **A label join gives GG2 names, not GG2 phylogeny.** If the goal is GG2's *tree* (UniFrac, Faith's PD, phylogenetic placement), a taxonomy-string crosswalk does not deliver it — that needs real GG2 feature IDs on the GG2 reference tree, which is genuine new work, not a free dimension column. Pin down which is wanted before scoping.

## Open questions / next steps

- **Add `outputs[]` to `manifest.json`?** Highest-leverage pipeline tweak — makes the ETL fully manifest-driven and self-verifying.
- **Validate gtdb derivation:** locate the MetaPhlAn SGB metadata (in the `metaphlan4.2.2` container / `store_dir`), build the `taxon` dimension for `mpa_vJan25_CHOCOPhlAnSGB_202503`, reproduce & diff gtdb_profile, and re-measure metaphlan↔bracken overlap *with* genus fallback.
- **Confirm a GG2 crosswalk** (GTDB↔GG2 or genome→GG2) exists in usable, version-matched form.
- **Marker store decision:** given markers are ~89% of rows and rarely queried by microbe, decide whether they live in the main lake, a separate coarse store, or are built lazily.
- **Reconcile with target storage:** outputs are currently in `gs://cmgd-data`; `storage-layout.md` targets R2 `cmgd-raw`. This ETL feeds a **dedicated cmgd DuckLake** — its own catalog, for independent full time travel, *not* a shared multi-tenant schema (see [`publish-and-catalog-design.md`](./publish-and-catalog-design.md) → Location; the published artifact is a periodic frozen snapshot of it with a DuckDB-file catalog) — regardless of source bucket. (This supersedes any "shared `cdsci-lake` schema-per-project" framing for cmgd outputs.)
