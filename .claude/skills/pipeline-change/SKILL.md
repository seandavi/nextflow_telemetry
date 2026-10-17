---
name: pipeline-change
description: >
  Checklist of rules and known edge cases for changing or adding a cmgd pipeline,
  pipeline version, registration, sample source, cluster, or deliverable/release —
  each item tied to the incident or issue that taught it. Use BEFORE coding a pipeline
  change, cutting a tag, registering or re-scoping a workflow, adding studies,
  reconciling, enabling a cluster, or shipping results. Trigger words: new pipeline,
  new version, add a step, new registration, register workflow, add studies, widen
  scope, reconcile, enable cluster, deliverable, release, roll out, checklist.
---

# Pipeline change checklist

Work through the sections that apply, in order. Each rule says why (the issue or
incident behind it). `cmgd-release` has the tag/register commands; `telemetry-api`
the endpoints; `cmgd-config` where each setting lives. If you hit a new edge
case, add it here in the same form.

## 1. Design the change

- [ ] **New params ⇒ new version.** A registration's params are fixed (ADR-0010);
      re-POSTing a version with different params is refused. Changing a tool
      flag (e.g. MetaPhlAn `--read_min_len`) is a new version or workflow id.
- [ ] **Different databases ⇒ different dataset.** Profiles on different MetaPhlAn
      indexes (vOct22 vs vJan26) are not comparable. Use a separate workflow id,
      and label every deliverable and release with it (#303, #304).
- [ ] **Check the real DAG, not the step names.** `metaphlan_unknown_viruses_lists`
      *is* the main MetaPhlAn run (profile + mapout + SAM), and `MARK_COMPLETE`
      waits on it. Trace what gates `MARK_COMPLETE` (`main.nf` tail) before
      deciding a step is optional (#299).
- [ ] **`IGNORE` on a gating step ⇒ dead letter, not partial success.** The sample
      never reaches `MARK_COMPLETE`, the sweep fails the job, and it dead-letters
      after `max_retries` full reruns. Deterministic failures should not retry
      (#115, #299).
- [ ] **Short reads.** MetaPhlAn needs reads ≥ 70 bp after KneadData. 2013–2015
      Illumina studies (LeChatelierE_2013, NielsenHB_2014, QinN_2014, maybe
      ZhangX_2015) fail entirely (#299). Check read length before adding old studies.
- [ ] **Tool version pairings come from upstream, then get tested.** e.g. BAQLaVa
      1.2.0 checks only HUMAnN's major version; its tested pairing is 4.0.0a2
      (#117). HUMAnN 4.0.0a1 needs MetaPhlAn 4.1.1 + vOct22; HUMAnN 3.9 needs
      vJun23 (`conf/humann_bundles.config`).

## 2. Code, image, databases

- [ ] **Pins live in one place.** Containers/versions/indexes in
      `conf/metaphlan_profiles.config` and `conf/humann_bundles.config`; add an
      entry there, don't inline a container.
- [ ] **Reported ≠ pinned version.** MetaPhlAn 4.2.6 prints `4.2.5` from
      `--version`; manifests inherit that (cMN#120). Record the pinned version for
      anything downstream reads.
- [ ] **New step ⇒ manifest + gate.** Add its `versions.yml` to
      `versions_per_sample`, and decide explicitly whether it gates `MARK_COMPLETE`.
- [ ] **Every cluster needs the databases first.** Stage with
      `--databases_only` (and the registration's params) on each cluster that will
      claim, *before* registering. Check the Ocean/project quota first (Bridges-2:
      2 TB for store + SIF + run dirs).
- [ ] **Cluster profiles must exist in the tag.** `-profile <cluster>,r2` fails if
      `conf/profiles/<cluster>.config` isn't in the registered revision. The submit
      template overrides `store_dir`/bind from `NF_TEL_STORE`, so hardcoded paths
      in the profile don't matter.
- [ ] **Test on the target cluster** with 2–3 real samples end to end before
      registering at scale; record core-h, peak memory and disk per sample.

## 2b. bioBakery tools (HUMAnN, BAQLaVa, MetaPhlAn)

These tools are the least predictable part of the pipeline. Treat each as a
black box behind a pinned image, and assume nothing about its defaults.

- [ ] **Pass every database path explicitly; never rely on defaults.** HUMAnN's
      default databases are the bundled `_DEMO` sets, configured in `humann.cfg`
      inside site-packages. The only demo guard is an input-size check, so a small
      real input runs against demo data and silently gives wrong answers.
- [ ] **Grep the task output for `_DEMO`.** BAQLaVa's run log prints the HUMAnN
      database folders. If any says `_DEMO`, the task is wrong even if it exited 0.
- [ ] **Wrappers don't forward database options.** BAQLaVa calls the `humann`
      CLI through anadama2 without `--nucleotide-database` or
      `--utility-database`. That is why its own depletion step is skipped
      (`--humann-depleted-fasta` fed from our `humann` task, ADR-0020, 2.4.0) and
      `--utility-database` goes in through its passthrough options (2.4.1).
- [ ] **Passthrough options replace the defaults; they don't merge.** Setting
      `--humann-passthrough-parameters-*` drops BAQLaVa's tuned values, so copy
      them from `baqlava/configs/baqlava.cfg` at the pinned commit and re-check
      them when the pin moves. Verify with `baqlava ... --dry-run` in the image:
      it prints every `humann` command.
- [ ] **HUMAnN 4.0.0a2 checks database versions by filename substring.** Protein
      DB files must contain `2019_06` and ChocoPhlAn files `SGB`, so any non-UniRef
      DIAMOND DB fails (BAQLaVa's did). Workaround: symlink the file with the tag
      appended (2.4.2). Expect an alpha bump to move these checks again (a2 dropped
      a1's MetaPhlAn version check and added this one).
- [ ] **MetaPhlAn DB ↔ ChocoPhlAn must match exactly** (`humann_bundles.config`).
      `--version` output isn't the pinned version (cMN#120).
- [ ] **Real errors are buried.** anadama2 exits with `RunFailed`; the cause is in
      `.command.out` under "Task N failed". Read it via `/api/task-logs`.
- [ ] **Outputs are named after the input file.** BAQLaVa strips
      `_bowtie2_unaligned.fa`, so link the input as `<sample>_bowtie2_unaligned.fa`
      to get `<sample>_BAQLaVa_profile.txt`. HUMAnN 4 numbers its tables
      (`out_2_genefamilies`, `out_3_reactions`, `out_4_pathabundance`); a
      HUMAnN 3 reader won't find them.
- [ ] **Smoke test with real-sized input, on the cluster.** The demo-sized test
      passes all of the above. Each of the three BAQLaVa bugs (2.4.0 → 2.4.2) only
      showed on a real sample, roughly 1 h per round trip on Bridges-2.

## 3. Tag and register

- [ ] **Lockstep:** `manifest.version` == git tag == registration `revision`.
- [ ] **Don't move a live registration's revision mid-campaign.** pk 7 ended up
      with samples from both 2.3.0 and 2.3.2 under one registration (found by
      `nf-etl deliver`, #305). Prefer a new version; if you must PATCH, note it in
      the registration description and expect mixed provenance.
- [ ] **One active version per workflow id** (enforced). Retire the old version
      before reconciling a new one of the same id, or every sample runs twice.
- [ ] **Re-POST the whole registration** to change `collections` (upsert replaces
      the list; params must be identical). Read the live row first and append.
- [ ] **ETL needs to know the registration.** `nf-etl` (ingest/publish/deliver)
      only accepts registrations in `src/nextflow_telemetry/etl/specs.py`; add the
      entry and any new output readers with the pipeline change.

## 4. Add samples

- [ ] **The add-study approval flow merges every run under an SRA sample.**
      Studies mixing metagenome + metatranscriptome runs get both in one input
      (#294); register those with a WGS-only TSV via `nf-client add-samples`.
- [ ] **The amplicon warning can be a false positive** (`library_selection=PCR`
      on WGS, #293). Check the paper.
- [ ] **cMD curation TSVs can lack run accessions** (HMP_2019_ibdmdb, VilaAV_2018:
      0 runnable). Olm may have the runs.
- [ ] **Overlaps dedupe by readset.** The same runs under two collections (cMD
      name + BioProject) produce one job for a readset-keyed registration; expect
      `jobs_created` < samples registered.
- [ ] **Sample metadata for the requester** comes from `lake.cmgd_meta`; refresh
      `nf-meta load cmd` / `load xref` for new studies.

## 5. Dispatch

- [ ] **Reconcile at scale.** It was quadratic until #297; time it after large
      registrations and watch for 500s at ~30 s (Durable Object limit, #298).
- [ ] **No priority.** `claimBatch` takes the oldest pending workflow first
      (#298). To run something first: scope it narrowly first, gate a cluster with
      `dispatch.workflow_id`, or pause the other registration.
- [ ] **Cluster quirks** (alpine-daemon skill, `docs/hpc-layout.md`): Alpine tasks
      need `task_qos: cpu-normal` (#285); Bridges-2 rejects >2000 MB/core and
      charges `bio260102p`; disk ≈ `batch_size × max_concurrent_runs × 50 GB` +
      store.
- [ ] **Deploying a daemon with active runs** needs `FORCE=1`; `deploy-clients`
      skips such clusters and exits 1.

## 6. First runs

- [ ] Run `health_report.py`; then read failure signatures yourself. Its
      `metaphlan*` "OOM" flag counts every exit code (#295).
- [ ] **Watch dead letter per collection**, not just the count: a study whose
      samples all dead-letter is a pipeline problem, not SRA noise (#299). v2 has no
      DLQ listing yet; join `jobs?status=failed` with
      `/metrics/processes/tasks?status=FAILED`.

## 7. Ship results

- [ ] **Fast hand-off = deliverable**, not a release: `nf-etl deliver`
      (`docs/etl-runbook.md`), scoped to collections, dated, 90-day expiry, may be
      deleted (#302). Check its README's versions table for mixed versions.
- [ ] **Durable = release** per registration (`nf-etl publish`, #296); settle DOI
      and supersession questions first (#304).
- [ ] Tell requesters what's *missing*: `coverage.tsv` per study, and known gaps
      (short reads, no viral output, non-comparable databases).
