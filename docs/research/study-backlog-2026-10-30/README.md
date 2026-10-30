# Study backlog and the 2026-10-30 registration batch

Which studies in the [source catalog](../../source-catalog.md) were not yet registered in the
v2 control plane, and what was registered on 2026-10-30. Everything was compared by **run
accession** against the live v2 registry (`GET /api/samples`), not by collection name.

## Sources used

| Source (catalog id) | Snapshot | Used for |
|---|---|---|
| `cmd-curation` | commit `6828fb9` | 152 curated studies; per-sample run accessions |
| `segata-study-tracker` (restricted) | `export-2026-10-19T022530Z` | 235-row study inventory (aggregates only here) |
| `asap-parkinsons-inventory` | commit `159bfff` | 22-row Parkinson's shotgun inventory |
| `ena-portal-zoe` | `ena-2026-10-19` | ZOE PREDICT read-run listings |
| `olm-microbiome-repo`, `asap-parkinsons-curation` | | not compared (candidate pool / metadata only) |

For tracker and ASAP rows, accessions were expanded to runs with the ENA Portal
`filereport` API on 2026-10-30. A study counted as registered when at least 95% of its
non-amplicon runs were in v2.

## Before the batch

v2 had 95 collections, 13,460 samples and 18,323 runs.

- **cMD curation:** 30 of 152 studies were fully registered. 11 were partial, mostly 1-4 sample
  test stubs (`DavidLA_2015`, `HallAB_2017`, `XieH_2016`, `ZellerG_2014`, ...). 96 were not
  registered at all, and 15 have no run accessions in their curation file.
- **Registered in v1 only:** five BioProjects approved through the `add-study` GitHub bot were
  never registered in v2: PRJNA834801, PRJNA1099939, PRJEB53401, PRJEB17784 and PRJNA1043247.
  The cause is that the repository variable `NF_TELEMETRY_URL` still points at the v1 API
  (`https://nf-telemetry.cancerdatasci.org/api`). **That bot still registers into v1.**
  All five are covered by cMD studies (`WallenZD_2022`, `SampsonTR_2025`, `BoktorJC_2023`,
  `BedarfJR_2017`, `ParkDG_2024`), so they were registered under those names.
- **ZOE PREDICT:** none registered. P1 is cMD's `AsnicarF_2021`.

## The 2026-10-30 batch

[`registered_2026-10-30.csv`](registered_2026-10-30.csv) lists the 109 collections and
58,863 rows (58,699 new samples; the rest were already present and only gained membership).

| Group | Collections | Samples |
|---|---:|---:|
| cMD-curated studies, unregistered or partial (collection = study name) | 104 | 23,021 |
| ZOE PREDICT 2 and 3 (collection = BioProject) | 5 | 35,842 |

- **cMD rows** were taken from the catalog snapshot, not from GitHub `master`. Placeholder
  accessions (`Not applicable`) were dropped.
- **ZOE rows** come from the cataloged ENA listings. They include WGS runs only, grouped per
  BioSample, with the BioSample stored. P1's 1,098 amplicon runs were left out because P1 is
  registered through cMD.
- **Registration and dispatch:** samples were loaded with `nf-client add-samples` against v2.
  The collections were then added to **cmgd_mpa4.2 2.3.0 only** (scope went from 48 to 150
  collections). The humann registration was not changed. `reconcile-jobs` created 58,813
  mpa4.2 jobs.
- **Checks:** v2 total after the batch was exactly 13,460 + 58,699 = 72,159 samples. Every
  collection's count matched its TSV.
- [`build_tsvs.py`](build_tsvs.py) built the TSVs. It reads local copies of the snapshots
  and refuses run groupings that would conflict with already-registered samples (there were none).

## Still open

1. **cMD studies with no run accessions** (15, about 4.8k curated rows). Each needs a
   sample-to-run mapping first: `HMP_2019_ibdmdb`, `HMP_2019_t2d`, `LifeLinesDeep_2016`,
   `YachidaS_2019`, `VilaAV_2018`, `DeCastroFonsecaM_2026`, `ThomasAM_2019_a/b/c`,
   `TettAJ_2016`, `DumitrescuDG_2023`, `MoiseyenkoA_2026`, `SchonhoffA_2023`, `WindTT_2020`,
   `OhJ_2016`. See [`cmd_coverage.csv`](cmd_coverage.csv).
2. **Tracker/ASAP studies with no cMD curation:** 74 accession sets, about 44.6k
   non-amplicon runs, still unregistered.
   - Hosts: 69 human, 2 dog, 1 mouse, 2 unknown.
   - Part of this is BioProjects whose run count exceeds what cMD curated (for example
     HMP_2012 and MetaCardis). Read each study before registering.
   - The only ASAP one is `ChenS_2022` (PRJNA762484, 110 runs). The rest come from the
     restricted tracker; their per-study table is kept privately at
     `r2:cmgd-private/notes/study-backlog-2026-10-30/` and not in this public repo.
3. **Tracker rows with no INSDC accession** (53): EGA IDs, "upon request", unpublished.
   Also private, in the same R2 folder.
4. **Five PRJEB53401 runs** (`ERR9830085`, `ERR9830110`, `ERR9830162`, `ERR9830165`,
   `ERR9830171`) are not in cMD's `BoktorJC_2023`. Left unregistered; check why cMD excluded them.
5. **PRJNA1489422** (tracker) returns no runs from ENA. Try omicidx.
6. **Point the `add-study` bot at v2:** set `NF_TELEMETRY_URL` to the v2 API and the
   `NF_OPERATOR_TOKEN` secret to `cdsci-nf-telemetry-v2-api-token`. Check first that v2
   serves `/submissions`; the v2 Worker has no such route today.
7. **Olm Microbiome Repo** was not compared. Its query recipe is in
   [adding-studies](../../adding-studies.md).
