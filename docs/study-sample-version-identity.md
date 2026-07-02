# Study / Sample / Version identity — design positions

> Status: **draft v1** — Epic A of `docs/roadmap.md`. Settles the three identity
> decisions that everything downstream (correct completeness #116, the study
> leaderboard #119, sample metadata #55, the outputs catalog keying #57/#93)
> inherits. Companion to `docs/sample-metadata-design.md`, which deliberately drew
> its scope line at the harmonization boundary and left *study identity* unsettled —
> this doc fills exactly that gap. No migrations are written until the forks in
> "Open questions" are resolved.

## Why this doc exists

The orchestration layer works. The **identity layer underneath it is muddled**, and
the correctness/UX complaints in the backlog are downstream symptoms. Three questions
have never been answered in one place:

1. What *is* a study, and where does study membership live?
2. Is a sample in exactly one study, or many?
3. What does "complete" mean when a pipeline has many versions?

Today the code answers each of these inconsistently, in ways that silently disagree.

## The problem, concretely

There are **three independent representations of "what study a sample belongs to"**,
and no server code path keeps them in sync (verified by a full read of `services/`,
`routers/`, `scripts/`, and the frontend):

| # | Representation | Cardinality | Written by | Read by |
|---|---|---|---|---|
| (a) | `collections` + `collection_samples` (`db.py:109-128`) | **many-to-many** (join table) | *only* `scripts/seed_collection_from_cohort.py` (manual backfill) | Cohorts dashboard: `services/cohort.py`, `CohortsPage.tsx` |
| (b) | `curated_studies` + `curated_sample_annotations.study_name` (`db.py:249-274`) | **many-to-many** (`uq(sample_id, study_name)`) | `POST /api/curated/import` → `services/curated.py` | `/api/curated/*` only — **an island; nothing joins it to jobs/collections** |
| (c) | `samples.metadata_.cohort` — free-text JSONB scalar (`db.py:99`) | **one-per-sample** (a scalar key) | every seeding script + the register form (`seed_from_tsv.py:73`, `load_bioproject.py:91`, `SamplesPage.tsx:211`) | Samples page grouping (`SamplesPage.tsx:85-146`) |

Ways they silently disagree today:

- **Samples page vs Cohorts dashboard show different truths for the same study.**
  The Samples page groups by `metadata.cohort` (rep c); the Cohorts dashboard groups
  by `collections` (rep a). A newly-registered sample appears under its cohort on the
  Samples page *immediately* but is **invisible on the Cohorts dashboard until someone
  manually runs `seed_collection_from_cohort.py --commit`.** Add samples to an existing
  cohort later and the Cohorts dashboard silently undercounts.
- **The same study lives under two different keys.** `seed_from_tsv.py:61` reads the
  TSV's `study_name` column and stores it under `metadata.cohort`; the *same* TSV
  imported through `/api/curated/import` keys everything by `study_name` in
  `curated_sample_annotations` and never touches `metadata.cohort`. Same identity, two
  spellings, no link.
- **`load_bioproject.py` writes `cohort` *and* `sra_study` *and* `bioproject` into one
  JSONB blob** (`load_bioproject.py:91-97`), any of which can disagree — and
  `collections.source` adds a fourth notion (`bioproject|sra_study|manual`).

## The three load-bearing decisions

### Decision 1 — Study is first-class, and there is exactly ONE source of truth for membership

Promote study to a real entity with a single membership table. **Rep (c)
`metadata.cohort` is retired as a source of truth** (see Decision 2 for why it *must*
be). Rep (a) `collections`/`collection_samples` is the survivor and becomes the
canonical study entity. Rep (b) `curated_studies` is reconciled into it (fork below).

### Decision 2 — Sample↔study is many-to-many (this is the decisive one)

A `sample_id` is **content-addressed** — the md5 of its sorted, deduplicated SRR set
(`db.py:88-91`). The identity is the *reads*, not the study context. The same physical
SRR set legitimately recurs across contexts:

- a BioProject, later re-analyzed as a curated cMD study;
- the same run accessioned into two publications / meta-analyses;
- a sample that is simultaneously in an `sra_study` collection and a hand-built
  `manual` cohort.

Therefore **sample↔study is many-to-many**, and this is not a corner case — it is the
normal shape once curated overlays sit on top of a raw SRA substrate (the exact
"minority overlay on a 10× raw substrate" framing of `sample-metadata-design.md`).

This decision **kills rep (c) structurally, not just for tidiness**:
`samples.metadata_.cohort` is a single scalar — it *cannot* express a sample in two
studies. The join tables (a) and (b) already can. A scalar cohort field forces a lie
whenever the true membership is >1. So membership **must** live in the join table, and
`metadata.cohort` cannot be the source of truth.

Consequence for completeness: "study X is N% complete" counts **distinct samples in
X** (via the join table), and a sample completing counts toward *every* study it
belongs to. That is correct and unambiguous under m2m; a scalar cohort would
double-count or drop.

### Decision 3 — Completion is measured in SAMPLES, under the ACTIVE version

Two bugs in one (this is #116):

1. **Counted in job rows across all versions.** `services/cohort.py::summary()` counts
   `jobs` grouped by status with the workflow filter *optional and defaulted off*
   (`cohort.py:75-104`) — so the denominator is `samples × all-ever-registered-versions`.
   `ArtachoA_2021` reads **0.5% = 3/552** where 552 = 69 samples × 8 versions. Retiring
   a workflow does **not** delete its `jobs` rows (`reconcile.py` only *creates* jobs
   for `status='active'`; nothing removes them on retire), so completed work under old
   versions pollutes both numerator and denominator forever.
2. **Expressed in the wrong unit.** Operators think in *samples* ("8,200 / 10,000
   done"), not job rows.

**Position:** define per-study completion as

```
completion = (distinct samples in study with a COMPLETED job
              under the workflow's ACTIVE version)
             ÷ (distinct samples in study)
```

"All workflows" becomes an explicit opt-in, never the default denominator. This is a
read-path fix (the *write* path is already version-correct: `MARK_COMPLETE` matches on
`run_name`, which belongs to exactly one version — `telemetry.py:160-175`).

## Proposed model

Minimal schema change; mostly a consolidation plus one rule.

### Samples — unchanged
Content-addressed `sample_id` stays the identity and the universal join key. No study
context on the sample row. (SRA-derivable columns from `sample-metadata-design.md` are
orthogonal and can land independently.)

### Study — one entity, one membership table
Generalize `collections` into the canonical study entity (keep the table name to avoid
churn, or rename to `studies` — cosmetic). `collection_samples` is the **single source
of truth for membership** (many-to-many). `source` distinguishes origin
(`bioproject|sra_study|cmd|manual`). Every path that today writes `metadata.cohort`
instead upserts a `collections` row + `collection_samples` membership **server-side at
registration** — closing the "invisible until backfill" gap by construction.

**A curated cMD study is not a separate entity (resolved, was Fork 1).** It is a
`collections` row with `source='cmd'`. `curated_sample_annotations` is reconceived as
annotation *rows attached to that study* (FK to the study), not a parallel study table
with its own `study_name` identity. There is exactly one study identity system-wide.
The harmonization boundary of `sample-metadata-design.md` is preserved at the level
that matters: study *identity* is operational and lives here; the *annotation content*
(controlled-vocab facets, provenance, publications) remains harmonization's problem and
does not move into the telemetry DB. `curated_studies.study_name` becomes the study's
`label`/natural key on the unified row; annotation rows join to the study by its id.

### `metadata.cohort` — demoted
Retired as source of truth. Either dropped, or kept *only* as a denormalized display
cache that is derived from membership (never written by clients). The Samples page
grouping moves onto the membership join, matching the Cohorts dashboard.

### Version / completion — one rule + version-scoped reads
- **Rule (resolved, was Fork 2): exactly one `active` version per `workflow_id`**,
  enforced by a partial unique index (`WHERE status='active'`). Promoting a new version
  to active auto-retires the prior active version in the same transaction. This makes
  "the active version" a single unambiguous row, so Decision 3's completion query is
  well-defined by construction. `reconcile.py`'s cross-join over active workflows then
  yields at most one active version per `workflow_id`, as intended.
- Rewrite the aggregate read paths (`cohort.py::summary`, `admin.py::stats`) to join
  `workflows` and scope to the active version, counting **distinct samples**. The
  per-workflow path (`workflows.py::job_summary`) is already version-scoped via
  `workflow_pk` and needs no change.

## Migration plan (sketch — not yet written)

Non-destructive, staged; each step independently shippable and reversible:

1. **Server-side membership on registration.** `POST /api/samples` (and the seeding
   scripts) upsert `collections` + `collection_samples` from the supplied study, in the
   same transaction as the sample. `metadata.cohort` still written (dual-write) so
   nothing breaks.
2. **Backfill.** Fold `scripts/seed_collection_from_cohort.py` logic into a one-shot
   admin endpoint; reconcile existing `metadata.cohort` and `curated_sample_annotations`
   into `collections`/`collection_samples`.
3. **Move reads to membership.** Samples page + Cohorts dashboard read the join;
   completion queries scoped to active version, counted in distinct samples.
4. **Enforce single-active-version:** add the partial unique index and the
   promote-auto-retires-prior transaction (Decision resolved above).
5. **Demote `metadata.cohort`** to derived-cache or drop, after all reads have moved.

## Resolved decisions

- **Fork 1 — study entity: UNIFIED.** One study entity; a cMD study is a `collections`
  row with `source='cmd'`; `curated_sample_annotations` are annotation rows attached to
  it. (Folded into "Proposed model → Study" above.)
- **Fork 2 — active version: EXACTLY ONE per `workflow_id`**, partial-unique-index
  enforced, promote-auto-retires-prior. (Folded into "Version / completion" above.)

## Open questions (remaining)

1. **Drop `metadata.cohort` entirely, or keep it as a read-only derived cache?**
   Position: keep short-term as a derived display cache (less frontend churn), drop once
   the Samples page reads membership directly. Low-stakes; decide at migration step 5.
2. **Rename `collections` → `studies`?** Purely cosmetic; the table becomes *the* study
   entity so the name `collections` is now slightly misleading. Defer to migration time.

## What this design explicitly does NOT do

- Does **not** change `sample_id` (content-addressing stays).
- Does **not** pull publications, vocabularies, or provenance into the telemetry DB —
  that boundary from `sample-metadata-design.md` is unchanged.
- Does **not** couple dispatch/reconcile to study identity beyond what already exists.
- Does **not** attempt subject-level identity (cMD `subject_id`) — sample-level only.
- Does **not** touch the outputs catalog schema (#57) — but it *unblocks* it by giving
  the catalog a stable study key to group outputs under.
