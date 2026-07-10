# nextflow_telemetry — Work Plan / Roadmap

_Last updated: 2026-07-10. Author: planning pass over the open-issue backlog after a
telemetry-hardening + 100k-scale UI evaluation session; 2026-07-10 added the Architecture
deepening section from a `/improve-codebase-architecture` design review._

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
- **Single-active-version enforcement** DONE: partial unique index
  `uq_one_active_version_per_workflow` (in `db.py` + migration `f4a5b6c7` with data
  cleanup + orphan-pending purge); `WorkflowService.register`/`update_status` auto-retire
  the prior active version (and purge its pending jobs) so the invariant holds at
  runtime. Migration verified end-to-end against real Postgres. Remaining Epic A:
  the membership-consolidation migrations (collections as SoT, demote `metadata.cohort`,
  unify `curated_studies`).

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
  past 1000 rows today). _Samples DONE: `GET /samples` returns a paginated envelope
  `{items,total,limit,offset}` with server-side `search`+`cohort` filters, plus
  `GET /samples/facets/cohorts` for whole-catalog chip counts; SamplesPage is now fully
  server-driven (debounced search, server pagination). Runs/cohort-failures pagination
  still TODO._
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
  `GET /api/published` → **dedicated cmgd** catalog DB → completion-hook write →
  file→table ETL + logical tables → **frozen-DuckLake** export → deprecate
  `curatedMetagenomicDataETL`). Design: `publish-and-catalog-design.md` (plumbing) +
  `output-catalog-etl-design.md` (file→table, schema, taxonomy).
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

## Architecture deepening (design review, 2026-07-10)

A `/improve-codebase-architecture` pass over the server, ETL, nf-client, and frontend
looked for **deep-module** opportunities (small interface over a lot of behaviour, tested
through that interface) rather than features. Distinct lens from the epics above: these are
internal refactors, most **not** tracked as issues. The through-line: a load-bearing concept
(job lifecycle, membership, "is this alive?", the `trace` shape) is re-derived inline at each
call site and kept consistent by "keep in sync with…" comments instead of by code. Each
candidate gives the concept one owning module. Full write-up was a scratch HTML review (not
in-repo); the table is the durable record.

Vocabulary (from `/codebase-design`): **module · interface · depth · seam · leverage ·
locality**. Strength: Strong / Worth exploring / Speculative.

| # | Deepening | Strength | Status | Seam / files | Cross-ref |
|---|-----------|----------|--------|--------------|-----------|
| 1 | Study-membership module — one write seam | Strong | **✓ DONE #164/#165** | `add_to_collection()` in `services/collection.py`; retired `metadata.cohort` | Epic A, ADR-0005 |
| 2 | Shared active-version scope predicate | Worth exploring | open | `_workflow_scope` (cohort) re-inlined in admin.stats, process_metrics; completion% redefined 3× | **#116**, Epic B |
| 3 | Job-lifecycle module (enums + legal transitions) | Strong | **✓ DONE #166** | `services/lifecycle.py` — free fns take `conn`; JobStatus/RunStatus enums; guarded/idempotent; carve-outs documented | — |
| 4 | DispatchService — pull the claim out of the handler | Strong | **✓ DONE #167** | `services/dispatch.py` (@dataclass, engine); pick-then-lock + response assembly; router thinned; calls `lifecycle.claim()` | pairs w/ #3 |
| 5 | Liveness module — one "is this alive?" | Strong | open · **latent bug** | stalled re-derived 6× / 5 columns; read path calls `submitted` runs stalled, watchdog won't reap them | **#115**, ADR-0002 |
| 6 | One reader for the `trace` JSONB | Worth exploring | open | decoded 3 ways (`.get` / `->>`); FAILED/ABORTED predicate copied ~17× | #62 |
| 7 | Make the ETL spec actually drive ingest | Worth exploring | open | `specs.py` bypassed (qc special-case); `lake.SCHEMAS` hand-synced to parser keys; cli re-loops | ETL #153–158 |
| 8 | Submitter seam for HPC modes (nf-client) | Strong | open | if/elif over mode twice; `submit_*` signatures disagree; `lsf` dead branch | #68 (sacct behind slurm adapter) |
| 9 | Shared `useFetch` (loading/error) hook | Strong | open | fetch+loading+error copied in all 8 pages; errors swallowed → permanent spinner | #118, #121 |
| 10 | One status→colour/label map + real union | Strong | open | ~60 inline ternaries; `classification`/`status` typed as bare `string` | #121 |
| 11 | "Run log artifacts" module | Worth exploring | open | `ON CONFLICT uq_task_log` upsert ×3; run-level logs smuggled into task-keyed table | #120, #62, #93 |
| 12 | Delete phantom `RunEvent` variants (YAGNI) | Strong · deletion | open | `wrapper_log`/`slurm_state`/`workflow_oncomplete` have zero producers | #62/#66 (re-add when real) |

**Solid ground (deep already — don't touch):** `WorkflowService` write side (one-active-version
invariant + auto-retire), `process_metrics` service, ETL `engine.py`+`specs.py` split, `log.py`
JSONFormatter, `lib/api.ts`+`types.ts` network seam, `_CaptureBuffer`.

**Where to start:** #3 done (#166) — #4 DispatchService is next (builds on `lifecycle.claim()`);
#5 liveness for the one real correctness bug (ties into #115 already shipped); #9 + #12 as the
fastest relief for "the UI feels confusing." #2, #6, #7 fold naturally into work already scheduled
under Epic B / ETL.

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
- A full 69-sample re-run of `cmgd_nextflow` 2.0.6 (2026-07-02) stress-tested the
  telemetry and surfaced the walltime-TIMEOUT zombie-run gap, #115 (see memory
  `project_timeout_zombie_run`, `reference_alpine_ssh_access`).
