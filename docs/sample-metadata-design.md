# Sample metadata — design positions

> Status: **draft v2** — addresses #55 with a corrected framing: most samples have no curation, and metadata harmonization is a separate module from telemetry. Read the meta-issue first for context.

## Framing

Two facts that constrain the design:

1. **Most samples have no curation.** Roughly 10× more samples will flow through this pipeline as bare SRA accessions (BioProject → samples loaded via `load_bioproject.py`) than as cMD-curated cohorts. The default sample carries SRA + BioSample-derived metadata only. cMD-style curated annotations are a minority overlay on top of a much larger raw substrate.

2. **Telemetry is operational; harmonization is not.** The existing separation of `samples_tbl` (content-addressed, SRA-derivable) from `curated_sample_annotations_tbl` (cMD-format, manually curated) was deliberate. Harmonization — turning messy free-text metadata + abstracts into normalized, ontology-backed facets — should live in a **separate module with its own lifecycle**, not be baked into the telemetry server's schema.

What this rules out:
- Promoting harmonization tables (publications, vocabularies, provenance, conflict review) into the telemetry server's database.
- Treating cMD's schema as the canonical sample-metadata shape. cMD is one of several inputs to harmonization.
- Coupling dispatch / reconcile / orchestration to harmonization state (a "dispute" on a body_site field doesn't gate execution).

What stays in scope:
- A clear contract for what the telemetry server stores about samples.
- A clear contract for what the harmonization module reads from telemetry and writes back (or doesn't).
- An explicit boundary so we can build harmonization later without retrofitting telemetry.

## Two layers

```
┌─────────────────────────────────────────────────────────────┐
│  telemetry server (this repo)                               │
│  - samples (content-addressed, SRA-derivable fields)        │
│  - curated_sample_annotations (cMD TSV imports, JSONB)      │
│  - jobs / runs / workflows / telemetry / process_metrics    │
│  - dispatch / reconcile / completion                        │
│  Operational. No LLM. No vocabularies. No publications.     │
└──────────────────┬──────────────────────────────────────────┘
                   │ read-only API: samples, accessions,
                   │ curated_sample_annotations.metadata_
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  harmonization module (separate package/service)            │
│  - publications (PMID/DOI, abstract, fulltext)              │
│  - vocabularies (mirror of cMD data dictionary + OLS4)      │
│  - sra_attributes (raw BioSample/SRA XML attributes)        │
│  - harmonized_facets (LLM/rule-based normalized values      │
│     with provenance, confidence, citations)                 │
│  - review queue, accept/reject endpoints                    │
│  Owns its own DB (or schema). Re-runnable. Versioned.       │
└─────────────────────────────────────────────────────────────┘
```

The harmonization module **reads** from telemetry. It does not write back into telemetry tables. Consumers (frontends, downstream analysis) that want harmonized facets for a sample call the harmonization module directly, joining on `samples.sample_id`.

## Telemetry-side data model (this repo)

The minimum required to support the operational work, plus a small SRA-derived footprint so common queries don't require harmonization to be running.

### `samples` (existing, mostly unchanged)

Stays content-addressed (`sample_id` = md5 of sorted SRR list). Add a small set of columns that are **directly derivable from SRA/BioSample without any harmonization decisions**:

```
ALTER TABLE samples
    ADD COLUMN organism text,             -- 'Homo sapiens' | 'Mus musculus' | NULL
    ADD COLUMN library_strategy text,     -- WGS | AMPLICON | RNA-Seq | …  (SRA enum, verbatim)
    ADD COLUMN library_source text,       -- METAGENOMIC | METATRANSCRIPTOMIC | …
    ADD COLUMN platform text,             -- ILLUMINA | OXFORD_NANOPORE | …
    ADD COLUMN instrument_model text,     -- 'Illumina NovaSeq 6000' (SRA verbatim)
    ADD COLUMN total_spots bigint,
    ADD COLUMN total_bases bigint;
```

These are SRA-canonical values, fetched once per accession at sample-registration time, populated verbatim from the SRA XML. **No vocabulary mapping, no LLM, no decisions** — if SRA says `ILLUMINA`, that's what we store. If a value is missing or ambiguous in SRA, the column is NULL. Used for:
- Coarse dispatch filters (`organism = 'Homo sapiens'`)
- Sanity checks ("are we accidentally claiming non-metagenomic samples?")
- Sample browsing in the UI without round-tripping the harmonization module

Not in scope for these columns: `body_site`, `disease`, `condition`, `host_phenotype`, anything controlled-vocabulary or interpretation-laden. Those live in harmonization.

### `curated_sample_annotations` (existing, one column added)

```
ALTER TABLE curated_sample_annotations
    ADD COLUMN cmd_sample_id text;        -- so cMD-format export can round-trip
```

The `metadata_` JSONB stays the verbatim TSV row. No schema change to the JSON shape. The harmonization module reads from here and uses it as one of its inputs; it does not write back.

### `sra_attributes` (new — but lives where?)

Raw BioSample/SRA XML attributes (the messy `key=value` pairs typed by submitters: `host_disease=ulcerative colitis`, `env_broad_scale=human-associated`, etc.) are needed by the harmonization module. **Open question**: do they live in the telemetry DB (because they're populated at sample-registration time, alongside the SRA-derivable columns above) or in the harmonization module's DB (because no operational code reads them)?

Argued either way. **Position**: put them in the telemetry DB as a thin `sra_attributes(sample_id, attribute_name, attribute_value, source)` table — fetched at sample-registration time alongside the columns above, written-once. Pro: single fetch path, normalized location, frontends can show "raw SRA attributes" for any sample without touching harmonization. Con: telemetry DB grows by ~10–30 rows per sample. At our volumes this is fine.

### Nothing else

No publications. No vocabularies. No provenance. No `sample_field_*` tables. Those are harmonization's problem.

## Harmonization module (separate, sketched)

Not implementing now. Sketched to make the boundary explicit.

### Lifecycle independence

- **Separate package**: probably `packages/nf_harmonize/` or a sibling repo — TBD based on whether it ever needs different deploy targets.
- **Separate database / schema**: probably its own Postgres database. Cross-DB joins are fine via FDW or a thin gateway service that joins server-side. Keeping schemas separate avoids accidentally treating harmonized facets as authoritative in operational queries.
- **Separate release cadence**: prompts, models, and vocabularies change far more often than dispatch logic. Decoupling versioning means we can rev harmonization weekly without touching the telemetry server.
- **Re-runnable**: a re-run on the same inputs with a new model produces a new result set. Old results stay accessible for evaluation and audit.

### What it owns

- `publications(pmid, doi, title, abstract, year, authors, fulltext_uri, fetched_at, raw_jsonb)`
- `study_publications(study_name, pmid, role)` — only for studies that exist in `curated_sample_annotations`. Uncurated samples have no `study_name`.
- `vocabulary_fields`, `vocabulary_terms` — mirror of cMD `cMD_data_dictionary.csv` + OLS4-resolved dynamic enums, pinned to a cMD repo SHA.
- `harmonized_facets(sample_id, field, value, curie, source, source_evidence, confidence, model, status, set_at)` — append-only with status (`pending|accepted|rejected|superseded`).
- Review-queue endpoints, accept/reject endpoints, harmonization-run records.

### What it reads from telemetry

- `samples` (the SRA-derivable columns + `sra_attributes`)
- `curated_sample_annotations` (when present)
- Sample identifiers (the cMD `study_name` for fetching the right paper(s))

Read-only. Via stable HTTP API on the telemetry server. No direct DB joins across modules.

### How consumers use it

A frontend wanting to display "body_site" for a sample does **two** API calls (or one to a gateway): `GET /samples/{id}` → operational facts; `GET /harmonize/samples/{id}/facets` → harmonized values with provenance. The harmonization API can be missing entirely (module not deployed) and the operational layer still works.

## cMD round-trip

cMD's curated TSV format remains a first-class input and output, but it lives at the **boundary**, not the core:

- **Input**: `seed_from_tsv.py` writes to `curated_sample_annotations.metadata_` verbatim. Already works.
- **Output**: a `harmonize export-cmd --study=…` command (in the harmonization module, not telemetry) reads accepted facets + curated annotations + SRA-derivable fields, projects them onto the cMD4 schema, and emits a TSV. Validates against `OmicsMLRepoCuration::validate_data_against_schema()` as the last step.

The export tool only runs against samples that have a `study_name` (i.e. exist in `curated_sample_annotations`). Uncurated samples never round-trip to cMD because they were never cMD samples to begin with.

## Open questions

- **`sra_attributes` location**: telemetry DB or harmonization DB? Position above is "telemetry," but worth pushback. The argument for "harmonization" is that nothing in the operational path reads attributes; the argument for "telemetry" is single-fetch-path and avoiding a network round-trip for any UI that wants to show raw SRA metadata.
- **Harmonization deployment shape**: package-in-repo, sibling repo, or separately deployed service? Affects whether cross-DB queries are even an option. Smallest first: a Python package in `packages/nf_harmonize/` with its own SQLAlchemy metadata, deployed alongside the telemetry server, but using a separate DB connection string. Can be split out later if it grows.
- **Module-to-module API shape**: REST gateway on the telemetry server (`/harmonize/*` mounted but proxy-only)? Or harmonization runs on its own port and clients are responsible for hitting both? The former is friendlier for frontends; the latter is purer.
- **Subject identity across uncurated samples**: cMD's `subject_id` is curator-assigned. For the 10× uncurated case, we have no subjects — only BioSamples. Decide whether the harmonization module ever attempts subject inference (e.g. multiple BioSamples sharing a `host_subject_id` attribute) or stays sample-level only.
- **Validator integration**: same question as before — Rscript sidecar invoking `OmicsMLRepoCuration` or Python port. Decide when the export path is built.

## Suggested next steps (telemetry side only)

Ordered, smallest first. Each is independently shippable.

1. **SRA-derivable columns on `samples`** (small): migration adding `organism`, `library_strategy`, `library_source`, `platform`, `instrument_model`, `total_spots`, `total_bases`. Backfill via a `populate-from-sra` admin endpoint that fetches via E-utils for a list of `sample_id`s. `load_bioproject.py` populates them at registration time going forward. **High value, no harmonization dependency** — closes a gap that bites every uncurated-sample query.
2. **`sra_attributes` table + populate-from-sra ingestion** (small): single new table. Same fetch path as #1, written in the same transaction. Sets up the substrate that harmonization will consume later. Operational value too: frontend can show raw SRA attributes for debugging.
3. **`cmd_sample_id` column on `curated_sample_annotations`** (trivial): one column, populated by `seed_from_tsv.py`. Closes the cMD round-trip gap that exists today regardless of whether harmonization ever ships.

That's it for the telemetry-side roadmap. The harmonization module is a separate planning exercise — once we have #1–#3 plus a handful of real samples loaded, we have everything the harmonization service needs to read, and we can scope its first ticket without changing this server.

## What this design explicitly does NOT do

- No publications table in the telemetry DB.
- No vocabulary tables in the telemetry DB.
- No `sample_field_provenance` table in the telemetry DB.
- No conflict-review endpoints in the telemetry server.
- No LLM dependencies in the telemetry server's deps tree.
- No biome ontology (project is human + mouse).
- No Temporal (shelved separately).
- No cMD `sample_id` as a join key (curators use it, our content-addressed `sample_id` is the join key).

If a feature appears in the harmonization-module section, it does not belong in the telemetry server. That's the line.
