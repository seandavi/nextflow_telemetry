# Plan: sample onboarding, multi-source metadata, and publication (MVP)

_Drafted 2026-10-15. Inputs: [`docs/research/olm-microbiome-repo.md`](../research/olm-microbiome-repo.md),
[`docs/research/singlem-sandpiper.md`](../research/singlem-sandpiper.md), ADR-0005/0007/0009/0010/0011,
[`docs/plan-v2-cutover-and-metadata-phase1.md`](../plan-v2-cutover-and-metadata-phase1.md) (#193),
`docs/etl-runbook.md`, `docs/data-access.md`, `cf/README.md`, the Olm data package
R2026.25 / data-v1.20.0, the cMD curation repo, lwaldron's issues #185–#190, and GETs against
the live Worker and `cmgd-public`. Every number below was queried, not assumed; the queries are
in [Appendix A](#appendix-a-queries-behind-the-numbers)._

## What the MVP delivers

About 470 samples from four studies go through the whole path, end to end:

1. They are registered from a request (an add-study issue, or a row picked from Olm).
2. They run through `cmgd_mpa4.2 2.3.0` on the clusters.
3. Their results land in the lake as Parquet.
4. They are published, with their metadata from two sources (cMD and Olm), to `cmgd-public`,
   Zenodo and a Hugging Face Dataset repo.
5. A fresh agent can get from the landing URL to any sample's profile and metadata in four
   fetches or fewer.

Most of the machinery already exists. The new code is small and sits in five places:

- an Olm loader
- a cMD loader
- a cost estimate in the add-study preview
- a few extra release artifacts (metadata tables, `samples` table, `llms.txt`, HTML)
- two upload commands (Zenodo, HF)

## Decisions to confirm before Phase 1

These came out of reading the repo. Each one changes where code goes, not whether the plan works.

| # | Finding | Recommendation |
|---|---|---|
| D1 | **The metadata catalog.** You chose the cmgd DuckLake (`cmgd_lake`, data at `r2://cmgd-data/lake/`). But `docs/etl-runbook.md` §4 calls `cmgd_lake` a *retired prototype* ("nothing reads it now") and has dump-then-drop steps for it. ADR-0011 put all results in the shared lake as `lake.cmgd.*`. `nf-etl publish` builds each release with cdsci-lake `publish_release` from **one** lake snapshot. Metadata in a different catalog therefore cannot be inside a release's checksummed manifest. It would have to ride next to it, like `studies/`, unlisted. | Keep the design exactly as decided: per-source tables, provenance columns, a harmonized view. But put the tables in the **shared lake under schema `cmgd_meta`** (`lake.cmgd_meta.*`). Then one snapshot covers results and metadata, and `publish_release` checksums both. If you keep `cmgd_lake`, stop the drop in the runbook. Phase 4 then exports the metadata as side artifacts with their own sha256 in `metadata/index.json`. Whichever you choose, the only code difference is the `ATTACH` target. This doc writes `<meta>` for the schema. |
| D2 | **The add-study flow does not work today.** Three reasons. `POST /submissions` exists only on v1 (`cf/README.md`: "Not built: `/submissions*`"). v1's `services/submission.py` does not write through to v2 (the #195 item is open). And #185–#190 never got the `add-study` label, so the action skipped them (#193 F7). | The MVP needs that write-through: the #195 item "Catalog writes through to v2 `POST /api/samples`" and the #196 item "fix add-study trigger". Do those two items as Phase 3's first step instead of waiting for the full catalog carve-out. |
| D3 | **Olm already contains cMD.** Olm's `gut_sample_determinations` has **79,548** rows with `evidence_source = 'external.cmd'` (route R2). On the cMD-overlap samples, R2 is the dominant route for age (8,681 R2 vs 2,861 R1), sex (11,667 vs 2,883) and health condition (15,032 vs 1,487). A naive cMD-vs-Olm comparison would mostly compare cMD with itself. | Ingest Olm's **long** determinations table, not only the wide one, so that `evidence_source` survives. The comparison and the harmonized view drop `external.cmd` rows when they treat Olm as an independent source. |
| D4 | **Licence.** cmgd releases are CC0-1.0 (`etl/publish.py`). Olm is CC-BY-4.0. | Publish Olm-derived tables with `license: CC-BY-4.0` per table. The release manifest already has a per-table `license` field (checked on the live `cmgd_nextflow-2.2.1/2026-10-08/manifest.json`). Amend ADR-0011's "licence set in one place" line accordingly. Do not redistribute Olm's `evidence_quote` text. It is verbatim from papers, so its terms are unclear. |

## MVP cohort (chosen from data)

| Study (cMD name) | BioProject | Source of request | Samples | Gbp | Median Gbp/sample | Olm metadata coverage (age / sex / country / health) | cMD curation | Est. core-h @ 24/sample | Est. Anvil SU (model) |
|---|---|---|---|---|---|---|---|---|---|
| NishiwakiH_2024 | PRJDB15956 | lwaldron #188 (pMD) | 167 | 415.9 | 2.47 | 1.0 / 0.0 / 1.0 / 1.0 | yes (`inst/curated`) | 4,008 | 1,684 |
| JoS_2022 | PRJNA743718 | lwaldron #186 (pMD) | 156 | 403.1 | 2.58 | 0.0 / 0.0 / 1.0 / 1.0 | yes | 3,744 | 1,557 |
| PernigoniN_2021 | PRJNA749645 | Olm × cMD overlap | 74 | 230.8 | 2.96 | 1.0 / 1.0 / 1.0 / 1.0 | yes | 1,776 | 1,099 |
| HoylesL_2018 | PRJEB14215 | Olm × cMD overlap | 73 (cMD lists 56) | 289.5 | 3.87 | 1.0 / 1.0 / 1.0 / 1.0 | yes | 1,752 | 1,063 |
| **Total** | | | **470** | **1,339** | | | | **11,280** | **5,403** |

Why these four:

- **All four have both sources.** Each has a cMD curated `*_sample.tsv` upstream (HTTP 200 from
  `waldronlab/curatedMetagenomicDataCuration/master/inst/curated/<study>/`) and current Olm
  rows. That gives the cMD-vs-Olm comparison on the same samples.
- **None is in the 2026-06-30 cMD release CSV.** The pMD studies are listed as `not_processed`
  in `studies_status.csv`.
- **The data is uniform.** All runs are Illumina WGS (the two pMD studies have exactly one run per sample). No sample
  carries an Olm/Sandpiper QC flag (0 of 421 profiled).
- **They are cheap.** At 2.5–3.9 Gbp median per sample they sit near the Olm corpus median
  of 4.09 Gbp.
- **HoylesL_2018 is a deliberate test case.** It has 73 BioSamples in Olm and ENA but 56
  in cMD curation. It exercises the "which samples does each source include" question.
- **Two of lwaldron's six pMD studies, not all six.** BoktorJC_2023 (192), VilletteR_2025
  (122, 6.6 Gbp median), ZhangM_2023 (24) and MazmanianS_MoiseyenkoA (24 BioSamples in Olm's
  registry, not in the gut catalog) follow as the first post-MVP batch. They use the same path.

How the two estimates are computed:

- **Core-h** uses the 2.3.x rehearsal figure: 24 core-h and ~50 GB peak scratch per sample.
- **The SU model** is `docs/research/resource-tuning-2.3.0.md`'s recommended fit,
  `Anvil SU = 6.59 + 0.198·M`, where M is million raw reads. I set M = 2 × spots, assuming
  paired-end; that assumption is unverified.

The two estimates differ by about 2×, because the model was fitted on 2.2.x. Phase 3 refits it
on the rehearsal's own telemetry. Budget against the rehearsal number until then.

- **Compute.** 11.3k core-h is about 1.5% of one 750k-SU allocation.
- **Scratch.** Peak scratch is 50 GB × the number of concurrently running samples. At the
  current 25-per-batch × 10-runs setting that is up to 12.5 TB per cluster. That limit
  matters for the disk-limited Bridges-2 (#244), which should take a smaller share.

## cMD ↔ Olm overlap (measured)

| Measure | Value |
|---|---|
| cMD curated release (local `cMD_curated_metadata_release.csv`, 2026-06-30) | 93 studies, 22,411 samples, 41,890 runs; 20,853 feces samples |
| cMD samples with at least one run in Olm `gut_runs` and a catalog `sample_key` | **17,232 / 22,411 (77%)**: 16,321 of 20,853 feces (78%), 911 of 1,558 non-feces |
| cMD samples whose runs appear anywhere in Olm's registry | 17,791 (16,322 feces + 1,469 non-feces) |
| cMD studies with any run in the Olm gut catalog | **78 / 93**; 75 of them have ≥90% of their samples in Olm |
| `studies_status.csv` BioProjects (152 studies incl. not-yet-processed) in Olm current gut studies | **129 / 152** |
| Olm field coverage on the 17,479 overlapping sample keys | age 11,572, sex 14,580, country 16,946, health 16,550, antibiotics 12,883, BMI 8,483 |
| Olm determinations sourced from cMD itself (`external.cmd`) | 79,548 (see D3) |
| Olm gut corpus, Illumina/BGI WGS, current catalog samples | 521,508 samples, 2.73 Pbp, median 4.09 Gbp, p90 11.2 Gbp |
| Corpus cost at 24 core-h/sample | 12.5 M core-h, against ~1.5 M SU on Anvil + Bridges-2. Roughly 60k samples, 12% of the corpus, fit |

The cMD/Olm overlap is high because Olm imports cMD (D3). The question worth asking is whether
Olm's independent routes (R1 BioSample attributes, R3 paper text) agree with cMD's human
curation. The MVP comparison answers that on the four studies above.

## Architecture: five deep modules, five seams

```
request ──▶ [1 Onboarding] ──▶ v2 Worker (exists) ──▶ clusters (exist) ──▶ cmgd-raw
                 ▲                                                       │
     [2 Metadata store] ◀── Olm package, cMD curation                    ▼
                 │                                   [3 Staging: nf-etl ingest] (exists)
                 └───────────────▶ [4 Release: nf-etl publish] ◀──────────┘
                                          │
                                          ▼
                     [5 Mirrors: Zenodo, HF]   cmgd-public (index.json, llms.txt, HTML)
```

| Module | Interface (the seam) | Hides | Status |
|---|---|---|---|
| 1 Onboarding | `nf-client submit-study ACC [--dry-run]`; the add-study issue | ENA resolution, readset ids, write-through to v2, collection membership, cost estimate | Exists on v1; write-through and estimate missing |
| 2 Metadata store | `nf-meta load <source> <path-or-url>`; SQL views `<meta>.sample_values`, `<meta>.sample_harmonized` | Each source's file format, release pinning, unpivoting, precedence | New |
| 3 Staging | `nf-etl ingest` / `tick` | Output specs, parsers, MARK_COMPLETE gate | Exists (`etl/engine.py`, `specs.py` covers `cmgd_mpa4.2 2.3.0`) |
| 4 Release | `nf-etl publish --registration W/V [--sync]` | `publish_release`, per-study artifacts, indexes, pages | Exists; gains metadata tables, `samples` table, `llms.txt`, HTML |
| 5 Mirrors | `nf-etl mirror --release R --to zenodo\|hf` | Zenodo deposit and versioning API, HF upload and dataset card | New |

The two new modules (2 and 5) are CLIs over a directory or URL. Neither adds a service, a
database or an endpoint.

## 1. Olm onboarding

**What we ingest.** Pin each load to one release: `R2026.25` = package `1.20.0`, DOI
10.5281/zenodo.23087712, CC-BY-4.0. Load these tables with no column changes, plus `source`,
`release` and `package_version` columns.

| Olm file | Rows (1.20.0) | Lake table | Why |
|---|---|---|---|
| `gut_studies.parquet` | 2,946 (2,798 current) | `<meta>.olm_studies` | Study-level selection fields (`n_runs`, `cov_*`, `body_site_primary`) |
| `gut_runs.parquet` | 718,658 | `<meta>.olm_runs` | run ↔ BioSample ↔ study, `base_count`, `read_count`, platform; drives cost estimates |
| `gut_sample_metadata_wide.parquet` | 602,163 | `<meta>.olm_samples_wide` | Olm's own resolved value per field (after precedence and conflict withholding) |
| `gut_sample_determinations.parquet` | 4.2 M | `<meta>.olm_determinations` (minus `evidence_quote`) | `evidence_source`, `determined_by`; needed for D3 |
| `gut_sandpiper_sample_summary.parquet` | 339,626 | `<meta>.olm_sandpiper_summary` | QC flags for prioritization |
| `VERSION.json` / `manifest.json` | — | `<meta>.source_releases` row | sha256 per file, release id, DOI, licence |

`registry_*` tables, relatedness and PCA wait for a later phase. Nothing in the MVP reads them.

**Load mechanism.**

- `nf-meta load olm <release-zip-url>` downloads the zip into a fresh directory.
- It verifies every file's sha256 against the package's own `manifest.json`.
- It then writes each table with the DuckLake contract in `monode/infrastructure/DUCKLAKE.md`:
  MERGE by natural key, gated on `_row_hash`, with a commit message.
- The natural key is `(release, <table key>)`, so a release is loaded once.
- Reloading the same release is a no-op.
- Untrusted-input hygiene: the loader runs from its own directory with `python -I` and never
  executes anything from the package (the zip contains an `.ipynb`).

**New releases.** Olm released 30 times in 17 days.

- We do **not** follow every release. `<meta>.source_releases.pinned` marks the release the
  harmonized view reads. Bumping the pin is a one-row update, reviewed like any data change.
- Older releases stay queryable, so the comparison can be re-run against the new pin and diffed.
- Re-ingest is manual in the MVP. A weekly systemd timer that loads the latest release *without*
  moving the pin is a later phase. It follows `SCHEDULING.md`: `OnFailure=ntfy-notify`, plus
  `cdsci.lake.ops.run` ledger rows.

**Attribution.**

- Every published table or file with Olm-derived columns carries `license: CC-BY-4.0` and an
  attribution string: "Microbiome Repo, OlmLab, release R2026.25 (data package 1.20.0),
  doi:10.5281/zenodo.23087712; values derived".
- The string sits in the release manifest's table `description`, the dataset card and the
  Zenodo record's `related_identifiers`.
- The Olm README says its own citation is a placeholder. Confirm the final citation with the
  authors before the first public release (Phase 5 gate).

## 2. Sample onboarding process (one path for both request types)

Both an lwaldron-style issue and an Olm-driven selection end as **one add-study issue per
BioProject**. Olm selection is a query that writes issues. It is not a second registration path.

| Step | Today (file / endpoint) | Missing | MVP change |
|---|---|---|---|
| a. Request | `.github/ISSUE_TEMPLATE/add-insdc-study.yml` → `.github/workflows/add-study.yml` (preview on open/edit, apply on `approved` label) | The template's label is not applied, so the action skips (#185–#190 have no comments). It targets v1 (`NF_TELEMETRY_URL`) | Trigger on issue title `[add-study]` as well as the label (#196 item 1) |
| a′. Olm-driven selection | — | A ranked candidate list | `nf-meta candidates`: one SQL over `<meta>.olm_studies` ⨝ `olm_runs` ⨝ `olm_sandpiper_summary`, anti-joined against v2 `GET /cohorts`. It ranks by metadata coverage and cost, and prints `gh issue create` commands for a human to run. No auto-filing |
| b. Resolve accession → BioSample/runs | v1 `services/submission.py`: ENA `filereport?result=read_run`, grouped by SRS; sets `biosample_id` and `metadata.{sra_sample, bioproject}` | `base_count`/`read_count` not requested | Add the two fields to the ENA `fields=` list |
| c. Estimate (new step) | — (no cost code anywhere; grep for `base_count`, `gbp`, `core_hours`) | Everything | The dry-run response gains `estimate: {samples, gbp, median_gbp, core_h, anvil_su, peak_scratch_gb_per_sample}`. It uses one function `estimate(base_counts, read_counts)` with the rehearsal constants (24 core-h, 50 GB) and the `resource-tuning-2.3.0.md` linear model. `scripts/format_study_preview.py` prints it in the issue comment |
| d. Approve | Human adds `approved` | Budget visibility | The comment shows the study's cost and the running total of approved-but-unfinished studies (sum over open `approved` issues). No ledger table |
| e. Submission + collection | v1 `POST /submissions` writes `submissions` + `collections` + `collection_samples` via `add_to_collection` (ADR-0005) | Nothing reaches v2 | Write-through: after the v1 transaction, `POST {V2}/api/samples {sample_id, ncbi_accession, biosample_id, collection}` per sample (#195 item 4). v2 computes `readset_id` (ADR-0007) |
| f. Scope + reconcile | v2 `POST /workflows` (upsert) carries `collections` (ADR-0010); `POST /admin/reconcile-jobs` | — | Re-POST `cmgd_mpa4.2 2.3.0` with the MVP collections appended. That registration's scope is currently 12 rehearsal collections, read from live `GET /workflows`. Check that upsert replaces `collections` (a 409 only fires on `params`) |
| g. Dispatch → completion | v2 `POST /dispatch/batch`, `/dispatch/submitted`, weblog `MARK_COMPLETE` → `completeSample` (`cf/src/index.ts`) | Priority: `claimBatch` takes the oldest pending job | None for the MVP. Scope is the throttle. Priority is a later phase |
| h. Post-completion | `nf-etl tick` (pulls `/api/workflows/{pk}/jobs?status=completed`, `etl/v2.py`) | — | None |

**Prioritization rule (MVP).** The rule is a human decision informed by the estimate. Rank
candidates by:

1. cost per sample (Gbp)
2. metadata coverage (`cov_age`, `cov_sex`, `cov_health_condition`)
3. Sandpiper QC flags. Exclude studies where more than 10% of samples carry
   `qc_low_depth`, `qc_low_complexity` or `qc_non_metagenome_strict`.

The registration's `collections` scope is the budget valve. Nothing runs that a human did not
add to it.

## 3. Multi-source metadata store

**Shape.** One long table per source, with the same columns, and a view over them. Wide
per-source tables are kept only as loaded, for debugging.

```
<meta>.source_releases(source, release, package_version, doi, license, url, sha256, loaded_at, pinned bool)
<meta>.sample_xref(readset_id, run_accession, biosample, sra_sample, bioproject)     -- one row per run
<meta>.field_map(source, source_field, field, unit_transform)                         -- small seed CSV in repo
<meta>.sample_values(                                                                 -- VIEW: union of per-source long tables
    source, release, biosample, run_accessions, field, value, value_raw,
    confidence, route, evidence_source, curator)
<meta>.sample_harmonized(readset_id, field, value, source, release, route, confidence)  -- VIEW
```

The per-source long tables behind `sample_values`:

- **`cmd_sample_values`.** Unpivot each `inst/curated/<study>/<study>_sample.tsv` row into
  field/value. `release` is the curation repo commit SHA, `confidence` is null, `route` is
  `'curated'`, `curator` comes from the TSV. Ontology ids are kept in a sibling column of
  `value_raw`. It joins on run accessions (`ncbi_accession`), because cMD has no BioSample
  column.
- **`olm_sample_values`.** Unpivot `olm_samples_wide`'s `value / __confidence / __route`
  triplets. Join `olm_determinations` on `(sample_key, field_name, route)` to attach
  `evidence_source` and `determined_by`. The Phase 1 acceptance checks that join is 1:1.

**Keys.**

- Sources are keyed by **BioSample + run accession**, not by our readset id.
- `sample_xref` maps both to `readset_id`. It is built from v2 `GET /samples`
  (`readset_id`, `ncbi_accession`, `biosample_id`) and `olm_runs`.
- This keeps ADR-0007 intact: BioSample is a join key into sources, never our identity.

**Field map (MVP fields only).** `age_years`, `sex`, `country` (ISO-2), `bmi`,
`antibiotics_current_use`, `health_condition`. `field_map` carries the unit and vocabulary
conversions:

- Olm `age_at_collection_days` / 365.25 → `age_years`
- cMD `country` → ISO-2
- `sex` values mapped onto `female`/`male`

Disease vocabularies are not harmonized in the MVP. `health_condition` is compared only as
"case vs healthy control".

**Precedence in `sample_harmonized`.** One `CASE` in the view, not a rules table:

1. `cmd`: human-curated
2. `olm` with route R1 or R2 at any confidence, excluding `evidence_source = 'external.cmd'`,
   because it would repeat cMD
3. `olm` with route R3 or R4 and `confidence ≥ 0.5`. This is Olm's own README rule.
4. Otherwise null. The view never invents a value.

Every row carries `source`, `release`, `route` and `confidence`. This is the "source of each
metadata column" that the release manifest needs.

**How cMD moves in.**

- `nf-meta load cmd <curation-repo-ref>` reads `inst/curated/*/*_sample.tsv` at a pinned
  commit. That is the same files `nf-client add-cmd` already fetches from
  `raw.githubusercontent.com`.
- v1 `curated_sample_annotations` (via `POST /curated/import`) is not used. Nothing calls it
  today, and per ADR-0009 harmonization reads from the catalog rather than writing to it.
- Retiring `curated_sample_annotations` is a later decision, out of scope here.

**Comparison on the MVP samples.** `<meta>.compare_cmd_olm` is a view producing, per study and
field:

- `n_both`
- `n_agree`
- `n_disagree`
- `n_cmd_only`
- `n_olm_only`

It is computed with and without `external.cmd` rows. Agreement is equality after `field_map`.
Age counts as agreeing within ±1 year.

Phase 2 posts the result as a table on the phase issue. It answers two questions: how often
Olm's independent routes agree with cMD curation, and which route is weakest. Olm's published
cMD benchmark covers infant fields only (research note), so this is new evidence for adult
cohorts.

## 4. Results and publication

**What exists (and stays).**

| Layer | Where | Mechanism |
|---|---|---|
| Working outputs | `cmgd-raw` (R2), `s3://cmgd-raw/<wf>/<ver>/<readset>/…`, `-profile <cluster>,r2` | Pipeline `publishDir`; `manifest.json` + `MARK_COMPLETE` per sample |
| Staged tables | `lake.cmgd.*` (shared DuckLake, `r2://cdsci-lake/`) | `nf-etl ingest`/`tick`; specs for `cmgd_mpa4.2 2.3.0` already in `etl/specs.py` |
| Public release | `cmgd-public` at `https://cmgd-public.cancerdatasci.org/<wf>-<ver>/<release>/` | `nf-etl publish` → `publish_release`: `manifest.json` (per-table sha256, schema digest, row count, licence), `provenance.json`, `lineage.json`, frozen `catalog.ducklake`, `releases.json`, `latest.json`; root `index.json` (only `cmgd_nextflow-2.2.1` today) |

**What the MVP adds to each release** (all inside `nf-etl publish`):

1. **`samples` table.** Columns: `readset_id`, `run_accessions`, `biosample`, `bioproject`,
   `collection`, `pipeline_version`, `git_revision`, `completed_at`. The values come from v2
   jobs plus the per-sample `manifest.json` `provenance`. It is the join hub a reviewer starts
   from.
2. **Metadata tables.**
   - `sample_metadata_cmd` (CC0-1.0)
   - `sample_metadata_olm` (CC-BY-4.0, without quotes)
   - `sample_metadata_harmonized` (CC-BY-4.0, because it mixes sources)
   
   All are filtered to the release's samples. Under D1's recommended option they are lake
   tables in the release manifest. Otherwise they are side artifacts under `metadata/` with
   sha256 in `metadata/index.json`.
3. **Release-level provenance.** Append a `cmgd` block to `provenance.json`:
   - the registration (`workflow_id`, `version`, `revision`, `params`)
   - the database versions pinned in `nextflow.config` at that revision: MetaPhlAn index
     `mpa_vJan26_CHOCOPhlAnSGB_202605`, Kraken `k2_pluspf_16_GB_20260226`, CARD
     `broadstreet-v4.0.1`
   - the `<meta>.source_releases` rows used
   
   The pipeline's per-sample `manifest.json` does **not** record Kraken or CARD versions. Adding
   a `databases{}` block there is a later pipeline change. Until then the registration pins
   them, because ADR-0010 makes params immutable per version.
4. **Discovery files at the bucket root:** `llms.txt` and `index.html` (section 5).

**Mirrors (new, `nf-etl mirror`).** Each mirror is a copy of a finished, verified release
directory. A mirror never rebuilds anything.

| Target | What | Versioning | Credential (GSM, `terraform/README.md` naming) |
|---|---|---|---|
| Zenodo | The release dir minus marker tables if over the size cap: `manifest.json`, `tables/*/data/*.parquet`, `catalog.ducklake`, `studies/`, `metadata/`, plus a README | One concept DOI for cmgd. Each release is a new version (Zenodo "new version" API). The DOI is written back into the release's root `index.json` entry and `llms.txt` | `cmgd-zenodo-api-token` |
| Hugging Face Dataset repo (e.g. `cmgd/curatedMetagenomicData`) | The same Parquet, laid out as `data/<table>/*.parquet` with a `README.md` dataset card. The card's YAML `configs:` maps one config per table so the HF viewer and `datasets.load_dataset` work | One git tag per release id. `main` = latest | `cmgd-huggingface-api-token` |
| `cmgd-public` | Unchanged: the canonical copy. Mirrors cite its URLs | `releases.json` | existing `cdsci-r2-*` |

Both credential secrets are new. Create them in GSM per `monode/infrastructure/terraform/README.md`.
The tokens never go into the app repo.

**Web pages.**

- No public static site exists today. `cmgd.cancerdatasci.org` is the operator dashboard
  (nginx container on onclappc02), and its Studies page reads `studies_status.csv`.
- Do not add a host. `nf-etl publish` already writes JSON indexes into `cmgd-public`. It also
  renders, with stdlib `string.Template`:
  - `index.html`: datasets, latest releases, DOIs, licence and attribution
  - `studies/<study>/index.html`: sample count, metadata coverage per source, file links, a
    DuckDB snippet
- The dashboard's Studies page links each processed study to its public page.
- R2 does not serve `/` → `/index.html` (`GET /` returns 404 today). The landing URLs are
  therefore `/index.html` and `/llms.txt`. A Cloudflare rewrite rule for `/` belongs in
  `monode/terraform/apps/cmgd` and comes later.

## 5. Reviewer-agent navigability

R2 has no anonymous LIST, so every file must be reachable from an index. The chain is:

```
/llms.txt                          plain text: what this is, licence, DOIs, URL patterns, 3 DuckDB snippets
  └─ /index.json                   datasets → latest.json (exists)
       └─ /<ds>/latest.json        → <release>/manifest.json (exists)
            ├─ tables/<t>/files.json → parquet URLs (exists)
            ├─ studies/index.json  study → readset_ids, file URLs, sizes, sha256 (exists; gains biosample + metadata links)
            └─ metadata/…          (new, or tables under D1)
```

Rules that make it work:

- **Stable URLs.** A release path never changes. `latest.json` is the only mutable pointer.
- **URL patterns are written out in `llms.txt`**, so an agent can construct URLs without
  walking the chain: `https://cmgd-public.cancerdatasci.org/{dataset}/{release}/tables/{table}/data/part-00000.parquet`.
- **Every Parquet table is sorted by `(study, sample, feature)` (ADR-0011).** DuckDB
  `read_parquet('https://…')` with a `WHERE` on study or sample therefore reads only the row
  groups it needs.
- **`llms.txt` lists the join keys** (`readset_id`, `biosample`, `run_accession`, `bioproject`)
  and the table that carries each.

**Acceptance test (Phase 5 exit).** A fresh agent with only `WebFetch` (or `curl`) and
`duckdb` gets this prompt and nothing else:

> Starting from https://cmgd-public.cancerdatasci.org/llms.txt, for BioSample `<a NishiwakiH_2024 BioSample>` report (1) its top 5 MetaPhlAn species with relative abundance, (2) its `sex` and `age` from each metadata source with source and release, (3) the Zenodo DOI of the release you used.

Pass condition:

- all three answers are correct (checked against the lake)
- in **≤ 4 fetches**, where one DuckDB query counts as one fetch
- with no guessing of unlisted URLs

Run the test with three different samples, one of them from the ~17 HoylesL_2018 BioSamples that are in Olm but not in cMD.
That sample must come back as "no cMD record", not as an error.

Script it as `scripts/smoke_reviewer_agent.md` (the prompt and expected answers) next to the
existing `scripts/smoke_public_data.py`.

## 6. Phases

The MVP is Phases 1–5. Effort is working days for one person plus an agent. Cluster time is
separate.

### Phase 1: Metadata store and Olm ingest

- **Deliverables:**
  - D1 resolved
  - `<meta>` schema
  - `nf-meta load olm` (one module, `src/nextflow_telemetry/meta/`, CLI entry in
    `pyproject.toml` next to `nf-etl`)
  - `source_releases`, `olm_*` tables for R2026.25
- **Reuses:** `etl/lake.py` attach and secret preamble; `cdsci.lake` MERGE and commit-message
  helpers; the DUCKLAKE.md contract.
- **New code:** one loader of about 200 lines (download, sha256 verify, MERGE per table);
  one unpivot view.
- **Acceptance:**
  - `select count(*) from <meta>.olm_runs where release='R2026.25'` = 718,658
  - wide-table rows = 602,163
  - a second load is a no-op (0 rows changed)
  - the determinations join is 1:1
- **Effort:** 2 days.

### Phase 2: cMD source, xref, harmonized view, comparison

- **Deliverables:**
  - `nf-meta load cmd`
  - `field_map.csv` (6 fields)
  - `sample_xref`
  - `sample_values`, `sample_harmonized` and `compare_cmd_olm` views
  - comparison table for the 4 MVP studies posted on the issue
  - new ADR-0012 "multi-source sample metadata in the lake", amending ADR-0009's "catalog
    owns curated annotations" for curated *values*
- **Reuses:** cMD TSV fetch logic from `nf-client add-cmd`; v2 `GET /samples` for xref.
- **New code:** loader of about 100 lines; three SQL views; a seed CSV.
- **Acceptance:**
  - every MVP readset has an xref row
  - `compare_cmd_olm` returns rows for all 4 studies × 6 fields
  - with `external.cmd` included, agreement on Olm R2 rows is ≥ 99%. This sanity-checks the
    join: those values are cMD's own.
- **Effort:** 3 days.

### Phase 3: Onboarding path working against v2, with estimates; run the MVP cohort

- **Deliverables:**
  - add-study trigger fix (#196 item 1)
  - v1 submission write-through to v2 (#195 item 4)
  - `estimate` in the dry-run plus the preview comment
  - `nf-meta candidates`
  - lwaldron #188 and #186 processed through the issue flow
  - PernigoniN_2021 and HoylesL_2018 filed from `nf-meta candidates` output and processed the
    same way
  - `cmgd_mpa4.2 2.3.0` scope extended
  - reconcile
  - 470 samples dispatched
- **Reuses:** `services/submission.py`, `add_to_collection`, `add-study.yml`,
  `format_study_preview.py`, v2 `/samples`, `/workflows`, `/admin/reconcile-jobs`, daemons on
  Alpine and Anvil (Bridges-2 once 2.3.2 lands).
- **New code:**
  - about 40 lines of write-through
  - about 30 lines of estimate
  - 2 ENA fields
  - one candidate query
  - a workflow `if:` change
- **Acceptance:**
  - each of the 4 issues carries a preview comment with an estimate and a `submission_id`
  - v2 `GET /cohorts` lists the 4 collections with 470 samples total
  - ≥ 95% of jobs reach `completed` (DLQ triaged with `cmgd-triage`)
  - refit the SU model on these runs and record measured core-h/sample vs the 24 estimate
    on the issue
- **Effort:** 3–4 days, plus about 1 week of cluster time.

### Phase 4: Stage and release with metadata and provenance

- **Deliverables:**
  - `nf-etl ingest` for the MVP samples
  - `nf-etl publish --registration cmgd_mpa4.2/2.3.0` with the `samples` table, metadata
    tables, the `cmgd` provenance block, `llms.txt`, `index.html` and per-study pages
  - synced to `cmgd-public`
  - ADR-0011 amended for per-table licence and the new artifacts
- **Reuses:** all of `etl/`; `publish_release` verify/promote; `export.py` per-study artifacts.
- **New code:** about 150 lines in `publish.py`/`export.py` (three extra tables, one provenance
  block, two templates).
- **Acceptance:**
  - `verify_release` passes
  - `scripts/smoke_public_data.py` passes against the new dataset
  - `duckdb -c "select count(distinct readset_id) from read_parquet('https://…/samples/…')"`
    = number of completed MVP samples
- **Effort:** 3 days.

### Phase 5: Mirrors and reviewer-agent acceptance

- **Deliverables:**
  - `nf-etl mirror --to zenodo|hf`
  - the two GSM secrets
  - first Zenodo version (DOI) and HF repo
  - DOI written back to `index.json`/`llms.txt`
  - Olm citation confirmed with the authors
  - reviewer-agent test passing
- **Reuses:** release directory and manifest as the upload set; `huggingface_hub`
  `upload_folder`; Zenodo REST deposit API.
- **New code:** about 150 lines (two upload functions and a dataset-card template).
- **Acceptance:**
  - the Zenodo record's file checksums equal the manifest's
  - `datasets.load_dataset("<repo>", "samples")` returns the MVP rows
  - the reviewer-agent test passes 3 of 3 within 4 fetches
- **Effort:** 3 days.

**MVP total:** about 14–15 working days, plus cluster time.

### Later phases (explicitly out of the MVP)

| Phase | What | Trigger to start |
|---|---|---|
| L1 | Priority on jobs/collections in v2 `claimBatch`, plus a budget ledger (approved SU vs consumed SU per allocation) | More than one requester competing for one allocation |
| L2 | Olm-driven bulk selection at scale: ~60k-sample budget, ranked selection, batched issues or a single "bulk" submission | MVP done and refit SU model in hand |
| L3 | Scheduled Olm re-ingest (systemd timer, ntfy), with a diff report between pinned and latest | Second manual pin bump |
| L4 | Sandpiper/GTDB profile comparison on shared runs (`singlem-sandpiper.md` item 3) | MVP release exists |
| L5 | Disease vocabulary harmonization, more fields, LLM-assisted mapping (`sample-metadata-design.md`) | Comparison shows which fields matter |
| L6 | Pipeline `manifest.json` gains `databases{}` (Kraken, CARD, MetaPhlAn index, HUMAnN bundle DBs) | Next pipeline minor version |
| L7 | Remaining pMD studies (#185, #187, #189, #190) | Phase 3 path proven. Can start right after Phase 3 |
| L8 | `/` → `/index.html` rewrite rule in monode terraform; Zenodo/HF on every release via the publish timer | After two manual mirrors |

## 7. Open questions and risks

| Risk / question | Detail | Mitigation |
|---|---|---|
| Zenodo size limits | 50 GB and 100 files per record by default (per Zenodo docs; verify at implementation). `marker_abundance` + `marker_presence` are 86% of bytes, ~830 KB/sample (`docs/data-volumes.md`). They cross 50 GB near 60k samples | MVP is ~0.5 GB. At scale, deposit everything except markers. Markers stay on `cmgd-public` and the HF repo. Request a quota increase if needed |
| HF limits | Per-file and repo-size guidance applies (large repos may need HF contact; verify at implementation). The viewer needs Parquet configs in the card | Same Parquet as the release, one file per table. Split by study once a file passes 5 GB |
| Olm licensing | CC-BY-4.0 data; evidence quotes are verbatim third-party text; their citation is a placeholder | Per-table licence, no quotes, attribution string, confirm citation (Phase 5 gate) |
| Olm churn | ~2.5 releases/week; rows retire (148 of 2,946 studies retired in 1.20.0) | Pin a release; keep old releases; `release_retired IS NULL` filter in views; bump the pin by hand |
| Olm already includes cMD | Comparison circularity (D3) | Filter `external.cmd`; report both ways |
| Cost model disagreement | 24 core-h (rehearsal) vs ~11.5 SU/sample (2.2.x model) | Budget on 24; refit in Phase 3 |
| Scratch on Bridges-2 | 50 GB/sample peak; B2 is disk-limited (#244) | Lower `batch_size` × concurrency on B2; ranking prefers low-Gbp studies there |
| Write-through partial failure | v1 commits, v2 POST fails | Re-running `submit-study` is idempotent on both sides (v2 upserts samples; v1 records a second submission). Log and retry. No distributed transaction |
| HoylesL_2018 sample-set mismatch | 73 BioSamples in ENA/Olm vs 56 in cMD | The xref and the comparison handle it. Decide whether all 73 should be processed (default: all 73, since the registration is by BioProject) |
| cmgd_lake vs shared lake | D1 | Decide before Phase 1 |
| Readset/BioSample many-to-many | Olm notes 62 BioSamples in two BioProjects; pooled samples have `;`-joined BioSamples | `sample_xref` is one row per run, so many-to-many falls out; the harmonized view picks per readset |

## Appendix A: queries behind the numbers

Run with `uv run --with duckdb python -I <script> <olm_dir> …` from a directory outside the
downloaded package. `cmd` is `cMD_curated_metadata_release.csv` (curation repo, 2026-06-30);
`st` is `inst/extdata/studies_status.csv` (fetched 2026-10-15).

```sql
-- cMD samples present in Olm
create table cmdrun as select study_name, sample_id, body_site,
  trim(unnest(string_split(ncbi_accession,';'))) run from cmd;
select body_site='feces', count(distinct sample_id),
       count(distinct sample_id) filter (where g.sample_key is not null)
from cmdrun r left join 'gut_runs.parquet' g on g.run_accession = r.run group by 1;

-- Olm values sourced from cMD
select route, evidence_source, count(*) from 'gut_sample_determinations.parquet'
where evidence_source = 'external.cmd' group by all;

-- per-study size
select study_accession, count(distinct sample_key), sum(base_count::double)/1e9 gbp,
       sum(read_count::double)/1e6 mspots
from 'gut_runs.parquet' where sample_key is not null group by 1;

-- corpus
with s as (select sample_key, sum(base_count::double)/1e9 gbp from 'gut_runs.parquet'
  where sample_key is not null and library_strategy='WGS'
    and instrument_platform in ('ILLUMINA','BGISEQ','DNBSEQ') group by 1)
select count(*), sum(gbp), median(gbp), quantile_cont(gbp,.9) from s;
```
