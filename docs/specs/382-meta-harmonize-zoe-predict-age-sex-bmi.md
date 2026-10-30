# Spec #382: harmonize ZOE PREDICT age, sex and BMI

Issue: [#382](https://github.com/seandavi/nextflow_telemetry/issues/382). No parent
issue. It is not part of the #381 control-plane migration (ADR-0013); nothing here
touches the API, the Worker in `cf/`, Postgres tables or Alembic.

## Goal

`nf-meta views` produces harmonized `age_years`, `sex` and `bmi` from
`lake.cmgd_meta.zoe_sample_values` for every ZOE sample that `zoe_sample_link` resolved
to ENA runs. ZOE becomes a third source in `sample_values` and ranks second in
`sample_harmonized` (cmd > zoe > olm R1/R2 > olm R3/R4). ZOE's `F`/`M` sex codes
become `female`/`male`. Implausible BMI values stay in the output and carry a flag.

## Non-goals

- No change to `load_xref` / `sample_xref`. The readsets in scope are already
  there: P3 (and P2) are registered (#324), so the `v2` origin covers them, and P1 is
  covered by cMD's `AsnicarF_2021`, so the `cmd` origin covers it. A `zoe` origin
  for unregistered predicted readsets can be added if a ZOE cohort is ever unregistered.
- No P2 linkage. P2 stays `unresolved_no_key` (no public key, exploration README).
- No `compare_cmd_zoe` view, no per-source `sample_metadata_zoe` publish table, no
  `value_flag` column in published tables, no ZOE line in `etl.publish.CITATION`.
  See Risks for what the next publish carries.
- No `--pin` flag on `nf-meta load zoe`. The first ZOE release pins itself
  (`_record_release`'s existing rule), and only one release exists. Add the flag when a
  second Zenodo version is loaded.
- No Alembic migration, endpoint, config or env change.

## Departures from the issue and the design record

1. **Link status.** The issue says to join on `link_status = linked`. No such value
   exists. `zoe_link.py` writes `resolved`, `not_in_ena`, `ambiguous` and
   `unresolved_no_key`. This spec joins on `link_status = 'resolved'`. `ambiguous`
   rows (several BioSamples) don't count.
2. **ADR-0012, decision 4.** The ADR fixes the precedence at cmd > olm R1/R2 > olm
   R3/R4 and covers two sources. This spec adds ZOE in second place and adds a value
   flag. Record it as an amendment section in ADR-0012, following the
   "Amendment (date): ..." pattern in ADR-0011. Don't rewrite the original text.
3. **ADR-0012, decision 2 (pinning).** `load_zoe` currently writes no
   `source_releases` row, so the pinned-only views can't see ZOE. `load_zoe` will
   record one like `load_olm`/`load_cmd` do. A ZOE release is six files, so the
   row's `sha256` is a digest of the six snapshot digests (defined below), not the
   hash of one file.

## Design

### Files changed

| File | Change |
|---|---|
| `src/nextflow_telemetry/meta/field_map.csv` | add 3 `zoe` rows |
| `src/nextflow_telemetry/meta/store.py` | `sex` transform; `zoe` branch in `sample_values`; `value_flag` column; ZOE rank in `sample_harmonized`; module docstring |
| `src/nextflow_telemetry/meta/zoe.py` | `load_zoe` records the `source_releases` row; `DOI`, `URL` constants |
| `src/nextflow_telemetry/meta/cli.py` | docstring: `load zoe` records and pins the release; `views` needs `load zoe` and `link zoe` |
| `docs/adr/0012-multi-source-sample-metadata-in-the-lake.md` | amendment section |
| `tests/test_meta.py` | shared schema helper with empty ZOE tables; new ZOE views test |
| `tests/test_meta_zoe.py` | assert the `source_releases` row |

No new files besides this spec. No new dependencies: DuckDB 1.5.6 (installed) has
`string_split`, `list_filter`, `TRY_CAST` and `QUALIFY`, and `store.py` already uses
all of them. The digest uses stdlib `hashlib`.

### `field_map.csv`

Append (the column order is `source,source_field,field,unit_transform`):

```
zoe,age,age_years,to_years
zoe,sex,sex,sex
zoe,bmi,bmi,round2
```

ZOE rows carry `value_unit = 'year'` for age, so `to_years` divides by 1 and rounds
(`43` → `43.0`). The set of harmonized fields doesn't change, so
`compare_cmd_olm`'s grid and the field-set assertions in `test_meta.py` and
`test_cmd_schema.py` still hold.

### `store.py`

**`TRANSFORM`**: add one branch:

```sql
WHEN 'sex' THEN CASE lower(trim(v.value_raw)) WHEN 'f' THEN 'female' WHEN 'm' THEN 'male'
    ELSE lower(trim(v.value_raw)) END
```

Only the `zoe` row uses it. cmd and olm keep `lower`.

**Constant**: `BMI_PLAUSIBLE = (12.0, 70.0)`, the bounds in
`docs/research/metadata-sources/README.md` ("under 12 or over 70").

**`views_sql()["sample_values"]`**:

- Add a third branch to the `v` CTE, combined with `UNION ALL BY NAME`:

  ```sql
  SELECT z.source, z.release, z.bioproject AS study, z.source_sample AS sample,
         l.biosample,
         list_filter(string_split(l.run_accessions, ';'), x -> x <> '') AS run_accessions,
         z.field AS source_field, z.value_raw, z.value_unit,
         NULL::VARCHAR AS value_ontology, NULL::DOUBLE AS confidence, 'curated' AS route,
         NULL::VARCHAR AS evidence_source, NULL::VARCHAR AS determined_by,
         NULL::VARCHAR AS curator
  FROM {s}.zoe_sample_values z
  LEFT JOIN {s}.zoe_sample_link l ON l.release = z.release AND l.cohort = z.cohort
       AND l.source_sample = z.source_sample AND l.link_status = 'resolved'
  ```

  Every ZOE row appears, every release, like the other sources. Rows without a
  resolved link have `biosample` and `run_accessions` NULL, so `sample_harmonized`
  can't reach them. `route = 'curated'` is the spelling cMD rows already use for
  values that weren't machine-extracted.
- Add a final column `value_flag VARCHAR`:
  `'implausible_bmi'` when the harmonized `field = 'bmi'` and
  `TRY_CAST(value AS DOUBLE)` is `< 12` or `> 70`, else NULL. It applies to every
  source's BMI, not only ZOE's, and is computed on the transformed `value`. Wrap the
  existing SELECT in a subquery or repeat the expression, whichever reads better.
  The column order is unchanged with `value_flag` appended last.

**`views_sql()["sample_harmonized"]`**:

- Rank: `cmd` 1, `zoe` 2, olm R1/R2 (not `external.cmd`) 3, olm R3/R4 with
  confidence ≥ 0.5 (not `external.cmd`) 4. Update the comment above the view.
- Output columns: `readset_id, field, value, source, release, route, confidence,
  value_flag`. `value_flag` is appended last. A flagged value still competes at its
  rank. It is kept, not dropped or demoted (issue: "keep … and flag them, don't
  drop them").

**Module docstring**: the `create_views` line names the ZOE source.
`compare_cmd_olm` doesn't change.

### `zoe.py`

- Constants: `DOI = "10.5281/zenodo.15308000"` and
  `URL = "https://zenodo.org/records/15308000"` (matching
  `examples/source-catalog/zoe.json`). Add `# ponytail:` on them: they describe
  `VERSION` and need changing together with it.
- `load_zoe(con, db_uri, version=VERSION) -> dict[str, int]` keeps its signature and
  return value. After the upsert, it calls

  ```python
  _record_release(con, "zoe", version, package_version=None, doi=DOI, license="CC-BY-4.0",
                  url=URL, sha256=hashlib.sha256("\n".join(sorted(
                      rc["sha256"] for rc in receipts.values())).encode()).hexdigest(),
                  pin=False)
  ```

  It imports `_record_release` from `.store`, next to the existing `S` and `_q`
  imports. The first ZOE release is pinned by the existing rule. A repeat load writes
  no new row: `_record_release` upserts on `(source, release)` with `loaded_at`
  excluded from change detection.
- Docstring: one line saying that the release row's `sha256` is the SHA-256 of the
  sorted, newline-joined snapshot digests.

### `cli.py`

Docstring only. Update `load zoe` to "ZOE open metadata from catalog snapshots;
records (first time: pins) the release", and add `load zoe` and `link zoe` to the
`views` prerequisites. No new arguments.

### ADR-0012 amendment

Add `## Amendment (2026-10-30): ZOE PREDICT as a third source (#382)`:

- ZOE values (`zoe_sample_values`) reach readsets through `zoe_sample_link` rows with
  `link_status = 'resolved'` only.
- `load zoe` records a `source_releases` row. Its `sha256` is the digest of the six
  snapshot digests.
- Precedence is cmd > zoe > olm R1/R2 > olm R3/R4 (≥ 0.5). ZOE is first-party data
  for its cohorts, and cMD's `AsnicarF_2021` already curates P1.
- `sample_values` and `sample_harmonized` carry `value_flag`
  (`implausible_bmi`: BMI < 12 or > 70). Flagged values are kept.
- Load order: `load olm`, `load cmd`, `load zoe`, `link zoe`, then `load xref`, then
  `views`.

Also add the amendment note to the ADR-0012 row in `docs/adr/README.md` (the
`(amended …)` form that 0006 uses).

### Callers affected

- **etl publish** (`etl/publish.py`): no code change. `sample_metadata_harmonized`
  selects named columns, so `value_flag` isn't published. On the next publish, ZOE
  values appear in that table with `source = 'zoe'`, and the ZOE `source_releases` row
  (licence, DOI) appears in `metadata_sources`. `_source_rows` filters by source, so
  the cmd/olm tables don't change. See Risks.
- **etl deliver**: none (it reads only cmd rows).
- **nf-client, frontend, health report, API, Worker**: none.

### Rollout (operator, after merge; not part of the implementation)

`nf-meta load zoe` (an idempotent value MERGE, then the new `source_releases` row,
pinned), then `nf-meta views`. `link zoe` and `load xref` are already current.

## Acceptance criteria

- [ ] `field_map.csv` has the three `zoe` rows above, and the set of harmonized
      fields is unchanged.
- [ ] In `sample_values`, a ZOE `sex` of `F`/`M` becomes `female`/`male`, and
      `female`/`male` passes through. ZOE `age` `43` becomes `age_years` `43.0`, and
      ZOE `bmi` is rounded to two decimals.
- [ ] Every `zoe_sample_values` row appears in `sample_values` with
      `source = 'zoe'`. Rows whose link is not `resolved` have NULL
      `run_accessions` and `biosample`.
- [ ] `sample_harmonized` gives `age_years`, `sex` and `bmi` from ZOE for a readset
      whose runs come only from a resolved ZOE link (pinned release).
- [ ] cmd beats zoe, and zoe beats olm R1/R2 for the same readset and field.
- [ ] `ambiguous`, `not_in_ena` and `unresolved_no_key` ZOE samples produce no
      `sample_harmonized` rows.
- [ ] A ZOE BMI of `107.9` is in `sample_harmonized` with
      `value_flag = 'implausible_bmi'`. A BMI of `22.0` has `value_flag` NULL. Values
      12 and 70 are not flagged.
- [ ] Existing assertions in `test_views_unpivot_harmonize_and_compare` still pass
      unchanged.
- [ ] `load_zoe` writes one `source_releases` row
      `(source='zoe', release=VERSION, license='CC-BY-4.0', doi=DOI, url=URL,
      pinned=true)` whose `sha256` equals the digest of the sorted snapshot digests,
      and a second load leaves exactly one row.
- [ ] ADR-0012 has the amendment section, and `docs/adr/README.md` marks it amended.
- [ ] `uv run pytest tests/test_meta.py tests/test_meta_zoe.py tests/test_meta_zoe_link.py tests/test_cmd_schema.py tests/test_etl_publish_metadata.py`
      and `uv run mypy src/nextflow_telemetry` pass.

## Test plan

**`tests/test_meta.py`**

- Move the schema setup of `test_views_unpivot_harmonize_and_compare` (everything
  before the `views_sql` loop) into a module helper `_base_schema(con)`. The helper
  also creates empty `m.zoe_sample_values` (the `zoe.COLUMNS` columns, all VARCHAR)
  and `m.zoe_sample_link` (the `zoe_link.COLUMNS` columns, all VARCHAR). The
  existing test calls the helper and keeps every assertion.
- New `test_views_harmonize_zoe_precedence_sex_and_bmi_flag`: `_base_schema(con)`,
  then:
  - `source_releases` += `('zoe', 'Z1', true)`.
  - ZOE values, release `Z1`:
    - `P3_a`: age 50, sex `female`, bmi `107.9`; link `resolved`, runs `SRR1` (the
      existing readset `RS.x`, which has cmd age/sex).
    - `P1_b`: age 43, sex `M`, bmi `22.0`; link `resolved`, runs `ERR9`.
    - `predict2_1`: sex `F`; link `unresolved_no_key`.
    - `P3_c`: sex `male`; link `ambiguous`, runs `ERR7;ERR8`.
  - `sample_xref` += `RS.z`/`ERR9` and `RS.amb`/`ERR7`.
  - `olm_sample_values` += one Olm R1 row for `ERR9`: `sex = 'female'`, confidence
    0.9, evidence_source `biosample`.
  - Create the views, then assert:
    - `RS.x`: `age_years` and `sex` from `cmd`; `bmi` from `zoe`, `107.9`, flag
      `implausible_bmi`.
    - `RS.z`: `age_years` `43.0`, `sex` `male` (zoe over Olm's `female`), `bmi`
      `22.0`, all `source = 'zoe'`, `release = 'Z1'`, flag NULL.
    - No `RS.amb` row.
    - `sample_values` for `predict2_1` has `value = 'female'` and NULL
      `run_accessions`.
  - A short DuckDB check of the flag bounds: BMI values `12` and `70` are unflagged,
    `11.9` and `70.1` are flagged. The check can insert extra resolved ZOE samples, or
    select `value_flag` from `sample_values` for them.

**`tests/test_meta_zoe.py`**: in `test_load_reads_only_retained_bytes_and_is_idempotent`,
after the two loads, select from `{store.S}.source_releases WHERE source = 'zoe'`.
Assert one row with the fields above, `pinned` true, and `sha256` equal to the digest
of the six `snapshot_sha256` values in `zoe_sample_values`.

Commands: the pytest line in the acceptance criteria (the `db_url` tests need the
disposable Postgres fixture that `tests/conftest.py` provides) and
`uv run mypy src/nextflow_telemetry`.

Manual check after rollout, which the implementer doesn't run against production:
`SELECT field, count(*) FROM lake.cmgd_meta.sample_harmonized WHERE source = 'zoe' GROUP BY 1`
should show roughly 35k readsets for `age_years` and `sex`.

## Risks

- **Attribution on the next publish.** `sample_metadata_harmonized` (already
  CC-BY-4.0) will carry ZOE-derived values. The release's `metadata_sources` table
  lists the ZOE release with its licence and DOI, but `etl.publish.CITATION`, the
  release README and `methods.py` name only cMD and Olm. Follow-up: add a ZOE citation
  before the next `nf-etl publish`. That is out of scope here because it changes
  publication text.
- **Views now need the ZOE tables.** `nf-meta views` fails if `zoe_sample_values` or
  `zoe_sample_link` doesn't exist, as it already does for cmd and olm. Both exist in
  production (README table 1).
- **The digest-of-digests `sha256`** isn't the hash of any single file. It is
  documented in the ADR amendment and the `zoe.py` docstring.
- **`DOI`/`URL` are tied to `VERSION`.** Loading another Zenodo version needs those
  constants updated together.
- **Flagged BMI still wins at its rank.** A ZOE value of 107.9 outranks a plausible
  Olm value for the same readset. This follows the issue ("keep and flag"). The flag
  lets consumers filter.
