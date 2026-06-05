---
name: cmgd-release
description: >
  Cut and roll out a new cmgd_nextflow pipeline version end-to-end — bump
  manifest.version, update CHANGELOG, tag + GitHub release, register the revision
  in the telemetry workflow registry, retire the prior revision, and reconcile to
  dispatch. Use when the user says "cut a release", "ship 2.0.x", "bump the
  version", "register the new workflow version", or "roll out" a pipeline change.
  Trigger words: release, cut, bump version, manifest version, tag, gh release,
  register workflow, retire, reconcile, roll out, ship.
---

# cmgd release & rollout

End-to-end procedure to ship a new `cmgd_nextflow` version. Two repos are
involved: the pipeline (`curatedMetagenomicsNextflow`) and the telemetry backend
(this repo, via the API in `telemetry-api`).

## The lockstep invariant

**`manifest.version` (in `nextflow.config`) == the git tag == the workflow
`revision` the orchestrator dispatches.** All three move together. If they drift,
published GCS paths (`.../cmgd_nextflow/<version>/...`) and the telemetry join
break silently. This is the single most important rule.

## Procedure

### 1. Pipeline repo (`curatedMetagenomicsNextflow`)
1. Land the code change on `main` (or a release branch merged to main).
2. Bump `manifest { version = 'X.Y.Z' }` in `nextflow.config`.
3. Add a `CHANGELOG.md` entry under `## [X.Y.Z] - YYYY-MM-DD` (Keep a Changelog
   format: `Added` / `Changed` / `Fixed` / `Documentation`). Append the compare
   link at the bottom:
   `[X.Y.Z]: https://github.com/seandavi/curatedMetagenomicsNextflow/compare/<prev>...X.Y.Z`
4. If the change alters runtime behavior (retry policy, errorStrategy, profile
   semantics, storage layout), record an ADR in `docs/adr/` — use the
   `adr-author` skill.
5. Commit, then tag and release:
   ```bash
   git tag X.Y.Z && git push origin X.Y.Z
   gh release create X.Y.Z --title X.Y.Z --notes-from-tag   # or --notes "<changelog body>"
   ```

### 2. Telemetry registry (the API)
The workflow row's `revision` is what daemons check out. Update it and flip
lifecycle status so only the new version dispatches.
```bash
API=https://nf-telemetry.cancerdatasci.org
curl -s "$API/api/workflows" | python3 -m json.tool        # find pks + current revisions

# Point the live row at the new tag (if reusing a row) ...
curl -s -X PATCH "$API/api/workflows/<pk>/revision" -H 'content-type: application/json' \
  -d '{"revision":"X.Y.Z"}'

# ... or, if a fresh row was registered for X.Y.Z, RETIRE every older revision:
curl -s -X PATCH "$API/api/workflows/<old_pk>/status" -H 'content-type: application/json' \
  -d '{"status":"retired"}'
```
Goal state: exactly one `active` `cmgd_nextflow` row, at revision X.Y.Z.

### 3. Dispatch
```bash
curl -s -X POST "$API/api/admin/reconcile-jobs"            # -> {"jobs_created":N}
curl -s "$API/api/workflows/<pk>/job-summary"              # N pending
curl -s "$API/api/admin/dispatchability"                   # cmgd NOT in stuck list
```
Then poll job-summary: `pending → claimed → submitted → running`. Daemon claims on
its next poll cycle.

## Pre-flight checklist (before reconcile)
- [ ] Exactly one active cmgd_nextflow row, revision == new tag.
- [ ] Old revisions retired (else cross-product double-dispatches every sample).
- [ ] Daemon up, fresh heartbeat, `dispatch.workflow_id` includes `cmgd_nextflow`
      (`GET /api/daemons/`).
- [ ] Cluster client yaml `profile` correct for this revision (see Gotchas).
- [ ] Reference DBs present in `store_dir` (e.g. SGB2GTDB table) if the new
      version added a step.

## Gotchas

- **Retire-before-reconcile.** `reconcile-jobs` = samples × *active* workflows.
  Two active revisions ⇒ every sample dispatched twice.
- **Profile flips are revision-gated.** `-profile alpine,gcs` is only safe at
  revision **2.0.4+** — earlier `gcs.config` forced a `gs://` workDir on SLURM.
  Coordinate the registry revision bump before changing the client profile.
- **Reconcile doesn't kill in-flight runs.** Bumping a revision mid-batch leaves
  older runs running; they finish (or fail) on the old revision. Don't expect them
  to "switch."
- **Deployed telemetry server caches modules at startup.** If the release includes
  *server* changes (not just pipeline), restart uvicorn after the deploy.
- A job only reaches `completed` via the `MARK_COMPLETE` sentinel — verify the
  pipeline still emits it if you touched the DAG tail.
