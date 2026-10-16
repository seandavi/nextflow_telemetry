# 0012. Multi-source sample metadata in the lake

- **Status:** Accepted
- **Date:** 2026-10-16
- **Deciders:** Sean Davis

## Context

Sample metadata now comes from two external sources:

- **cMD curation**: `waldronlab/curatedMetagenomicDataCuration`,
  `inst/curated/<study>/<study>_sample.tsv`. It is human-curated, keyed by
  study and sample, and joins to reads only through `ncbi_accession`. Its
  columns differ per study.
- **Olm's Microbiome Repo**: GitHub-released data packages (R2026.25 is
  package 1.20.0), CC-BY-4.0. Values are machine-extracted by route (R1
  BioSample attributes, R2 supplementary tables, R3 paper text, R4 abstracts),
  each with a confidence and an evidence source. 79,548 of its values are
  imported from cMD itself (`evidence_source = 'external.cmd'`). It publishes
  about 2.5 releases a week.

A release (ADR-0011) must say which source and release each value came from.
It is built from one shared-lake snapshot. [0009](0009-catalog-in-postgres-control-plane-in-worker.md)
placed curated annotations in the Postgres catalog. That still fits values
people write. It does not fit bulk external tables that are versioned
upstream, and nothing calls the v1 `curated_sample_annotations` path today.

Plan: `docs/plans/onboarding-and-publication.md`, decisions D1, D3 and D4 on #266.

## Decision

1. **Source values live in the shared DuckLake, schema `cmgd_meta`**
   (`lake.cmgd_meta.*`), next to `lake.cmgd.*`, not in the retired
   `cmgd_lake` catalog. One snapshot then covers results and metadata. This
   amends 0009 for externally curated *values*: the catalog keeps
   submissions and human annotations, and the lake holds source releases.
2. **Each source is loaded as published, keyed by `(release, natural key)`.**
   `nf-meta load olm|cmd` writes through `cdsci.lake.upsert` inside an
   `ops.run`, so reloading a release is a no-op and each change is one
   attributed snapshot. `source_releases` records every loaded release
   (sha256, URL, DOI, licence) and its `pinned` flag. The views read only the
   pinned release, so moving the pin is a one-row reviewed change.
   - Olm: `olm_studies`, `olm_runs`, `olm_samples_wide`, `olm_determinations`
     (the long table, so `evidence_source` survives), `olm_sandpiper_summary`.
     Every file is checked against the package's `VERSION.json` sha256 and row
     count before any write, and the load fails unless each current wide value
     joins exactly one current determination.
     `olm_sample_values` is the wide table unpivoted (one row per sample and
     field, with route, confidence and the determination's `evidence_source`).
     It is a table written at load, not a view, so the views over it stay
     cheap.
   - cMD: `cmd_sample_values`. Each TSV is unpivoted to one row per sample
     and field. `<f>_ontology_term_id` and `<f>_unit` columns ride along as
     `value_ontology` and `value_unit`. The release is the curation commit
     SHA.
3. **Readset ids join the sources; BioSample stays a join key**
   ([0007](0007-readset-identity.md)). `sample_xref` has one row per
   `(readset_id, run_accession)`. It covers v2's registered readsets, plus
   the readsets that registration would compute from each pinned Olm sample's
   runs and each cMD sample's `ncbi_accession`. `origins` records which of
   these produced each row. It is rebuilt in full by `nf-meta load xref`.
4. **Harmonization is views plus a seed CSV, not a rules engine.**
   `field_map.csv` maps six fields (`age_years`, `sex`, `country` ISO-2,
   `bmi`, `antibiotics_current_use`, and `health_condition` as case vs
   healthy control) and names a transform. `country_iso2.csv` maps cMD
   country names.
   - `sample_values`: every source's long rows, with the source's own and
     the harmonized field and value.
   - `sample_harmonized`: one value per readset and field. cmd wins, then Olm
     R1/R2, then Olm R3/R4 with confidence ≥ 0.5. Olm rows from
     `external.cmd` never count as Olm. Nothing is invented.
   - `compare_cmd_olm`: per cMD study and field, Olm with (`olm_rows='all'`)
     and without (`'independent'`) its `external.cmd` rows. Samples pair
     when they share a run. Age agrees within 1 year and BMI within 0.1;
     other fields must be equal after `field_map`. Mapped "no value" spellings
     such as cMD's `Not applicable` count as absent.
5. **Licence per table.** `olm_*` tables carry CC-BY-4.0. The ops source
   comment gives the licence; the attribution names the release and DOI. Any
   Olm column whose name contains `quote` (`evidence_quote`,
   `intervention_evidence_quote`) is not loaded. cMD-derived tables are
   CC0-1.0.

## Alternatives considered

- **The `cmgd_lake` catalog.** Rejected (D1). It is retired, and metadata in
  it could not be inside a release's checksummed manifest.
- **Postgres catalog tables (`curated_sample_annotations`).** Rejected for
  source values. Hundreds of MB of versioned upstream tables would sit in an
  OLTP database outside the release snapshot.
- **Only Olm's wide table.** Rejected (D3). It loses `evidence_source`, and the
  comparison would mostly compare cMD with itself.
- **Following every Olm release.** Rejected. Releases load side by side and
  the pin moves by hand.

## Consequences

- Olm's resolved value per field is what `sample_values` carries for Olm. The
  long table holds one current determination per sample and field in 1.20.0,
  so nothing is lost. If Olm starts keeping several, the load fails loudly on
  the 1:1 check and this needs revisiting.
- The `cmgd_meta` views are DDL: `nf-meta views` recreates them on demand.
  Loads never touch them, so a reload stays snapshot-free.
- Load order: `load olm` and `load cmd`, then `load xref`, then `views`.
- Predicted readsets assume registration groups a BioSample's gut WGS runs as
  Olm does. Once Phase 3 registers samples, the `v2` origin is authoritative.
- ADR-0011's "licence set in one place" now has a per-table exception for
  Olm-derived tables. Phase 4 carries it into the release manifest.

## References

- #266 (epic), #267 (Phase 1), #268 (Phase 2)
- `docs/plans/onboarding-and-publication.md`
- `src/nextflow_telemetry/meta/`
- monode `infrastructure/DUCKLAKE.md` (write contract)
