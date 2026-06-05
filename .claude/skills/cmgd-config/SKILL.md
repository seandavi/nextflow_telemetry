---
name: cmgd-config
description: >
  Map of where configuration lives across the cmgd system (pipeline + telemetry
  server + nf-client) and how to change each setting safely. Use when you need to
  change a path, profile, resource, database URL, credential, store_dir,
  publish location, or any "where do I set X?" question. Config is deliberately
  spread across several files — this skill is the index. Trigger words: config,
  where is X configured, store_dir, publish_dir, profile, env var, SQLALCHEMY,
  credentials, settings, change a setting, which file.
---

# cmgd configuration map

Config is spread across three components and several files (a known wart). This
is the authoritative index of what lives where and how to change it.

## 1. Pipeline (`curatedMetagenomicsNextflow`)

### `nextflow.config` (top level — params + manifest)
- `params.store_dir = 'databases'` — default reference-DB cache (overridden per
  profile; Alpine points it at a project path).
- `params.sgb2gtdb_url` — SGB→GTDB table URL; **must match the active
  metaphlan_index**. Downloaded once into `store_dir`.
- `params.publish_base_dir` — stable site prefix. The workflow derives the real
  output dir at runtime as `<publish_base_dir>/<workflow-name>/<workflow-version>`.
- `params.publish_dir = null` — escape-hatch full override of the derived path.
- `params.publish_mode = 'copy'` — copy (not symlink); required for GCS.
- `params.cmgd_version = '4'` — the cMD data-release major (in the GCS path).
- `manifest { version }` — **the release version; keep == git tag == registry
  revision** (see `cmgd-release`).
- Bottom: `includeConfig` lines pull in every `conf/profiles/*.config`.

### `conf/base.config` — process resources & error handling
- `errorStrategy` (`finish` since 2.0.4), retries, per-`withName` cpu/mem/time.
- Change resource escalation here (`memory = { N.GB * task.attempt }`).
- **No `time > 24h`** on Alpine — escalate memory, not wall-time.

### `conf/profiles/*.config` — per-environment overrides
Selected via `-profile`, composable (`alpine,gcs`):
- `alpine.config` — SLURM executor, `process.time='24h'` (Alpine requires it),
  `workDir='work'`, `params.store_dir='/projects/seda0001_amc/cmgd/store'`,
  singularity bind mount.
- `anvil.config` — sibling SLURM profile for Anvil.
- `gcs.config` — **split (ADR-0011)**: `gcs` publishes OUTPUTS to GCS only
  (`publish_base_dir=gs://cmgd-data/results/cMDv${cmgd_version}`,
  `google.project='curatedmetagenomicdata'`); `gcswork` *additionally* routes
  workDir to `gs://cmgd-data/work` (cloud compute only — never on SLURM).
- `local`, `dev`, `google`, `unitn`, `rollback` — other targets.

## 2. nf-client (`client-<cluster>.yaml`, on the cluster, NOT in repo)

Schema = `packages/nf_client/src/nf_client/config.py`; annotated example =
`packages/nf_client/client-example.yaml`. Reloaded every poll cycle.
- `server_url`, `weblog_url` — API + weblog endpoints.
- `profile` — the Nextflow `-profile` passed to every run on this cluster.
- `continuous`, `dispatch.{batch_size,workflow_id,workflow_version}`.
- `submission.{mode,template_path,max_concurrent_runs,slurm_export_none,defaults}`.
- `submission.defaults` — template variables (mem, cpus, time, account, partition,
  credentials, `client_env_setup`). May hold credential paths → stripped from any
  config echo.
See `alpine-daemon` for the full field rundown.

## 3. Telemetry server (env vars → `config.py` `Settings`)

Set in the deploy environment — on onclappc02 via the compose `env_file:`/`environment:`
(`deploy/onclappc02/.env` + `.env.secrets`), not committed to the repo:
- `SQLALCHEMY_URI` — DB DSN. `postgresql://` is auto-upgraded to
  `postgresql+asyncpg://`. Default dev: local postgres `cmdg_dev`.
- `CORS_ORIGINS`, `FRONTEND_URL`, `SESSION_COOKIE_DOMAIN`.
- `OAUTH_CLIENT_ID/SECRET`, `OAUTH_REDIRECT_URI`, `SESSION_SECRET` — auth.
- `DISPATCH_TOKEN` — dispatch auth.
- `TELEMETRY_SKIP_DB_INIT` — skip schema init (used by unit tests).

## "Where do I change…?" quick table

| Want to change | File / place |
|---|---|
| Reference-DB cache location | `conf/profiles/<cluster>.config` → `params.store_dir` |
| Where outputs are published | `params.publish_base_dir` (or `gcs` profile) |
| GCS project | `conf/profiles/gcs.config` → `google.project` |
| Per-process mem/cpu/time | `conf/base.config` (`withName:`) |
| Retry / errorStrategy | `conf/base.config` |
| Which profile a cluster uses | `client-<cluster>.yaml` → `profile` |
| Batch size / workflow filter | `client-<cluster>.yaml` → `dispatch.*` |
| SLURM account/partition/mem for the driver | `client-<cluster>.yaml` → `submission.defaults` |
| Database connection | `SQLALCHEMY_URI` env var on the server |
| Release version | `manifest.version` + git tag + registry revision (lockstep) |

## Gotchas

- **The same setting can exist in two layers.** Pipeline `params.store_dir` (for
  Nextflow) is *not* the client yaml; the client yaml's `defaults` feed the submit
  *template*. Know which layer you're editing.
- **`publish_dir` vs `publish_base_dir`:** prefer `publish_base_dir` (keeps the
  version in the path). `publish_dir` overrides the whole thing and breaks the
  versioned layout — escape hatch only.
- **`-profile` order matters and composes:** `alpine,gcs` = Alpine SLURM + GCS
  publish, local workDir. Adding `gcswork` would (wrongly) push workDir to GCS on
  SLURM. Don't.
- **Secrets belong in GCP Secret Manager**, not static config files; `.env`/yaml
  values are derived from SM. Rotate by versioning SM first.
- **Server caches modules at startup** — restart uvicorn after changing anything
  the server reads at import time.
