# 0010. Registrations are bundles

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** Sean Davis

## Context

Pipeline 2.3.0 (`curatedMetagenomicsNextflow` `main`, its ADR-0016 and
ADR-0018) can run three configurations from one tag. `humann_bundle` picks a
HUMAnN release together with the MetaPhlAn profile it can read, and
`skip_humann` turns HUMAnN off. The outputs differ, and each configuration
costs a full pipeline run per sample.

Until now a registered workflow was a repository and a revision. Every run
used the pipeline's default params, so one tag could only be registered one
way. Reconcile created a job for every sample against every active
workflow, so a costly configuration could not be limited to a pilot.

[ADR-0004](0004-workflow-version-vs-revision.md) makes `version` the output
contract: bump it when re-running a successful sample would give a different
output. Pipeline params change outputs as much as code does.
[ADR-0008](0008-object-storage-on-r2.md) and #220 publish results under
`cmgd-raw/<workflow_id>/<version>/<sample>/`, keyed by the registration and
not by the pipeline's manifest version.

## Decision

A registration is one pipeline configuration, a **bundle**.

- **workflow_id per bundle.** Each configuration gets its own `workflow_id`.
  Results, job sets and completion stay separate per registration.
- **version is the output epoch** (ADR-0004), as before.
- **params are pinned per registration.** `workflows.params` is a flat JSON
  object of string, number and boolean pipeline params. It is part of the
  output contract, so it is fixed for a `(workflow_id, version)`. Re-registering
  with different params is refused with 409; changing them means a new
  version. The claimed batch carries `params`, the submit template writes them
  to `params.json` and passes `-params-file params.json`. Params the
  orchestrator owns (`--metadata_tsv`, `--run_name`, `--publish_dir`) stay on
  the command line, where they override the file.
- **collection scope.** `workflows.collections` is an optional list of
  collection ids. Reconcile creates jobs only for samples in those
  collections; null means every sample. This is how a pilot stays a pilot.
- **Results are keyed per registration**
  (`cmgd-raw/<workflow_id>/<version>/<sample>/`, ADR-0008, #220), so two
  bundles of one tag never share a prefix.

The 2.3.0 registrations:

| workflow_id | version | pipeline params | scope |
|---|---|---|---|
| `cmgd_humann3.9` | 2.3.0 | `humann_bundle=humann3.9`, `skip_humann=false` (HUMAnN 3.9 on MetaPhlAn 4.1.1 vJun23; main pass mpa4.2.2 vJan25) | corpus-wide |
| `cmgd_humann4a1` | 2.3.0 | `humann_bundle=humann4.0.0a1`, `skip_humann=false` (MetaPhlAn 4.1.1 vOct22) | pilot collections only |
| `cmgd_mpa4.2` | 2.3.0 | `skip_humann=true` (mpa4.2.2 has no working HUMAnN) | not dispatched corpus-wide: humann3.9 already yields the mpa4.2.2 profiles; this continues the 2.2.x corpus |

For example:

```bash
nf-client register-workflow --server $S --id cmgd_humann4a1 --version 2.3.0 \
  --repo https://github.com/seandavi/curatedMetagenomicsNextflow --revision 2.3.0 \
  --param humann_bundle=humann4.0.0a1 --param skip_humann=false \
  --collection PRJNA000001
```

`--param` turns `true`/`false` into JSON booleans and integers into JSON
numbers, because Groovy reads the string `"false"` as true. Every other value
stays a string, so `4.0` is not turned into a float.

A daemon serves several bundles by listing them in `dispatch.workflow_id`
(a YAML list or a comma-separated string); see [`hpc-deployment.md`](../hpc-deployment.md).

## Alternatives considered

- **One registration per tag, params in the daemon config.** Rejected: the
  same `(workflow_id, version)` would produce different outputs depending on
  which cluster ran it, and results would share one prefix.
- **Params as mutable as `revision`.** Rejected: params change outputs, so
  changing them in place would silently mix outputs under one version, which
  ADR-0004 rules out.

## Consequences

- **Cost scales with registrations.** Each registration is a full pipeline run
  per sample in scope. Three corpus-wide registrations would triple the
  compute, so scope has to be chosen per bundle. `cmgd_humann3.9` also
  produces the mpa4.2.2 main-pass profiles, which is why `cmgd_mpa4.2` is not
  dispatched corpus-wide.
- Re-registering an existing version must repeat its params exactly. Omitted
  params mean `{}`, which is refused if the version was registered with
  params. Use `PATCH /workflows/{pk}/revision` for hotfixes (ADR-0004).
- Re-registering without `collections` keeps the existing scope, so a pilot
  cannot be widened by accident; an explicit `null` widens it to every sample.
  Narrowing the collections does not delete jobs already created; reset or
  retire them explicitly.
- Existing Durable Object storage gets the new columns at startup
  (`params` `'{}'`, `collections` null), so earlier registrations behave as
  before.

## References

- Issue #222: per-registration params and collection scope.
- Issues #220 and #221: results published under the registered workflow.
- `curatedMetagenomicsNextflow` ADR-0016 (HUMAnN bundles) and ADR-0018
  (MetaPhlAn profiles); `conf/humann_bundles.config`,
  `conf/metaphlan_profiles.config`.
- `cf/src/control-do.ts` (`registerWorkflow`, `reconcileJobs`, `claimBatch`),
  `templates/submit_slurm.sh.j2`.
