# Metadata source exploration

Findings from the first look at sources captured in the source catalog
([#333](https://github.com/seandavi/nextflow_telemetry/issues/333)). All analysis ran on bytes
retained in `cmgd-private` (receipts in `metadata_source_snapshots`), not on fresh downloads.
Scripts here are the ones that produced the numbers. They are exploration, not a framework.

Captured 2026-10-18; counts are as of those snapshots.

## ZOE PREDICT open metadata (zenodo 15308000 v1)

Columns are only `sampleID`, `age`, `sex`, `bmi`. No participant, visit, date, diet or lab fields,
so nothing says whether one person appears twice.

| Cohort file | Rows | `sampleID` is | Joins to ENA | Notes |
|---|---:|---|---|---|
| P1 | 1,098 | ENA BioSample accession | all 1,098 shotgun (WGS) BioSamples | PRJEB39223 also holds 1,098 AMPLICON samples with no metadata. Add-study preview must separate them. |
| P2 | 975 | `predict2_NNNNNN` | **no public key** | ENA aliases/titles are `15001…`; no overlap. A numeric offset is plausible but unverified: left unlinked. |
| P3_US21 | 11,798 | ENA `sample_alias` | 11,797 exact, 1:1, one run each | 1 metadata row has no ENA sample |
| P3_US22A | 8,470 | ENA `sample_alias` | 8,469 exact | 1 row has no ENA sample |
| P3_UK22A | 12,353 | ENA `sample_alias` | 12,353 exact | |
| P3_UK23ART | 2,248 | ENA `sample_alias` | 2,245 exact | 3 rows have no ENA sample |

- No `sampleID` appears in more than one file. Whether people overlap across cohorts is unknowable from this source.
- Sex is coded `F`/`M` in P1 and `female`/`male` elsewhere. Age is integer years (18-95).
- BMI has implausible values: 8.4 to 107.9 overall; 23 values under 12 or over 70 across P2 and P3. Keep raw; flag, don't drop.
- Missing: 1 BMI in P1, 2 in US21. Everything else is complete.

## Study tracker (restricted; aggregates only)

235 rows, 20 columns, mixed purposes:
- 5 blank-study rows and 3 summary rows (`... Total`, `Overall`). Summing the sample column over all rows gives
  138,947, which double-counts. These are not studies.
- 8 study names appear on more than one row (curation waves overlap, multi-arm studies).
- The accession column is free text: 178 clean INSDC accessions, 43 empty, 14 other (EGA IDs, annotated
  values like `X (paper says Y)`, `A,B`, prose such as "part of a manuscript in prep").
- `Host species` includes Mouse (8) and Dog (2); `Note` is overloaded (wave tag, URLs, names, tags).
- Sharing status is filled for only 18 of 235 rows.

## What this means for loaders and linkage

1. Loaders keep the source's own ID and say what kind of ID it is (`biosample`, `ena_sample_alias`, `source_local`).
   They don't need ENA or readsets.
2. Linkage is a separate step per ID scheme. P3 and P1 can be resolved deterministically from ENA. P2 needs
   the authors, or evidence, before it can be linked. Unresolved stays unresolved.
3. The tracker needs row typing (study / summary / blank) and an accession parser that reports what it couldn't
   parse, before anything counts samples from it.
4. Checks worth keeping in every loader: duplicate source IDs, rows without a link target, value-range flags.
