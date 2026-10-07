# Plan: finish the v2 cutover, then metadata phase 1

_Drafted 2026-10-07 from a review of nextflow_telemetry, curatedMetagenomicsNextflow,
curatedMetagenomicDataCuration and metacurator. Decision context: #170 (2026-10-07
comment). Tracking issue: see "Tracking" at the end._

## The decision this plan executes

Split by concern (#170): the **v2 Worker** is the control plane (dispatch, runs, timers,
event sink, task logs). A **Postgres catalog service** (v1 cut down) owns study and
sample metadata, submissions, curated annotations and the historical-metrics tier
(DuckDB over R2 NDJSON). The shared key between them is the sample/readset id.

## What the review found (facts the plan has to respect)

| # | Fact | Consequence |
|---|---|---|
| F1 | `cf/` lives on `feat/cf-control-plane` (PR #182 open) and `feat/cluster-parity`; `main` has no `cf/`. The deployed Worker is built from an unmerged branch. | Merge first. Everything else builds on `main`. |
| F2 | Dashboard (`cmgd.cancerdatasci.org`), the fleet-health cron (#143), GitHub var `NF_TELEMETRY_URL`, the curation repo's `build_manifest.py`, and the pipeline's default `params.api_url` all point at **v1**. | Five repoints, not one (#191 covers only the dashboard). |
| F3 | HUMAnN pilot tasks post `/api/task-logs` to v1 with `run_name='null'` and some bodies contain NUL bytes (500s in the v1 log). | Pipeline `afterScript` must skip upload when `run_name` is null; the catalog/Worker must strip `\x00`. |
| F4 | v1 holds 1,727 samples in 28 collections (2,300 memberships); v2 holds 4 samples in 2 collections. v1 has 1,715 orphaned `pending` jobs for retired versions. | The catalog has to be re-registered into v2, not migrated row by row (#171 already chose reprocess-from-scratch). |
| F5 | ADR-0007 (readset ids) is accepted, not implemented, and says every sample will be re-run and ids/paths need not survive. metacurator already implements the same algorithm (SPEC 170) and its 727k-readset discovery output is keyed by it. | Implement readset ids **before** the bulk re-registration, or we pay for two id epochs. |
| F6 | Pipeline `main` is at `ad0225a` with unreleased **breaking** changes (`--metaphlan_profile` replaces `--metaphlan_index`; HUMAnN bundles; `--databases_only`). v2 has `cmgd_nextflow 2.2.1 @ 0623be7`. CHANGELOG rule: tag, `manifest.version` and dispatched revision move in lockstep. ADR-0004 (version vs revision) is still **Proposed**. | A `2.3.0` registration is imminent. Per ADR-0004 it changes the job set (new epoch); per #192 concurrent revisions share one asset checkout. Sequence it. |
| F7 | Issues #185–#190 (six pMD studies) have **no `add-study` label**, so `add-study.yml` skipped; nothing was registered. The action targets v1 anyway. | Fix the flow once, against v2 + the catalog service, then process the six. |
| F8 | curatedMetagenomicDataCuration publishes `inst/extdata/studies_status.csv` weekly: 152 studies, 35,388 samples, 1,727 processed; `processing_status` ∈ {processed 6, partial 22, not_processed 109, no_run_accessions 15}. Columns include `study_name`, `study_id` (BioProject), `n_samples`, `n_samples_processed`, `telemetry_collections`, `primary_disease`, `body_site`, `country`, `pmid`. Its R helper `nf_get_completed_run.R` still points at the dead Cloud Run URL. | This CSV **is** the phase-1 vocabulary users understand. Serve it from the catalog instead of recomputing it in a GitHub Action. |
| F9 | The 136 curated `*_sample.tsv` files already carry `target_condition`, `body_site`, `country`, `host_species`, `age_group`, `sex`, `pmid`, `ncbi_accession` with ontology ids. v1 `POST /curated/import` already loads them. | Phase-1 facets need no harmonisation and no LLM. |
| F10 | metacurator discovery (2026-05-01 SRA snapshot, Clef): 4,591 include studies, 727,070 readsets (650k human / 75k mouse), outputs in `r2://cmgd-raw/discovery/2026-05-01/cmd/`. 376 studies in review. | A bulk pool exists but is ~16× cMD. Inclusion for bulk runs is a cost decision that needs a gate, not a plan step. |
| F11 | Deployed Worker has `ALLOW_RESET="true"` and `CORS_ORIGINS="*"`. | Production hardening is part of cutover, not after. |

## Phase 0 — Land and harden v2 (1 week)

Goal: v2 on `main`, production-safe, and the only thing the clusters and the pipeline talk to.

1. **Merge PR #182** into `main`; rebase `feat/cluster-parity` on top and merge it too. Close #178 (first real batch ran 2026-10-03/04: 4 runs, 1 wrapper failure, 3 completed).
2. **Harden the Worker**: `ALLOW_RESET="false"`, `CORS_ORIGINS` = dashboard origin(s), and a smoke test that asserts `POST /admin/reset` returns 403. Rotate the operator token (STATUS.md item 1) if not done.
3. **Pipeline default URL**: change `params.api_url` in `nextflow.config` to the v2 origin (`/api`), and make the `afterScript` task-log upload a no-op when `params.run_name` is null (F3). Strip `\x00` from log bodies before upload. Release as a **patch** (`2.2.2`) because no successful-sample output changes (ADR-0004 rule), then `POST /workflows/{pk}/revision` on v2.
4. **Repoint the other callers of v1** (F2): GitHub var `NF_TELEMETRY_URL`; the fleet-health cron (#143) base URL; `build_manifest.py` in the curation repo (`TELEMETRY = …`); `nf_get_completed_run.R` (either point at v2 `/api/runs?status=completed` or delete it; the Cloud Run URL has been dead since May).
5. **Dashboard on v2** (#191) with the acceptance criteria already written there: explicit "not available yet" state for 501 routes, write actions hidden until #176.
6. **Auth decision #176**: pick Cloudflare Access for the browser, keep the bearer token for daemons and operators, GETs stay open. Record it; the catalog service inherits the same answer for its own write routes.

Exit: `cmgd.cancerdatasci.org` shows v2 data; no process on any cluster or in any repo references `nf-telemetry.cancerdatasci.org/api`; v1 container is read-only (keep it up until Phase 2 step 4).

## Phase 1 — Readset identity and the catalog seam (1–2 weeks, before any bulk load)

Goal: one id algorithm everywhere, and the catalog service standing next to v2.

1. **Implement ADR-0007 in v2 ControlDO, nf-client and the pipeline** publish path (`<publish_base>/cmgd_nextflow/<version>/<readset_id>/`). Golden vectors are in the ADR and in metacurator SPEC 170; `cf/test` and `nf_client` tests pin all three implementations to each other. Keep `sample_id` as an alias column during Phase 2 so the curated TSV join (F9) still works.
2. **Supersede the md5 id in the docs**: ADR-0007 → Accepted (implemented); amend `docs/study-sample-version-identity.md` and `cf/README.md` ("Sample identity is the content address…") to say readset id.
3. **Stand up the catalog service** from v1 by deletion, not construction: keep `routers/{submissions,curated,auth}` and `services/{submission,curated,collection,auth}`, `etl/`; delete `routers/{dispatch,runs,daemons,admin,process_metrics,task_logs,workflows}` and `services/{dispatch,lifecycle,reconcile,process_metrics,workflow}` and the sweeper endpoints. Postgres keeps `samples`, `submissions`, `curated_*`, `users`, `etl_*`; drop `jobs`, `runs`, `dead_letter`, `daemon_agents`, `task_logs`, `telemetry`, `task_executions` after a final `pg_dump` through the container (deploy gotchas memo). Database size drops from 885 MB to tens of MB.
4. **Catalog writes through to v2**: `submit-study` and `curated/import` register samples into v2 via `POST /api/samples` (what `nf-client add-samples` already does) after writing their own provenance rows. `source` on the v2 collection records `cmd` / `insdc` / `discovery`.
5. **Historical tier spike (#175)**: one real query (`/metrics/processes/summary` shape) with DuckDB over `r2://nf-telemetry/telemetry/events/**/*.ndjson.gz`, served from the catalog service, response shape from `models.py`. Paste query + output into #175, then implement the other seven routes and `/cohorts/{id}/failures` as follow-ups. Add the SinkDO `source`/`run_name` partition keys the queries need, if the spike shows a scan cost problem.

Exit: `nf-client add-study` works end to end against catalog → v2; `/api/metrics/processes/summary` returns 200 from the catalog service with v1's shape.

## Phase 2 — Re-register the corpus and register the next pipeline version (1 week, cluster time dominates)

1. **Fix the add-study flow** (F7): make the action also trigger on `labeled` with a dedupe guard, or have a tiny workflow add the label to any issue whose title starts with `[add-study]`. Point it at the catalog service. Then label #185–#190 and let the dry-runs post; approve.
2. **Re-register cMD** into v2 through the catalog: `add-cmd` for the 136 curated studies (readset ids), then `POST /admin/reconcile-jobs`. This is the "reprocess from scratch" #171 chose. Expect ≈ 1.7k + pMD jobs under the active version.
3. **Pipeline 2.3.0** (F6): cut the tag when HUMAnN defaults are decided (#90, #98), register with `nf-client register-workflow --id cmgd_nextflow --version 2.3.0 --revision 2.3.0`. v2 enforces one active version per `workflow_id`, so 2.2.1 retires and its pending jobs purge; completed 2.2.1 jobs archive to R2. Decide **before** step 2 whether the first full re-run is 2.2.1 or 2.3.0; running the corpus twice in a month is the expensive mistake here. Recommendation: reconcile under 2.3.0 only, after the HUMAnN comparison closes on 2026-10-08.
4. **Fix #192** on the cluster side (per-revision asset checkout via `NXF_HOME`/`nextflow pull -r` per run or per revision) before 2.3.0 goes active alongside 2.2.1 stragglers.
5. **Retire the v1 API container** once the catalog service answers `/submissions`, `/curated`, `/auth`, `/metrics/processes/*`. Keep the Postgres database (it is now the catalog).

## Phase 3 — Metadata phase 1: progress in the users' terms (2 weeks)

Users are cMD curators and downstream scientists. Their unit is the **study** (cMD `study_name`, BioProject), and their questions are: which studies are processed under the current pipeline, how far along is each, what is in them (condition, body site, country, host), and where are the outputs. `studies_status.csv` (F8) already answers the first two in the right vocabulary; we move it in-house and add the third and fourth.

1. **Catalog `studies` table** (Postgres): `study_name` (PK), `study_id` (BioProject/SRA study), `source` (cmd/pmd/insdc/discovery), `pmid`, `title`, `curation_available`, plus the curated facets rolled up from the sample TSVs (`primary_disease`, `body_site[]`, `country[]`, `host_species[]`, `n_samples_curated`). Loaded by the existing `curated/import` path from the curation repo's `inst/curated/`. Membership: `study_samples(study_name, readset_id)` many-to-many (identity doc, ADR-0005). The v2 `collection_id` equals `study_name` for cMD studies; that is the join to live counts.
2. **`GET /api/studies`** on the catalog service: catalog columns joined with live counts pulled from v2 (`/cohorts/leaderboard` gives samples completed under the active version per collection). Returns per study: `n_samples`, `n_processed_active_version`, `n_running`, `n_failed`, `processing_status` (same four values the curation repo uses), `outputs_prefix` (`r2://cmgd-raw/cmgd_nextflow/<version>/`), facets. Cache for 60 s.
3. **Dashboard**: replace the Cohorts page with a **Studies** page: one row per study, progress bar in samples under the active version, status chip, facet filters (disease, body site, host, country), link to outputs. Overview page headline becomes "N studies processed, M partial, K not started under cmgd_nextflow 2.3.0" plus total samples, instead of job counts. Samples page gains the readset id and the study column.
4. **Feed the curation repo**: `build_manifest.py` becomes a 20-line fetch of `/api/studies` (or is retired and the repo's Pages site reads the endpoint). One source of truth for "processed", computed where the data is.
5. **Phase 1 boundary**: no harmonisation, no vocabularies, no publications, no LLM. Those are the harmonisation module in `docs/sample-metadata-design.md` and metacurator; they read from the catalog, never write to it.

## Phase 4 — Bulk pool from metacurator (gated)

Not scheduled; make it a decision with data first.

- **Gate**: a cost model. 727k readsets is ~16× cMD. Price one readset end to end on Alpine from the 2.3.0 batch telemetry (wall-time, storage per sample on R2), multiply, compare to the ACCESS allocation. Record the number in the issue before anything is registered.
- **Narrowing knobs already recorded per study**: `body_site`, `study_kind`, `host`. A stool-only, cohort-plus-intervention, human-only cut is the obvious first pool.
- **Mechanics once gated**: load `readsets.parquet` into the catalog `studies`/`study_samples` with `source=discovery`, one v2 collection per SRA study, `status=paused` workflow or a separate `workflow_id` so discovery jobs never starve curated ones, then reconcile in batches. Readset ids already match (F5), so no re-keying.
- **Review queue**: the 376 `review` studies go to a human list in the catalog, not to the pool.

## Issues to close, retitle or supersede

| Issue | Action | Why |
|---|---|---|
| #178 | Close | First real batch ran 2026-10-03/04 on v2. |
| #182 | Merge | F1. |
| #175, #180 | Decided 2026-10-07; convert to implementation tickets per Phase 1.5 and 1.3 | — |
| #171, #172, #181, #183, #184 | Already closed | — |
| #62, #66, #68 (run-lifecycle meta, pipeline hooks, sacct polling) | Retitle to v2 context or close #62; #68 stays (client-side) | v2 timers replaced the sweeper half; the wrapper/`sacct` half is unchanged. |
| #83 (pg_duckdb for analytics) | Close as superseded | Analytics are DuckDB over R2 NDJSON, not Postgres. |
| #84 (Postgres backups) | Keep, narrow to the catalog DB | Still self-hosted; much smaller after Phase 1.3. |
| #86 (client disk buffer) | Keep | Still valid; v2 does not change the client. |
| #93, #57, #152–#158 (artifact catalog, DuckLake ETL phases) | Keep; re-home to the catalog service | They assumed Postgres on the same host, which is still true for the catalog. |
| #114, #115, #119 | Closed in v1; verify v2 parity then leave closed | v2 implements retire-purges-pending, liveness timer, leaderboard. |
| #116, #117, #118, #120, #121, #122 (UI) | Keep; #116 is solved by v2's active-version leaderboard, close after #191; #118 Runs pagination exists in v2 (`limit/offset`), close after frontend uses it | — |
| #173, #174, #179 (API contract, parity audit, frontend seam) | Keep; sequence after #191 | — |
| #177 (custom domain, CORS) | Keep; part of Phase 0.2 | — |
| #185–#190 | Process in Phase 2.1 | F7. |
| #192 | Phase 2.4, blocks 2.3.0 going active | — |
| #143 | Repoint the cron in Phase 0.4 | Reports v1 numbers today. |
| #160 (end-user docs) | Keep; feeds from Phase 3.2 | — |
| curation repo: `nf_get_completed_run.R` | Delete or repoint | Dead URL since May. |

## ADRs to supersede or amend

| ADR | Change |
|---|---|
| 0006 Cloudflare control plane | **Amend**: scope is the control plane, not "replace the FastAPI + Postgres server". Record the split, the catalog service, and that the historical tier is DuckDB over R2 in Python. Add `ALLOW_RESET=false` as a production invariant. Status stays Accepted. |
| **New 0009** Catalog service in Postgres, control plane in the Worker | The decision from #170: what lives where, the shared key, write-through from catalog to v2, read-only harmonisation boundary. Supersedes the "replacement" framing in 0006 and the "stay on v1" alternative. |
| 0004 Version vs revision | **Proposed → Accepted**. v2 implements it (`POST /workflows/{pk}/revision`, job key excludes revision). Add the 2.2.2 (patch, revision bump) vs 2.3.0 (new epoch) worked example from Phase 0.3 / 2.3. |
| 0007 Readset identity | **Accepted (not yet implemented) → Accepted** after Phase 1.1; note metacurator SPEC 170 as a second conforming implementation. |
| 0005 Single collection seam | Add the status line (missing); note the v2 `collection_samples` table and the Phase 3 `study_samples` join as the same seam. |
| 0002, 0003 Run death classification, dispatchability | Note "implemented in `cf/src/control-do.ts`"; no decision change. |
| **New 0010** Bulk inclusion from discovery (Phase 4) | Only once the cost gate has numbers. |
| Pipeline ADR-0015 (R2 storage profile) | Add the readset-id publish path once Phase 1.1 lands. |

## Order and dependencies

```
Phase 0 (merge, harden, repoint) ─┬─▶ Phase 1 (readset ids, catalog seam, metrics spike)
                                  │            │
                                  │            ▼
                                  └─▶ Phase 2 (add-study fix, re-register, 2.3.0, retire v1 API)
                                               │
                                               ▼
                                   Phase 3 (studies table, /api/studies, Studies page)
                                               │
                                               ▼
                                   Phase 4 (gated: bulk pool)
```

Phase 1.1 (readset ids) must precede Phase 2.2 (re-registration). Phase 2.3 (2.3.0) should precede 2.2 so the corpus runs once. Phase 3 can start its schema and endpoint work during Phase 2 cluster time.

## Tracking

Open one tracking issue per phase (0–3) with the numbered steps above as checkboxes; Phase 4 is a single "decision needed" issue carrying the cost model. Link all to #170.
