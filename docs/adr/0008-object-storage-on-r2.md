# 0008. Keep all cloud object storage on Cloudflare R2

- **Status:** Accepted
- **Date:** 2026-10-03
- **Deciders:** Sean Davis

## Context

Object storage is split across two clouds:

- Pipeline outputs go to GCS. Production runs `-profile alpine,gcs`, which
  publishes to `gs://cmgd-data/results/cMDv<version>/`
  (`curatedMetagenomicsNextflow` `conf/profiles/gcs.config`, its ADR 0011).
  GCS dates from the Cloud Run era of this service.
- Everything built since is on R2: the v2 control plane writes its ledger,
  snapshots, logs and telemetry to the `nf-telemetry` bucket
  ([0006](0006-cloudflare-control-plane.md)); the shared DuckLake data lives
  in `cdsci-lake`; the OmicIDX SRA parquet that metacurator's discovery reads
  is served from R2.

[`storage-layout.md`](../storage-layout.md) proposed moving everything to R2,
but it is a draft and nothing has moved.

Forces:

- **Egress.** onclappc02 sits behind the campus firewall and pulls outputs for
  the catalog ETL; outside users download outputs and the published lake. GCS
  bills every byte out; R2 bills none.
- **Public reads.** R2 public access is HTTPS GET on known keys with no
  anonymous LIST. The catalog enumerates every file, so DuckDB and browsers
  never need to list (`storage-layout.md`, "Discovery").
- **No migration debt.** [0007](0007-readset-identity.md) re-runs every sample
  under readset ids, so the existing GCS outputs do not need to be copied.
- **One credential story.** R2 keys (`cdsci-r2-access-key-id`,
  `cdsci-r2-secret-access-key`, `cdsci-r2-account-id`) already live in GCP
  Secret Manager next to the Cloudflare API tokens.

## Decision

We will keep all cloud object storage for these projects on **Cloudflare R2**.

- Buckets follow `storage-layout.md`: `cdsci-lake` (DuckLake data),
  `cmgd-raw` (pipeline outputs and other derived, re-creatable artifacts),
  `cmgd-public` (curated, stable URLs), `cdsci-backups` (Object Lock),
  `nf-telemetry` (control plane, [0006](0006-cloudflare-control-plane.md)).
- Pipeline outputs publish to
  `cmgd-raw/<workflow_id>/<workflow_semver>/<readset_id>/<step>/…`, the
  layout in `storage-layout.md` with the readset id from
  [0007](0007-readset-identity.md) as the sample key. The pipeline writes
  through R2's S3 API.
- Discovery outputs from metacurator go to
  `cmgd-raw/discovery/<sra_snapshot_date>/<target>/`. The first set,
  `cmgd-raw/discovery/2026-05-01/cmd/`, was uploaded on 2026-10-03.
- GCS gets no new writes. `gs://cmgd-data` stays readable until the v2
  re-run has replaced its outputs, then it is deleted.
- The cold-archive destination stays open, as `storage-layout.md` already
  allows (`archive_location` may name R2 Infrequent Access or a GCS/S3
  archive class). This decision covers hot storage.

## Alternatives considered

- **Stay on GCS.** Rejected: egress charges on every download to onclappc02
  and to outside readers, and a second cloud beside a control plane and lake
  that are already on Cloudflare.
- **Write to both during a transition.** Rejected: two copies to keep
  consistent with nothing gained, since 0007 already forces a full re-run.
- **Copy the existing GCS outputs to R2.** Rejected for the same reason: the
  outputs are keyed by the old md5 `sample_id` and will be regenerated.

## Consequences

- `curatedMetagenomicsNextflow` needs an R2 storage profile (S3 endpoint
  `https://<account>.r2.cloudflarestorage.com`, path-style access, keys from
  Secret Manager) used in place of `gcs`. The Nextflow driver (the SLURM
  wrapper job) needs write keys at run time. It uses the
  existing R2 S3 keys `cdsci-r2-access-key-id`, `cdsci-r2-secret-access-key`
  and `cdsci-r2-account-id` (the same keys as the operators' rclone `r2:`
  remote), installed as `~/.nf_tel.r2`. They are account-wide, so they can
  write to every R2 bucket on the account; we accept that on the clusters. A bucket-scoped key
  (`cmgd-r2-write-token` in `storage-layout.md`) can replace them later
  without changing the pipeline profile.
- Publishing to R2 needs Nextflow >= 25.04: 24.04's nf-amazon sends writes
  to `s3.<location-hint>.amazonaws.com` instead of the R2 endpoint
  (pipeline ADR-0015). Alpine runs a 25.10.8 launcher on Java 18. Anvil only
  has Java 8/11, which caps Nextflow at 23.10.1; its daemon has been stopped
  since 2026-10-03 and must get Java 17+ and `anvil,r2` before it dispatches
  again, so no new outputs go to GCS.
- `cmgd-raw` was created private on 2026-10-03. Turning on public read, as
  `storage-layout.md` intends, is a separate step once there are outputs to
  serve.
- Google Batch runs (`-profile google`) lose their storage co-location and
  would pay egress to R2. They are not used in production.
- `storage-layout.md` is no longer the only record of the R2 choice; its
  framing point 1 is this ADR.

## References

- [`storage-layout.md`](../storage-layout.md) — buckets, lifecycle, discovery
- [`output-catalog-etl-plan.md`](../output-catalog-etl-plan.md) — R2-backed lake
- [0006](0006-cloudflare-control-plane.md), [0007](0007-readset-identity.md)
- `curatedMetagenomicsNextflow` `conf/profiles/gcs.config`, ADR 0011
- R2 public buckets: https://developers.cloudflare.com/r2/buckets/public-buckets/
