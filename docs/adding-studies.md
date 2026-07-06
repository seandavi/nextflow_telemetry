# Adding studies

How to register an SRA study / BioProject's samples into the telemetry system.
Two audiences: **submitters** (request a study) and **maintainers** (review,
approve, and dispatch). The normal path is a GitHub issue; a direct API/CLI path
is documented at the end.

The flow, at a glance:

```
open "add-study" issue  →  dry-run preview comment  →  maintainer reviews
     (submitter)              (bot, automatic)              (maintainer)
                                                                 │  add `approved` label
                                                                 ▼
                                             registered  →  reconcile → jobs dispatched
                                              (bot)          (maintainer, manual)
```

## For submitters — request a study

1. Open a new issue with the **“Add Bioproject or SRA study”** template.
2. Put an **INSDC study or BioProject accession** in the Accession field —
   `PRJNA…`, `PRJEB…`, `PRJDB…`, or `SRP…` / `ERP…` / `DRP…`. A single run
   (`SRR…`) or sample (`SRS…`) accession is **not** accepted; a study/project is
   required. Add optional notes for the reviewer (cohort, context, urgency).
3. Submitting the issue automatically posts a **dry-run preview** comment within
   a minute or two. It reports:
   - how many **samples** the accession expands to, and how many are **new** vs.
     already registered;
   - a **library composition** sanity check (see below).
4. Then wait — a maintainer reviews the preview and approves. Nothing is
   registered until they do. If you got the accession wrong, just **edit the
   issue**; a fresh preview is posted on every edit.

### Reading the dry-run preview

```
🔍 Dry-run: PRJEB17784 — found 200 samples (200 new, 0 already registered).

Library composition (sanity check — expect shotgun WGS, not 16S/amplicon):
- library_strategy: WGS (364)
- library_selection: RANDOM (364)
- library_source: METAGENOMIC (364)
- instrument_platform: ILLUMINA (364)
```

The composition is aggregated per-run from the ENA Portal API. This pipeline is
for **shotgun (WGS) metagenomics on Illumina short reads**, so a healthy study
looks like the above. The strong negative signal is **`library_strategy: AMPLICON`**
or **`library_selection: PCR`** — that's 16S/amplicon data, which this pipeline
does not process. When present, the preview adds a `⚠️` warning line. The check
is **advisory only** — it never blocks a submission; a human makes the call.

## For maintainers — review, approve, dispatch

1. **Review the preview comment.** Sanity-check both the sample counts and the
   library composition. Confirm it's `METAGENOMIC` / `WGS` / `RANDOM`, platform
   `ILLUMINA`; heed any `⚠️` amplicon/16S warning. Mixed studies (some WGS, some
   16S) are common — the counts show the split so you can decide.
2. **Approve** by adding the **`approved`** label to the issue. That triggers the
   real registration: it creates the new samples, the collection + membership,
   and mints a `submission_id` (provenance), then comments the result and closes
   the issue. Existing samples are **not** clobbered — a sample already present
   (e.g. shared across studies) only gains a membership row; its metadata is
   untouched. Re-approving the same accession is a harmless no-op, still recorded.
3. **Dispatch is a separate, manual step.** Registration only creates sample
   records — **it does not create or dispatch jobs.** When you're ready to run
   the pipeline on the new samples, reconcile:
   ```
   nf-client reconcile            # or: POST /api/admin/reconcile-jobs
   ```
   That creates the `pending` jobs; the HPC daemon claims them on its next poll.

### One-time setup (admin)

The `add-study` GitHub workflow needs two repository settings:

| Kind | Name | Value |
|------|------|-------|
| Variable | `NF_TELEMETRY_URL` | API base URL, e.g. `https://nf-telemetry.cancerdatasci.org/api` |
| Secret | `NF_OPERATOR_TOKEN` | mirror of GCP SM secret `cmgd-api-operator-token` |

## Direct API / CLI path (no GitHub)

The GitHub flow is a thin wrapper over the submissions API. With an operator
token (`$NF_OPERATOR_TOKEN`) you can do the same directly:

```bash
# Preview (writes nothing) — returns counts + library_composition + warnings
nf-client submit-study PRJEB17784 --dry-run --json

# Register for real (mints a submission_id); add --reconcile to also create jobs
nf-client submit-study PRJEB17784
nf-client reconcile
```

Or against the HTTP API: `POST /api/submissions {"accession": "…", "dry_run": true}`
to preview, then without `dry_run` to register, then `POST /api/admin/reconcile-jobs`.

## Notes

- **Sample identity** is content-addressed: a sample's id is the md5 of its
  sorted run (SRR) accessions, so the same biological sample registered from two
  studies resolves to one record shared by both collections.
- **`display_name` / DOI and other descriptive metadata are intentionally out of
  scope here.** This path is technical validation + registration only; metadata
  enrichment is handled separately.
