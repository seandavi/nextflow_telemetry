# 0004. Distinguish workflow version (logical) from revision (operational)

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** Sean Davis

## Context

A workflow in the registry is identified by two fields that already play
different roles in the schema:

- The **job key is `(sample_id, workflow_id, version)`** — `revision` is
  deliberately *excluded*. The `workflows` table comment states it outright:
  *"revision is intentionally mutable: the composite job key is
  `(workflow_id, version, sample_id)`, so changing revision does not force
  reruns."* `workflow_runs.revision` records the exact code a given run used.
- Completion is measured **per active version** (ADR-relevant: issue #116). A
  new `version` therefore invalidates prior completions — they read as
  incomplete until re-run.

Two forces are in tension:

1. **Provenance / reproducibility** — we want to know exactly what code produced
   each output.
2. **Operational agility at scale** — the target corpus is 10k–100k+ samples.
   Reprocessing the entire corpus for a code change that does **not** alter the
   outputs of already-successful samples is prohibitively wasteful in compute,
   storage, and time.

The decision was forced by a concrete incident (2026-07-02): a batch-poisoning
failure-handling bug — one bad SRA download (`fasterq_dump` exit 3) failed an
entire 25-sample Nextflow batch, dead-lettering 56 samples of which 52 were
healthy — required a pipeline fix (`curatedMetagenomicsNextflow` PR #68). That
fix changes **no successful-sample output**; it only isolates failures. Bumping
the workflow version to deploy it would force a re-run of the ~240 already-good
samples in this test set, and would be absurd at 32k. Yet the code genuinely
changed and must be traceable.

## Decision

We assign the two identifiers distinct, non-overlapping roles:

- **`version` is the logical / output-contract identity.** It defines the job
  set, the completion boundary, and the reprocessing epoch. It is part of the
  job key. Changing it invalidates prior completions and triggers reprocessing.
- **`revision` is the operational code pointer** (git tag/commit). It is
  mutable, *not* part of the job key, and is recorded per run in
  `workflow_runs.revision` as the provenance of record.

The rule for choosing between them:

> **Bump the `version` if and only if re-running an already-successful sample
> would produce a different output. Otherwise it is a hotfix: update the
> `revision` in place and re-run only failed/incomplete jobs.**

Concrete procedures:

- **Hotfix** — failure handling, retry/`errorStrategy`, resource sizing,
  scheduler/infra config, logging, and bug fixes confined to the failure path.
  → `PATCH /workflows/{pk}/revision` to the new tag/commit, then requeue
  dead-letter / reset failed jobs. Completed jobs are untouched. This is the
  path taken for PR #68.
- **New version** — tool or reference-DB upgrades, parameter/algorithm changes,
  output-schema changes, and correctness bugs that change results. → register a
  new `(workflow_id, version)` and reconcile → reprocess the corpus.

**Numbering convention (recommended, refinable):** the telemetry `version`
tracks the *output-contract epoch*, not every pipeline patch. The pipeline keeps
ordinary semver tags (`2.0.6`, `2.0.7`, …); telemetry advances the epoch's
`revision` on operational/patch releases and bumps the `version` only on an
output-affecting release. This keeps version bumps rare and meaningful (they
mean "reprocess") and avoids a confusing `version 2.0.6 / revision 2.0.7`
mismatch by letting the version be the coarser epoch label.

## Alternatives considered

- **Reprocess on every code change (version-only, no mutable revision).**
  Simplest model and gives exact per-version reproducibility, but reprocesses
  the whole corpus for changes that don't alter outputs, and spuriously resets
  completion. Prohibitive at scale. Rejected.
- **Put `revision` in the job key.** Makes every code change a reprocess (same
  cost problem) and conflates operational and logical identity. Rejected.
- **A per-release `output_affecting` flag that mechanically drives the choice.**
  Still requires the same human judgment to set the flag; adds machinery without
  removing the essential decision. The criterion above is the essence; a flag or
  release-metadata field can be layered on later.

## Consequences

- **No mass reprocess for operational fixes** — the property that makes the
  system viable at 10k–100k+ samples.
- **Completion metrics stay correct across hotfixes** (the version, and thus the
  job set, is unchanged).
- **`version` is not a perfect reproducibility guarantee.** Within one version,
  samples may have run on different revisions (pre/post hotfix). The
  reproducibility record of truth is `workflow_runs.revision`, not the version.
  A published dataset must cite the revision(s), or be re-run to a single
  revision before publication.
- **Partial-reprocess gap.** If a change believed operational turns out to alter
  *some* samples' outputs, only those should be reprocessed — but there is no
  first-class "reprocess this subset under the same version" operation today; it
  requires manually resetting the affected jobs. **Rule of thumb: when in doubt,
  bump the version.**
- Requires discipline in applying the criterion; mis-classifying an
  output-changing fix as a hotfix silently mixes outputs under one version.
- **Follow-up work:** surface the per-sample revision in the UI ("which code
  produced this output"); consider a first-class "reprocess subset" operation;
  ratify the epoch-numbering convention above.

## References

- `src/nextflow_telemetry/db.py` — workflow registry (revision intentionally
  mutable; job key excludes revision) and `workflow_runs.revision`.
- Issue #116 — completion measured per active workflow version.
- `curatedMetagenomicsNextflow` PR #68 — the failure-isolation hotfix that
  motivated formalizing this policy.
- `curatedMetagenomicsNextflow` ADR-0010 — retry / `errorStrategy` policy.
