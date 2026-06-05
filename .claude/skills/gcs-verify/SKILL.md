---
name: gcs-verify
description: >
  Confirm that cmgd pipeline outputs were actually published to Google Cloud
  Storage, and locate them. Use after a run completes to verify GCS publish, when
  checking "did the outputs land", or when listing/finding result objects for a
  sample or version. Trigger words: GCS, did it publish, outputs in bucket,
  check the bucket, rclone, gs://, where are the results, output directory.
---

# Verify cmgd GCS outputs

cmgd publishes results to GCS when run with a `gcs`-bearing profile
(`-profile alpine,gcs`). This skill confirms they landed and finds them.

## Output path layout

```
gs://cmgd-data/results/cMDv<cmgd_version>/<workflow-name>/<workflow-version>/<sample>/<branch>/<step>/
```
- `cmgd_version` = `4` (the cMD data release) → `cMDv4`
- `workflow-name` = `cmgd_nextflow`, `workflow-version` = e.g. `2.0.6`
- `<branch>` = full-depth vs rarefied dual-profiling branch
- per-step subdirs, e.g. `gtdb/`, plus `.command*` files alongside outputs

Example: `gs://cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6/<sample>/.../gtdb/`

## Check (on Alpine — `rclone` only)

```bash
# There is NO gsutil/gcloud on Alpine. Use the rclone gs1: remote.
rclone ls    gs1:cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6
rclone lsf   gs1:cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6/        # dirs (samples)
rclone size  gs1:cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6
# Did the GTDB step publish for any sample?
rclone ls gs1:cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6 | grep -i gtdb
```

## Gotchas

- **`rclone gs1:` is the only GCS tool on Alpine** — neither `gsutil` nor
  `gcloud` is installed. From a machine that has them, the equivalent is
  `gsutil ls gs://cmgd-data/results/cMDv4/cmgd_nextflow/2.0.6`.
- **Public access to these buckets/R2 is HTTPS GET only — no anonymous S3 LIST.**
  You cannot "browse" the bucket unauthenticated; discovery is via the app-layer
  artifacts catalog or an authenticated rclone/gsutil. Don't expect anonymous
  listing to work.
- **GCS project is `curatedmetagenomicdata`** (not `omicidx-338300`, an old
  value). Wrong project = permission errors.
- **The version in the path is `manifest.version`.** If outputs aren't where you
  expect, the run's manifest.version may not match the tag you think it ran (see
  the lockstep rule in `cmgd-release`).
- Publish is `mode = copy` (not symlink) because symlinks aren't meaningful across
  cloud storage. A run that completed locally but used a non-`gcs` profile will
  have NO GCS objects — check the run's profile, not just its status.
- Cross-check completion with telemetry: a run can be `completed` while a single
  step (e.g. `gtdb`) failed under `errorStrategy=finish` — verify the specific
  step's subdir exists, not just the sample dir.
