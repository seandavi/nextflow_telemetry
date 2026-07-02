# nextflow_telemetry — Work Plan / Roadmap

_Last updated: 2026-07-02. Author: planning pass over the open-issue backlog after a
telemetry-hardening + 100k-scale UI evaluation session._

The project has grown organically: orchestration works well, but the **data model
underneath it is muddled** (study identity, completion semantics, sample metadata),
and several UI/observability gaps are downstream symptoms of that. This plan groups
the 33 open issues into epics, states the dependency order, and takes a position on
the "additional data/metadata" question.

## The one load-bearing insight

Most correctness and UX complaints trace to **three unsettled identity/semantics
decisions**:

1. **Study/collection is not first-class.** It's free-text `samples.metadata.cohort`
   in some code paths and `collection_id` in others — they can silently disagree
   (found in the UI eval; see #119, #116).
2. **"Completion" is measured across all workflow versions**, so a study that is done
   under the active pipeline reads as ~0.5% complete (#116). Every count the system
   reports inherits this.
3. **Sample metadata lives in ≥2 free-form JSONB blobs** with no provenance or
   controlled vocabulary (#55).

Nothing downstream — correct completeness, a study leaderboard, cross-study
analytics, the outputs catalog's keying — is right until these are settled. **Settle
them in a short design doc before writing migrations.** This is the highest-leverage
move in the whole backlog.

## Epics

### Epic A — Data-model foundation  ⟵ START HERE
Settle identity/semantics before adding columns. Deliverable: design doc(s), then a
migration plan. Blocks B, D, E.
- **Study/sample/version identity** settled in `docs/study-sample-version-identity.md`
  (2026-07-02). Resolved: sample↔study is **many-to-many** (kills `metadata.cohort` as
  source of truth; join table wins); **one unified study entity** (cMD study =
  `collections` row with `source='cmd'`); **exactly one active version per
  `workflow_id`**; completion measured in **distinct samples under the active version**.
  Migration plan sketched there; not yet written.
- **#55** sample metadata data model + agentic (LLM) harmonization — write
  `docs/sample-metadata-design.md` answering: annotation key `(sample_id)` vs
  `(sample_id, study)`; provenance (`source`/`evidence`/`confidence`); publications
  as an entity (PMID/DOI) vs stashed; vocabularies in CHECK vs `vocabularies_tbl`;
  re-run semantics; human-in-the-loop review. Scope: human + mouse hosts only.
- Make **study/collection first-class and consistent** (one source of truth for
  membership; subsumes the data-model half of #116/#119).

### Epic B — Operational-state correctness  (cheap, high-value, unblocks the UI)
- **#114** retiring a workflow reconciles its pending jobs; bucket pending by
  active/retired in stats (fixes the "298 pending" illusion). _DONE: `/admin/stats`
  returns `jobs_by_status_active` alongside the all-versions `jobs_by_status` (#125);
  and retiring a workflow now purges its still-pending (never-dispatched) jobs at the
  source, leaving in-flight/completed jobs untouched (pausing purges nothing)._
- **#116** per-sample completion under the **active** workflow version (depends on A).
  _DONE (branch `epic-b/completion-semantics`): cohort `summary()` now measures
  completion as distinct samples with a completed job under the active version
  (`completion_pct = samples_completed / sample_count`); `all_workflows=true` opts into
  the old across-versions view. Read-path only; tested. TODO: frontend samples-based
  labels + the #119 leaderboard on top of the corrected semantics._
- **#115** server-side heartbeat-staleness watchdog — makes `running` honest; a
  walltime-killed run currently sits `running` forever (observed live 2026-07-01).
  _DONE: `POST /admin/heartbeat-watchdog?stale_after_minutes=15` fails `running` runs
  whose `last_heartbeat_at` (fallback `started_at`) is stale and sweeps their jobs
  through retry/DLQ; queued `submitted` runs (no heartbeat by design) are untouched.
  Operator-invokable via a new Dispatch → Heartbeat Watchdog tab; cron-safe._

### Epic C — Run-lifecycle observability  (finish the #62 epic)
Tracker: **#62**. "Know when/why a run died."
- **#115** heartbeat watchdog (also in B — do first, no cluster access needed).
- **#68** daemon-side `sacct`/`squeue` polling → `slurm_state` events with reason
  (server already models the event + columns; **purely client-side**). Surfaces
  TIMEOUT/OOM/NODE_FAIL and "queued behind maintenance".
- **#66** pipeline `workflow.onComplete`/`onError` hooks (lives in the pipeline repo).
- Note: the wrapper's SIGTERM trap does **not** reliably fire at SLURM walltime on
  Alpine, so the watchdog (#115) is the real backstop, not the wrapper.

### Epic D — Study-centric dashboard & scale  (the 100k-operator UX)
Built on B's corrected semantics. From the UI eval (#116–#122) + older UI issues.
- **#117** completion timeline endpoint → burndown + ETA + rate-based stall alert
  (the single biggest missing thing for a 100k operator).
- **#119** cross-study completeness leaderboard (depends on #116). _DONE: batched
  `GET /api/cohorts/leaderboard` (active-version completion in samples, laggards-first)
  + sortable leaderboard table on CohortsPage with a "stalled" flag (no completion in
  7d). Single-cohort drill-down kept below._
- **#118** server-side pagination for Samples/Runs/cohort-failures (Samples is broken
  past 1000 rows today).
- **#120** failure-triage drill-down: failed task → its log (`task_hash` exists,
  `<TaskLogViewer>` already built — wire it up).
- **#121** legibility (human run-name labels, explain `ENDED-NO-LOG` / `exit
  2147483647`, default Workflows to active).
- **#122** accessibility (no headings on any page, muted-text contrast, aria, focus).
- Older, still-valid: **#13/#14** metrics filters (time/workflow), **#15/#16** live
  running-samples view, **#17** per-host throughput, **#40** infra/daemon-fleet page
  (partially landed).
- Sequence: #116 → #119 → #117; #118 + #120 in parallel; #121/#122 as polish.

### Epic E — Pipeline outputs & analytical catalog  (science-facing data growth)
Tracker: **#57**; design doc `docs/publish-and-catalog-design.md`. This is the
"additional data" that turns telemetry into a queryable science surface.
- **#93** per-task artifact catalog: server-side `publishDir` enumeration via
  `universal_pathlib` (credentials once in the server, not per-cluster) →
  `artifacts_tbl`. Feeds #57.
- **#57** DuckLake catalog (7 shippable steps: `publish_base_uri` migration →
  `GET /api/published` → catalog DB → completion-hook write → logical views →
  public-parquet generator → deprecate `curatedMetagenomicDataETL`).
- **#83** `pg_duckdb` for analytical queries (process_metrics, cohort) once volume
  warrants.

### Epic F — Auth & roles  (parallel security track)
Tracker: **#94**. Rollout: ship API with enforcement **off**, roll daemons, then flip.
- **#97** service bearer token on `/dispatch/*` (behind env flag).
- **#98** nf-client sends `Authorization: Bearer` on dispatch + heartbeat.
- **#96** gate workflow pause/edit + TSV ingestion on roles.
- **#41** dashboard action-endpoint auth. Note: the UI eval found mutating dispatch
  actions are **client-side gated only** — server enforcement is the real fix.

### Epic G — Ops resilience & misc  (backlog / as-needed)
- **#84** Postgres backup strategy — **do soon**; the DB is self-hosted now, this is
  cheap insurance. Arguably belongs in the near-term tier.
- **#86** client-side disk buffer + retry (parking-lot; only if we see event loss).
- **#87** off-cluster orchestrator (push mode via SSH ControlPersist) — preserve pull.
- **#18** pre-populate pipeline DBs in `store_dir` before running.
- **#46** command-line API client.
- **#48** Temporal investigation — **recommend decline/park**: solves orchestration,
  not the visibility gap we actually have; re-introduces heavy stateful infra.

## Suggested sequencing

1. **Foundation (now):** A design docs (`sample-metadata-design.md`; confirm
   `publish-and-catalog-design.md`) + B correctness (#114, #116, #115). Plus #84
   backups as cheap insurance.
2. **Observability + core UX (next):** C (#68, #66) and D core (#119, #117, #118,
   #120) on the corrected model.
3. **Data growth (then):** E outputs catalog (#93 → #57 → #83).
4. **Parallel:** F auth (#97 → #98 → #96/#41), rollout enforcement-off first.
5. **Backlog:** G remainder; decline #48.

## On "additional data/metadata" (the user's question)
Yes — pursue **both** sides, each gated behind a design doc (#55 and #57 both ask for
one, correctly):
- **Input:** first-class study + sample metadata with provenance, controlled
  vocabularies (human/mouse scope), publications as entities, and re-runnable LLM
  harmonization with citations + confidence (#55).
- **Output:** artifact catalog + DuckLake so completed runs become queryable science
  (#57/#93/#83).
The prerequisite for both is Epic A's identity model. Do the study/sample/version
design doc first; it unblocks correct completeness (#116), the leaderboard (#119),
metadata (#55), and catalog keying (#57/#93) simultaneously.

## Session context captured for continuity
- Telemetry verified end-to-end on Alpine; a forced walltime TIMEOUT exposed the
  silent-death gap → **#115**. Retired-workflow orphan-jobs confusion → **#114**.
- 100k-scale UI eval (reusable agent `.claude/agents/ui-observability-evaluator.md` +
  `scripts/ui_eval_capture.py`, merged in #123) → **#116–#122**.
- A full 69-sample re-run of `cmgd_nextflow` 2.0.6 was kicked off 2026-07-02 to
  stress the telemetry (see memory `project_timeout_zombie_run`, `reference_alpine_ssh_access`).
