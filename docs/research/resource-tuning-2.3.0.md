# Resource tuning for the 2.3.0 corpus run, from real traces

*Analysed 2026-10-08 for issue #238 item 3. No pipeline changes are applied here. The recommendations are for curatedMetagenomicsNextflow #105 (2.3.0), and should be re-checked after the rehearsal batch (#238 item 4).*

## Summary

- **Memory, not CPU, sets the bill on Anvil.** Anvil `shared` has `MaxMemPerCPU=1896M`, so SLURM raises the core count to `ceil(mem / 1896 MiB)` and bills every one of those cores. `sacct` confirms it: MetaPhlAn at 16 cpus / 47 GiB is billed as **26 cores**, kneaddata at 8 / 30 GiB as **17**, kraken2 at 8 / 32 GiB as **18**. Alpine `acpu` also bills memory: `floor(0.5 × cores + 0.141 × GiB)`.
- **Measured cost of the base pipeline today** (121 samples, mean 46.5 M raw reads, both branches): **14.2 allocated CPU-h**, **21.1 Anvil SU** and **11.9 Alpine billing-hours per sample**. Restricted to the 34 completed samples (mean 28.7 M reads), this query gives 11.8 CPU-h, which reproduces the issue's 11.7.
- **Recommended requests** cut that to **9.7 CPU-h / 16.1 Anvil SU / 8.5 Alpine billing-h per sample**, a drop of 32 % / 24 % / 29 %. The savings come from right-sizing memory on every step except MetaPhlAn and from dropping single-threaded steps (`metaphlan_markers`, `sample_to_markers`, fastqc, KMA, bracken, rarefy) to 1–2 cpus. Outside MetaPhlAn, no recommended first-attempt request is below 1.2× the largest peak RSS observed.
- **MetaPhlAn is the exception, and 2.3.0 makes it worse.** Its RSS is set by the bowtie2 index (median 37.6 GiB even on 1 M rarefied reads), 47 GiB already OOMs 2/122 tasks on vJan25, and the vJan26 index is 19 % larger (41.7 GB vs 35.1 GB). Plan on **55 GiB** for 2.3.0 and re-measure it in the rehearsal. On Anvil, also raise MetaPhlAn's cpus to the 30 cores the 55 GiB already pays for.
- **2.3.0 estimate, tuned:** ~**11.0 CPU-h / 20.3 Anvil SU / 10.1 Alpine billing-h per sample**, against 15.5 / 25.3 / 13.6 if only the MetaPhlAn memory is raised. At 200 k samples that is **4.1 M Anvil SU instead of 5.1 M**.

## Data and method

- **Traces:** every `process_completed` event in the v2 archive (`r2://nf-telemetry/telemetry/events/dt=2026-10-03 … 2026-10-08`). That is 1,678 tasks from 5 runs: 523 tasks on Alpine and 1,155 on Anvil, covering 121 samples from 7 studies (ZhangM_2023, AsnicarF_2017, BaiX_2021, LassalleF_2017, WibowoMC_2021, ZellerG_2014, DavidLA_2015). The snapshot was taken at 2026-10-08 16:00Z, and two Anvil runs were still in flight.
- **Billing:** `sacct -X` for every task's `native_id` on both clusters (`AllocCPUS`, `AllocTRES` billing, `ElapsedRaw`). Cost = billed units × SLURM elapsed hours, counting failed attempts. The SLURM overhead over trace `realtime` is about 0.1 min per task.
- **Input size:** `read_accounting` from each sample's published `manifest.json` (`cmgd-raw/cmgd_nextflow/{2.2.1,2.2.3}/<sample_id>/`). 114 of 121 samples have one; the earliest Alpine pilot samples published before R2. Raw reads per sample: median 39.7 M, p95 90.1 M, max 116 M.
- **Requests in force:** `conf/base.config` on `origin/main` (= 2.2.3 apart from HUMAnN entries). `process.time = '24h'` comes from the cluster profiles. Memory is `baseline × task.attempt`. The `withName: 'metaphlan_unknown_viruses_lists'` selector reaches the `_full`/`_rarefied` aliases, since the retried task got 94 GiB.

### How each cluster charges (verified 2026-10-08)

| | Anvil `shared` | Alpine `acpu` / `cpu-normal` |
|---|---|---|
| Node shape | 128 cores, 251 GiB (250 nodes) | 32–128 cores, 240 GiB (Milan) / 494 GiB (Genoa) |
| `DefMemPerCPU` / `MaxMemPerCPU` | 1896 MiB / 1896 MiB | 3840 MiB / 3840 MiB |
| Cores allocated | `max(cpus, ceil(mem_MiB / 1896))` | `max(cpus, ceil(mem_MiB / 3840))` |
| Billing | `TRESBillingWeights=CPU=1.0`, so billing = allocated cores; 1 SU = 1 core-hour | `TRESBillingWeights=CPU=0.5,Mem=0.141G`, so billing = `floor(0.5 × cores + 0.141 × GiB)` |
| Memory forces extra cores? | **Yes.** Every 1.85 GiB above `cpus × 1.85 GiB` adds a billed core | Yes, above 3.75 GiB/core, but memory is billed on its own anyway |
| Example: MetaPhlAn 16 c / 47 GiB | 26 cores, 26 SU/h | 16 cores, billing 14/h |

Both formulas reproduce `AllocTRES` for every process shape in the traces. Alpine billing feeds fair-share accounting. This analysis does not establish whether `amc-general` is capped. Anvil SUs come out of the ACCESS allocation.

Corollary for Anvil: memory up to `cpus × 1.85 GiB` is free, and cpus up to `ceil(mem / 1.85 GiB)` are free. A memory-bound step should request the cores its memory already pays for if the tool can use the threads.

## Per-process observations

All attempts. The util column is `%cpu / (100 × cpus)`. RSS is in GiB, realtime is the trace `realtime` in minutes, and every task requested 24 h.

| process | tasks | failed | cpus | mem GiB | RSS p50 | RSS p95 | RSS max | util p50 | util p95 | rt p50 | rt p95 | rt max |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| metaphlan_unknown_viruses_lists_full | 122 | 2 | 16 | 47 (94 retry) | 37.6 | 42.9 | 57.2 | 61 % | 89 % | 17.5 | 44.4 | 62.4 |
| kneaddata | 121 | 0 | 8 | 30 | 3.61 | 3.66 | 3.70 | 52 % | 55 % | 10.1 | 18.3 | 25.3 |
| fasterq_dump | 121 | 0 | 8 | 16 | 0.73 | 1.03 | 1.04 | 36 % | 39 % | 9.2 | 17.7 | 24.5 |
| metaphlan_markers_full | 108 | 0 | 16 | 30 | 10.0 | 10.8 | 12.5 | 6 % | 7 % | 4.8 | 6.0 | 8.0 |
| metaphlan_unknown_viruses_lists_rarefied | 120 | 0 | 16 | 47 | 36.7 | 36.7 | 36.7 | 19 % | 25 % | 3.5 | 7.7 | 12.5 |
| sample_to_markers_full | 108 | 0 | 16 | 32 | 14.0 | 14.3 | 14.4 | 6 % | 6 % | 3.8 | 5.8 | 7.3 |
| metaphlan_markers_rarefied | 58 | 0 | 16 | 30 | 9.37 | 9.39 | 9.43 | 6 % | 7 % | 4.5 | 5.4 | 6.1 |
| sample_to_markers_rarefied | 58 | 0 | 16 | 32 | 13.5 | 13.5 | 13.5 | 6 % | 7 % | 2.0 | 3.6 | 3.8 |
| kraken2_full | 68 | 0 | 8 | 32 | 15.6 | 16.0 | 16.1 | 25 % | 75 % | 1.5 | 4.8 | 6.7 |
| resistome_kma_full | 121 | 0 | 8 | 16 | 0.11 | 0.12 | 0.13 | 23 % | 29 % | 0.7 | 2.4 | 3.6 |
| fastqc | 121 | 0 | 2 | 4 | 0.25 | 0.48 | 0.52 | 50 % | 51 % | 3.2 | 7.3 | 9.7 |
| sample_manifest | 117 | 0 | 1 | 2 | 0.03 | 0.03 | 0.03 | 99 % | 100 % | 4.5 | 9.4 | 12.2 |
| kraken2_rarefied | 58 | 0 | 8 | 32 | 15.2 | 15.2 | 15.2 | 6 % | 24 % | 0.3 | 3.3 | 5.3 |
| rarefy_fastq | 121 | 0 | 2 | 8 | 0.65 | 0.66 | 0.66 | 48 % | 50 % | 0.2 | 0.4 | 0.5 |
| resistome_kma_rarefied | 121 | 0 | 8 | 16 | 0.03 | 0.05 | 0.06 | 20 % | 25 % | 0.0 | 0.1 | 0.1 |
| bracken_full | 56 | 0 | 2 | 4 | 0.01 | 0.06 | 0.07 | 45 % | 47 % | 0.0 | 0.0 | 0.1 |
| bracken_rarefied | 45 | 0 | 2 | 4 | 0.01 | 0.01 | 0.03 | 43 % | 47 % | 0.0 | 0.0 | 0.0 |
| MARK_COMPLETE | 34 | 0 | 1 | 2 | 0.00 | 0.00 | 0.00 | 24 % | 47 % | 0.0 | 0.0 | 0.0 |

The two failures were both MetaPhlAn full-branch tasks. SLURM recorded `OUT_OF_MEMORY`, but Nextflow saw **exit 1**, not 137, because the kill hits bowtie2 inside MetaPhlAn. The `137..140` branch of the retry policy (up to 4 attempts) therefore never fires for these OOMs. They get the generic single retry (attempt 2, memory × 2). The Alpine retry succeeded at 94 GiB with a peak of 57.2 GiB. The Anvil retry was still queued at the snapshot, 3.5 h after submission, because 94 GiB on Anvil is a 50-core job.

Memory efficiency in this window (task average of `peak_rss / requested`) is 24.8 %, which matches the catalog card. Weighted by elapsed time it is 55 %, because MetaPhlAn dominates the hours and is sized close to its need.

### Relationship to input size

Linear fits on completed full-depth tasks. x is million reads: raw for fasterq_dump and kneaddata, host-decontaminated for the rest. Runtime uses the requested cpus as they are today.

| process | n | rt min at 0 | rt min / M reads | R² rt | used CPU-h / M reads | RSS GiB at 0 | RSS GiB / M reads | R² RSS |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| metaphlan_unknown_viruses_lists_full | 114 | 3.3 | 0.374 | 0.71 | 0.097 | 37.1 | 0.028 | 0.09 |
| kneaddata | 114 | 0.9 | 0.185 | 0.96 | 0.014 | 3.5 | 0.001 | 0.10 |
| fasterq_dump | 114 | 0.5 | 0.184 | 0.94 | 0.009 | 0.6 | 0.004 | 0.24 |
| sample_manifest | 114 | −0.2 | 0.106 | 0.97 | 0.002 | 0.0 | 0.000 | 0.20 |
| fastqc | 114 | 0.0 | 0.075 | 0.88 | 0.001 | 0.3 | 0.000 | 0.00 |
| sample_to_markers_full | 105 | 2.3 | 0.036 | 0.73 | 0.001 | 13.6 | 0.008 | 0.85 |
| resistome_kma_full | 114 | 0.0 | 0.018 | 0.54 | 0.001 | 0.1 | 0.001 | 0.67 |
| kraken2_full | 65 | 1.4 | 0.012 | 0.04 | 0.002 | 15.1 | 0.012 | 0.46 |
| metaphlan_markers_full | 105 | 4.8 | 0.003 | 0.02 | 0.000 | 9.5 | 0.011 | 0.45 |

- **Runtime scales with reads; memory mostly does not.** Every memory floor is a database or index loaded into RAM: the MetaPhlAn bowtie2 index (~37 GiB), the MetaPhlAn pickle for `metaphlan_markers` and `sample_to_markers` (~9.5 / 13.6 GiB), the Kraken2 hash (~15 GiB) and the human bowtie2 index (~3.5 GiB). Only `sample_to_markers` has a clear read-driven memory slope, 0.008 GiB per M reads, which adds ~1.2 GiB at 150 M reads.
- **MetaPhlAn's memory tail is not explained by depth.** Correlation of RSS with reads, bases and mean read length is 0.38–0.41. Peak RSS was above 44 GiB in 3.5 % of tasks, and the 57 GiB sample had 52 M reads. A read-count-based memory closure would not catch it. The flat request plus retry is the right shape.
- **MetaPhlAn parallelism grows with depth.** Correlation of %cpu with reads is 0.87. An Amdahl split of %cpu gives a serial share of ~47 % of wall time on the full branch (index load and post-processing) and ~87 % on the 1 M-read rarefied branch.
- **Per-sample cost vs depth.** Summing the per-process fits gives allocated cost ≈ a + b × raw M reads:

| metric | a | b | 5 M | 20 M | 46.5 M | 90 M | 120 M |
|---|--:|--:|--:|--:|--:|--:|--:|
| alloc CPU-h, current | 6.69 | 0.158 | 7.5 | 9.8 | 14.0 | 20.9 | 25.6 |
| alloc CPU-h, recommended | 3.25 | 0.134 | 3.9 | 5.9 | 9.5 | 15.3 | 19.4 |
| Anvil SU, current | 9.04 | 0.251 | 10.3 | 14.1 | 20.7 | 31.6 | 39.1 |
| Anvil SU, recommended | 6.59 | 0.198 | 7.6 | 10.5 | 15.8 | 24.4 | 30.3 |
| Alpine billing-h, current | 5.47 | 0.134 | 6.1 | 8.2 | 11.7 | 17.5 | 21.6 |
| Alpine billing-h, recommended | 3.35 | 0.106 | 3.9 | 5.5 | 8.3 | 12.9 | 16.1 |

The marginal cost is ~0.13–0.16 allocated CPU-h per M reads, of which MetaPhlAn is ~0.10. For comparison, the HUMAnN pilot measured ~0.21 CPU-h per M reads for HUMAnN alone. The corpus read-depth distribution, not the per-sample mean of this pilot, should drive the final budget. Plug its mean into `a + b × M`. Fits beyond 116 M reads are extrapolation.

## Recommendations

Values are first-attempt requests. Keep `memory × task.attempt`, and add `time × task.attempt`. "Headroom" is the request divided by the largest peak RSS observed on vJan25. "Billed" is cores per hour on Anvil / billing per hour on Alpine.

| process | now cpus / mem | rec. cpus / mem / time | headroom | billed now → rec (Anvil; Alpine) | risk / when |
|---|---|---|--:|---|---|
| fasterq_dump | 8 / 16 GiB | **4 / 6 GiB / 6 h** | 5.8× | 9 → 4; 6 → 2 | low, apply now. Download-bound (util 36 %, p95 %cpu 314). Check that the rehearsal runtime does not grow; the SRA fallback is unmeasured |
| kneaddata | 8 / 30 GiB | **8 / 8 GiB / 3 h** | 2.2× | 17 → 8; 8 → 5 | low, apply now. RSS is the flat human index |
| fastqc | 2 / 4 GiB | **1 / 1792 MiB / 1 h** | 3.4× | 3 → 1; 1 → 0 | low, apply now |
| rarefy_fastq | 2 / 8 GiB | **1 / 1792 MiB / 1 h** | 2.7× | 5 → 1; 2 → 0 | low, apply now |
| metaphlan_markers_{full,rarefied} | 16 / 30 GiB | **2 / 16 GiB / 2 h** | 1.28× | 17 → 9; 12 → 4 | cpus: low, apply now (single-threaded, util 6 %). Memory: re-measure on vJan26 (larger pickle) |
| sample_to_markers_{full,rarefied} | 16 / 32 GiB | **2 / 20 GiB / 2 h** | 1.39× | 18 → 11; 12 → 5 | cpus: low, apply now (util 6 %). Memory: re-measure on vJan26 |
| kraken2_{full,rarefied} | 8 / 32 GiB | **8 / 20 GiB / 2 h** | 1.24× | 18 → 11; 9 → 6 | low if the Kraken2 DB is unchanged in 2.3.0. RSS = DB size, so re-measure if the DB changes |
| bracken_{full,rarefied} | 2 / 4 GiB | **1 / 1792 MiB / 1 h** | 25× | 3 → 1; 1 → 0 | low, apply now |
| resistome_kma_{full,rarefied} | 8 / 16 GiB | **2 / 3.5 GiB / 1 h** | 27× | 9 → 2; 6 → 1 | low, apply now (p50 %cpu 182). Re-check if the CARD DB grows |
| sample_manifest | 1 / 2 GiB | **1 / 1792 MiB / 1 h** | 58× | 2 → 1; 0 → 0 | low, apply now (15 min modelled at 150 M reads) |
| MARK_COMPLETE | 1 / 2 GiB | **1 / 1792 MiB / 1 h** | — | 2 → 1; 0 → 0 | low, apply now |
| metaphlan_unknown_viruses_lists_full | 16 / 47 GiB | **16 / 55 GiB / 6 h**; Anvil: **cpus 30** | see below | 26 → 30; 14 → 15 | **must re-measure on 2.3.0** |
| metaphlan_unknown_viruses_lists_rarefied | 16 / 47 GiB | **16 / 55 GiB / 2 h** | see below | 26 → 30; 14 → 15 | **must re-measure on 2.3.0**, then cut to the vJan26 floor + 10 % (its RSS spread is under 0.1 GiB) |

`1792 MiB` is chosen for the 1-cpu steps so that Anvil bills 1 core. At 2 GiB, Anvil bills 2.

**MetaPhlAn sizing for vJan26.** On vJan25 the RSS floor (37.6 GiB median) is the index (35.1 GB tar) plus ~5 GiB. The tail adds up to +8 GiB at p99 and +20 GiB at the max. With the vJan26 index at 41.7 GB, the expected floor is ~44 GiB, p99 ~52 GiB and the worst case ~63 GiB. At 47 GiB most vJan26 tasks would OOM. At 55 GiB, by this estimate, roughly the top 1–2 % do, and the exit-1 retry at 110 GiB covers them (Anvil nodes have 251 GiB, Alpine 240 GiB). These figures are an **estimate from file sizes, not a measurement**. The rehearsal's first 20–30 MetaPhlAn tasks settle it. Set full = p99 × 1.05 and rarefied = max × 1.1, rounded to whole multiples of 1.85 GiB for Anvil.

**Anvil MetaPhlAn cpus.** 55 GiB already bills 30 cores on Anvil, so requesting `cpus = 30` costs nothing extra per hour. If bowtie2's parallel share scales, the full-branch elapsed time falls from 20.2 to ≤14.1 min on average, about 3 SU per sample (upper bound from the Amdahl split of %cpu). Put this in `conf/profiles/anvil.config` only. On Alpine 30 cpus would raise billing from 15 to 22 per hour for the same uncertain speed-up. Measure it in the rehearsal before relying on it.

**Time limits.** Billing does not depend on `time` on either cluster. Shorter limits only help backfill. The values above are ≥ 3× the modelled runtime at 150 M reads, and `× task.attempt` keeps them under Alpine's 24 h `acpu` cap even at attempt 4. Whatever exit code a SLURM TIMEOUT surfaces as, the policy retries it at least once (attempt 2, 2× time). This was not observed in these traces. Treat these as low-risk but low-value. If the rehearsal shows any TIMEOUT, revert that process to 24 h.

**Retries cover the tail.** Every recommended first-attempt memory except MetaPhlAn is ≥ 1.24× the largest RSS seen, so the attempt-2 doubling (≥ 2.5×) covers any plausible outlier. Because OOMs here exit 1, there is exactly **one** retry, not four. A sample that OOMs twice is `ignore`d (dropped). If the rehearsal shows exit-1 OOMs on MetaPhlAn after the 55 GiB change, the pipeline-side fix is to make the memory-growth retry fire on exit 1 for that process. Resizing alone will not fix it. That is a policy change for #105 and is not proposed here.

### Proposed config for #105 (not applied)

```groovy
// conf/base.config, process scope: first-attempt sizes from nextflow_telemetry docs/research/resource-tuning-2.3.0.md
withName: 'fasterq_dump'                       { cpus = 4; memory = { 6.GB * task.attempt };    time = { 6.h * task.attempt } }
withName: 'kneaddata'                          { cpus = 8; memory = { 8.GB * task.attempt };    time = { 3.h * task.attempt } }
withName: 'fastqc|rarefy_fastq|bracken.*|sample_manifest|MARK_COMPLETE' {
                                                 cpus = 1; memory = { 1792.MB * task.attempt }; time = { 1.h * task.attempt } }
withName: 'metaphlan_unknown_viruses_lists.*'  { cpus = 16; memory = { 55.GB * task.attempt };  time = { 6.h * task.attempt } }
withName: 'metaphlan_markers.*'                { cpus = 2; memory = { 16.GB * task.attempt };   time = { 2.h * task.attempt } }
withName: 'sample_to_markers.*'                { cpus = 2; memory = { 20.GB * task.attempt };   time = { 2.h * task.attempt } }
withName: 'kraken2.*'                          { cpus = 8; memory = { 20.GB * task.attempt };   time = { 2.h * task.attempt } }
withName: 'resistome_kma.*'                    { cpus = 2; memory = { 3584.MB * task.attempt }; time = { 1.h * task.attempt } }

// conf/profiles/anvil.config: 55 GiB already bills 30 cores on Anvil (MaxMemPerCPU=1896M)
withName: 'metaphlan_unknown_viruses_lists_full' { cpus = 30 }
```

The process-level `cpus`/`memory` directives in `modules/processes/*.nf` are overridden by these selectors. Confirm with `nextflow config -profile anvil` that the Anvil override wins over `base.config`: both are included from `nextflow.config`, `base.config` first. Leave the `db_setup` processes alone; they run once per store.

## Projected cost

Per sample, both branches, at this pilot's depth mix (mean 46.5 M raw reads). Failed attempts are included. Rows with "est." apply the vJan26 assumptions to MetaPhlAn (55 GiB, +20 % elapsed) and are not measurements.

| scenario | alloc CPU-h | Anvil SU | Alpine billing-h |
|---|--:|--:|--:|
| A. 2.2.x, current requests (measured) | 14.21 | 21.13 | 11.92 |
| B. 2.2.x, recommended requests (MetaPhlAn kept at 47 GiB) | 9.66 (−32 %) | 16.11 (−24 %) | 8.45 (−29 %) |
| C. 2.3.0 est., current requests but MetaPhlAn 55 GiB | 15.53 | 25.29 | 13.59 |
| D. 2.3.0 est., recommended requests | 10.98 (−29 % vs C) | 20.28 (−20 % vs C) | 10.12 (−26 % vs C) |
| D + Anvil MetaPhlAn 30 cpus, if bowtie2 scales | — | ~17 (upper-bound saving) | — |

Totals (millions):

| scenario | alloc CPU-h at 35 k / 200 k / 400 k | Anvil SU at 35 k / 200 k / 400 k | Alpine billing-h at 35 k / 200 k / 400 k |
|---|---|---|---|
| A | 0.50 / 2.84 / 5.68 | 0.74 / 4.23 / 8.45 | 0.42 / 2.38 / 4.77 |
| B | 0.34 / 1.93 / 3.86 | 0.56 / 3.22 / 6.44 | 0.30 / 1.69 / 3.38 |
| C | 0.54 / 3.11 / 6.21 | 0.89 / 5.06 / 10.12 | 0.48 / 2.72 / 5.44 |
| D | 0.38 / 2.20 / 4.39 | 0.71 / 4.06 / 8.11 | 0.35 / 2.02 / 4.05 |

Where the remaining cost is (scenario B, Anvil SU per sample): MetaPhlAn full 9.0 and rarefied 1.8, markers + `sample_to_markers` 2.6 across both branches, kneaddata 1.3, fasterq_dump 0.6, kraken2 0.5, everything else < 0.3. MetaPhlAn is ~2/3 of the bill after tuning, and its memory floor is fixed by the index. The rarefied branch costs ~3.1 Anvil SU per sample after tuning, and dropping it (`skip_rarefied`) would remove that. That is a product decision outside this analysis.

**Queue wait (observation, not billed).** On Alpine, 1–8-core jobs started within ~1 min of submission, while the 16-core MetaPhlAn jobs waited a median ~130 min and markers / `sample_to_markers` ~64 min. Smaller shapes should raise Alpine throughput. On Anvil, waits were 1–2.5 h regardless of shape, which points to account or QoS limits rather than job size.

## Limits of this analysis

- **Small sample.** 121 samples (1,678 tasks) from 7 studies, all short-read Illumina, raw depth 0.04–116 M. The memory tails rest on 1–5 tasks per process, and MetaPhlAn's 57 GiB tail is a single sample. Long-read, very deep (> 120 M) or unusual libraries are not represented.
- **Partial runs.** Two Anvil runs were still running at the snapshot, so kraken2 (68), markers (108/58) and bracken have fewer samples than the 121 fetched. Per-sample costs are averaged per process to correct for that.
- **Unchanged runtime assumed.** The "after" figures keep each task's measured elapsed time. That is safe for steps that use ≤ 1 core (%cpu ≈ 100). It is optimistic for fasterq_dump at 4 cpus (p95 %cpu 314 during compression) and conservative for the Anvil MetaPhlAn 30-cpu change.
- **2.3.0 not measured.** MetaPhlAn 4.2.6 + vJan26 (72 k SGBs vs 58 k) changes the index (+19 %) and the pickle (+17 % for the DB tar). Every MetaPhlAn, `metaphlan_markers` and `sample_to_markers` number must be re-measured. The 55 GiB / +20 % figures are planning estimates.
- **Alpine pricing.** Alpine billing is shown in its own units. This analysis does not establish whether `amc-general` usage is capped or charged.

## Re-run after the rehearsal

1. Restrict the events query to the rehearsal's run names (or to `dt >=` the rehearsal start) and re-run the appendix queries. The `rec` table holds the requests; update it to whatever 2.3.0 shipped.
2. Gates before the corpus run: MetaPhlAn full p99 RSS ≤ 0.95 × request with no exit-1 OOM on attempt 2; markers / `sample_to_markers` max RSS ≤ 0.8 × request; no TIMEOUTs; fasterq_dump p50 runtime within 20 % of 2.2.x at equal depth.
3. On Anvil, compare MetaPhlAn full elapsed at 30 cpus with the 16-cpu fit (3.3 + 0.374 × M min). Keep 30 cpus only if it is lower.

## Appendix: queries

Run from a scratch directory (`export TMPDIR=/data/davsean/tmp`). DuckDB CLI ≥ 1.5. The R2 secret uses an isolated `secret_directory`, so the persistent secrets in `~/.duckdb` are never loaded.

```bash
umask 077
cat > secret.sql <<EOF
SET secret_directory='/data/davsean/tmp/ddsec-tuning';
CREATE OR REPLACE SECRET r2 (TYPE r2,
  KEY_ID '$(gcloud secrets versions access latest --secret=cdsci-r2-access-key-id --project=cdsci-infra)',
  SECRET '$(gcloud secrets versions access latest --secret=cdsci-r2-secret-access-key --project=cdsci-infra)',
  ACCOUNT_ID '$(gcloud secrets versions access latest --secret=cdsci-r2-account-id --project=cdsci-infra)');
EOF

# 1. Snapshot the v2 event archive locally.
duckdb -c ".read secret.sql" -c "
COPY (SELECT * FROM read_json('r2://nf-telemetry/telemetry/events/*/*.ndjson.gz',
        format='newline_delimited',
        columns={run_id:'VARCHAR', run_name:'VARCHAR', event:'VARCHAR', utc_time:'TIMESTAMPTZ',
                 sample_id:'VARCHAR', process:'VARCHAR', payload:'JSON'},
        filename=true))
TO 'events.parquet'"
```

```sql
-- 2. tasks.sql: one row per process_completed trace (all attempts), flattened.
CREATE OR REPLACE TABLE tasks AS
SELECT
  run_id, run_name, utc_time,
  t->>'process'                    AS process,
  t->>'tag'                        AS tag,
  t->>'status'                     AS status,
  (t->>'attempt')::INT             AS attempt,
  t->>'exit'                       AS exit,
  t->>'native_id'                  AS native_id,
  CASE WHEN t->>'workdir' LIKE '/anvil/%' OR t->>'container' LIKE '/anvil/%' THEN 'anvil'
       WHEN t->>'workdir' LIKE '%alpine%' OR t->>'container' LIKE '%alpine%' THEN 'alpine' END AS cluster,
  t->>'cpu_model'                  AS cpu_model,
  (t->>'cpus')::INT                AS cpus,
  (t->>'memory')::DOUBLE / 2^30    AS req_mem_gb,
  (t->>'time')::DOUBLE / 3.6e6     AS req_time_h,
  (t->>'realtime')::DOUBLE / 3.6e6 AS realtime_h,
  (t->>'duration')::DOUBLE / 3.6e6 AS duration_h,
  (t->>'%cpu')::DOUBLE             AS pct_cpu,
  (t->>'peak_rss')::DOUBLE / 2^30  AS peak_rss_gb,
  (t->>'peak_vmem')::DOUBLE / 2^30 AS peak_vmem_gb,
  (t->>'rchar')::DOUBLE / 2^30     AS rchar_gb,
  (t->>'wchar')::DOUBLE / 2^30     AS wchar_gb,
  (t->>'read_bytes')::DOUBLE / 2^30  AS read_gb,
  (t->>'write_bytes')::DOUBLE / 2^30 AS write_gb
FROM (SELECT run_id, run_name, utc_time, payload->'trace' AS t
      FROM read_parquet('events.parquet') WHERE event = 'process_completed');
```

```bash
# 3. Billing: sacct for every task job, per cluster (read-only; ssh aliases from ~/.ssh/config).
for c in anvil alpine; do
  duckdb tuning.duckdb -noheader -list -c "select distinct native_id from tasks where cluster='$c' and native_id is not null" > ids_$c.txt
  split -l 200 ids_$c.txt chunk_$c.; : > sacct_$c.psv
  for f in chunk_$c.*; do
    ssh $c "sacct -n -X -P -j $(paste -sd, $f) -o JobID,JobName%80,AllocCPUS,ReqMem,AllocTRES%80,ElapsedRaw,State" | grep -v '^#' >> sacct_$c.psv
  done; rm chunk_$c.*
done
# Partition limits behind the billing formulas:
ssh anvil  'scontrol show partition shared | grep -E "MemPerCPU|TRESBilling"'
ssh alpine 'scontrol show partition acpu   | grep -E "MemPerCPU|TRESBilling"'

# 4. Input size: per-sample manifest.json (public). Tags = distinct fasterq_dump tags.
duckdb tuning.duckdb -noheader -list -c "select distinct tag from tasks where process='fasterq_dump'" > tags.txt
mkdir -p manifests
while read t; do for v in 2.2.1 2.2.3; do
  [ -s manifests/$t.json ] && break
  curl -sf --max-time 20 "https://cmgd-raw.cancerdatasci.org/cmgd_nextflow/$v/$t/manifest.json" -o manifests/$t.json || rm -f manifests/$t.json
done; done < tags.txt
```

```sql
-- sacct.sql
CREATE OR REPLACE TABLE sacct AS
SELECT cluster, column0::VARCHAR AS native_id, column2::INT AS alloc_cpus, column3 AS req_mem,
       coalesce(nullif(regexp_extract(column4, 'billing=(\d+)', 1), '')::INT, 0) AS billing,
       column5::DOUBLE / 3600 AS elapsed_h, column6 AS state
FROM (SELECT 'anvil' AS cluster, * FROM read_csv('sacct_anvil.psv', delim='|', header=false, all_varchar=true)
      UNION ALL BY NAME
      SELECT 'alpine' AS cluster, * FROM read_csv('sacct_alpine.psv', delim='|', header=false, all_varchar=true));

-- reads.sql: per-sample input size from manifest.json.
CREATE OR REPLACE TABLE reads AS
SELECT j->>'sample_id' AS tag,
       (j->'read_accounting'->'raw'->>'number_reads')::DOUBLE / 1e6            AS raw_mreads,
       (j->'read_accounting'->'raw'->>'number_bases')::DOUBLE / 1e9            AS raw_gbases,
       (j->'read_accounting'->'decontaminated'->>'number_reads')::DOUBLE / 1e6 AS clean_mreads,
       (j->'read_accounting'->'decontaminated'->>'number_bases')::DOUBLE / 1e9 AS clean_gbases
FROM read_json_objects('manifests/*.json', format='auto') AS r(j);

-- observed.sql: per-process table above. util = %cpu / (100 * requested cpus).
SELECT process, count(*) AS tasks, sum((status <> 'COMPLETED')::INT) AS failed,
  min(cpus) || CASE WHEN max(cpus) <> min(cpus) THEN '–' || max(cpus) ELSE '' END AS cpus,
  min(round(req_mem_gb)) || CASE WHEN max(req_mem_gb) <> min(req_mem_gb) THEN '–' || max(round(req_mem_gb)) ELSE '' END AS mem_gib,
  round(max(req_time_h)) AS time_h,
  round(quantile_cont(peak_rss_gb, .5), 2)  AS rss_p50,
  round(quantile_cont(peak_rss_gb, .95), 2) AS rss_p95,
  round(max(peak_rss_gb), 2)                AS rss_max,
  round(quantile_cont(pct_cpu / cpus, .5))  AS util_p50_pct,
  round(quantile_cont(pct_cpu / cpus, .95)) AS util_p95_pct,
  round(quantile_cont(realtime_h * 60, .5), 1)  AS rt_p50_min,
  round(quantile_cont(realtime_h * 60, .95), 1) AS rt_p95_min,
  round(max(realtime_h * 60), 1)                AS rt_max_min
FROM tasks GROUP BY process ORDER BY sum(cpus * realtime_h) DESC;

-- Failures and their SLURM state (exit 1 + OUT_OF_MEMORY).
SELECT t.process, t.tag, t.attempt, t.status, t.exit, t.cluster, s.state, round(t.req_mem_gb) AS req_mem, round(t.peak_rss_gb, 1) AS rss
FROM tasks t JOIN sacct s USING (cluster, native_id)
WHERE t.status <> 'COMPLETED' OR t.attempt > 1 OR s.state <> 'COMPLETED';

-- readfit.sql: runtime and RSS vs input size.
WITH x AS (
  SELECT t.*, CASE WHEN process IN ('fasterq_dump', 'kneaddata') THEN raw_mreads ELSE clean_mreads END AS mreads
  FROM tasks t JOIN reads USING (tag)
  WHERE status = 'COMPLETED' AND process NOT LIKE '%rarefied' AND process NOT IN ('MARK_COMPLETE', 'rarefy_fastq', 'bracken_full'))
SELECT process, count(*) AS n,
  round(regr_intercept(realtime_h * 60, mreads), 1) AS rt_min_at_0,
  round(regr_slope(realtime_h * 60, mreads), 3)     AS rt_min_per_mread,
  round(regr_r2(realtime_h, mreads), 2)             AS r2_rt,
  round(regr_slope(pct_cpu / 100 * realtime_h, mreads), 4) AS used_cpuh_per_mread,
  round(regr_intercept(peak_rss_gb, mreads), 1)     AS rss_gib_at_0,
  round(regr_slope(peak_rss_gb, mreads), 4)         AS rss_gib_per_mread,
  round(regr_r2(peak_rss_gb, mreads), 2)            AS r2_rss
FROM x GROUP BY process ORDER BY process;

-- MetaPhlAn RSS tail vs input.
SELECT round(corr(peak_rss_gb, clean_mreads), 2) AS c_reads, round(corr(peak_rss_gb, clean_gbases), 2) AS c_bases,
       round(corr(peak_rss_gb, clean_gbases / clean_mreads), 2) AS c_readlen,
       round(quantile_cont(peak_rss_gb, .99), 1) AS p99, round(avg((peak_rss_gb > 44)::INT), 3) AS frac_gt44
FROM tasks JOIN reads USING (tag)
WHERE process = 'metaphlan_unknown_viruses_lists_full' AND status = 'COMPLETED' AND attempt = 1;

-- project.sql: requests (now vs recommended), cluster billing, per-sample cost per process.
CREATE OR REPLACE TABLE rec AS SELECT * FROM (VALUES
  ('fasterq_dump',                             8, 16.0,  4,  6.0),
  ('kneaddata',                                8, 30.0,  8,  8.0),
  ('fastqc',                                   2,  4.0,  1,  1.75),
  ('rarefy_fastq',                             2,  8.0,  1,  1.75),
  ('metaphlan_unknown_viruses_lists_full',    16, 47.0, 16, 47.0),
  ('metaphlan_unknown_viruses_lists_rarefied',16, 47.0, 16, 47.0),
  ('metaphlan_markers_full',                  16, 30.0,  2, 16.0),
  ('metaphlan_markers_rarefied',              16, 30.0,  2, 16.0),
  ('sample_to_markers_full',                  16, 32.0,  2, 20.0),
  ('sample_to_markers_rarefied',              16, 32.0,  2, 20.0),
  ('kraken2_full',                             8, 32.0,  8, 20.0),
  ('kraken2_rarefied',                         8, 32.0,  8, 20.0),
  ('bracken_full',                             2,  4.0,  1,  1.75),
  ('bracken_rarefied',                         2,  4.0,  1,  1.75),
  ('resistome_kma_full',                       8, 16.0,  2,  3.5),
  ('resistome_kma_rarefied',                   8, 16.0,  2,  3.5),
  ('sample_manifest',                          1,  2.0,  1,  1.75),
  ('MARK_COMPLETE',                            1,  2.0,  1,  1.75)
) v(process, cpus_now, mem_now_gb, cpus_rec, mem_rec_gb);

-- Anvil shared: MaxMemPerCPU=1896M, billing = allocated cores.
-- Alpine acpu:  MaxMemPerCPU=3840M, billing = floor(0.5*cores + 0.141*GiB).
CREATE OR REPLACE MACRO anvil_billing(c, gb) AS greatest(c, ceil(gb*1024/1896));
CREATE OR REPLACE MACRO alpine_billing(c, gb) AS floor(0.5*greatest(c, ceil(gb*1024/3840)) + 0.141*gb);

-- Every attempt is costed with its SLURM elapsed time; each process is divided by the samples that ran it.
CREATE OR REPLACE TABLE per_process AS
SELECT t.process, count(DISTINCT t.tag) AS samples,
  sum(r.cpus_now * s.elapsed_h)                                  / count(DISTINCT t.tag) AS alloc_cpuh_now,
  sum(r.cpus_rec * s.elapsed_h)                                  / count(DISTINCT t.tag) AS alloc_cpuh_rec,
  sum(anvil_billing(r.cpus_now, r.mem_now_gb * t.attempt) * s.elapsed_h)  / count(DISTINCT t.tag) AS anvil_su_now,
  sum(anvil_billing(r.cpus_rec, r.mem_rec_gb * t.attempt) * s.elapsed_h)  / count(DISTINCT t.tag) AS anvil_su_rec,
  sum(alpine_billing(r.cpus_now, r.mem_now_gb * t.attempt) * s.elapsed_h) / count(DISTINCT t.tag) AS alpine_bu_now,
  sum(alpine_billing(r.cpus_rec, r.mem_rec_gb * t.attempt) * s.elapsed_h) / count(DISTINCT t.tag) AS alpine_bu_rec
FROM tasks t JOIN sacct s USING (cluster, native_id) JOIN rec r USING (process)
GROUP BY ALL;

-- Scenarios A and B (per sample):
SELECT sum(alloc_cpuh_now), sum(alloc_cpuh_rec), sum(anvil_su_now), sum(anvil_su_rec), sum(alpine_bu_now), sum(alpine_bu_rec)
FROM per_process;

-- The 34-sample reconciliation with the issue's 11.7 (trace realtime x cpus); c34 = sample_key list from
-- GET /api/workflows/6/jobs?status=completed.
SELECT count(DISTINCT tag), sum(cpus * realtime_h) / count(DISTINCT tag) FROM tasks WHERE tag IN (SELECT tag FROM c34);

-- readmodel.sql: per-sample cost = a + b * raw M reads (per-process fits on attempt-1 tasks, summed).
CREATE OR REPLACE TABLE cost_model AS
WITH x AS (
  SELECT t.process, rd.raw_mreads, s.elapsed_h, r.*
  FROM tasks t JOIN sacct s USING (cluster, native_id) JOIN rec r USING (process) JOIN reads rd USING (tag)
  WHERE t.attempt = 1 AND t.status = 'COMPLETED'),
long AS (
  SELECT process, raw_mreads, m.metric, m.cost FROM x, LATERAL (VALUES
    ('alloc_cpuh_now', cpus_now * elapsed_h),
    ('alloc_cpuh_rec', cpus_rec * elapsed_h),
    ('anvil_su_now',   anvil_billing(cpus_now, mem_now_gb) * elapsed_h),
    ('anvil_su_rec',   anvil_billing(cpus_rec, mem_rec_gb) * elapsed_h),
    ('alpine_bu_now',  alpine_billing(cpus_now, mem_now_gb) * elapsed_h),
    ('alpine_bu_rec',  alpine_billing(cpus_rec, mem_rec_gb) * elapsed_h)) m(metric, cost))
SELECT metric, sum(a) AS a, sum(b) AS b
FROM (SELECT process, metric, regr_intercept(cost, raw_mreads) AS a, regr_slope(cost, raw_mreads) AS b
      FROM long GROUP BY ALL)
GROUP BY metric;

-- scenario_230.sql (scenario D): MetaPhlAn main steps at 55 GiB and +20 % elapsed (assumptions).
-- Scenario C: same query with r.cpus_now / r.mem_now_gb in place of r.cpus_rec / r.mem_rec_gb.
WITH x AS (
  SELECT t.process, t.tag, t.attempt, s.elapsed_h, r.cpus_rec,
    CASE WHEN t.process LIKE 'metaphlan_unknown_viruses_lists%' THEN 55.0 ELSE r.mem_rec_gb END AS mem_gb,
    CASE WHEN t.process LIKE 'metaphlan_unknown_viruses_lists%' THEN 1.2 ELSE 1.0 END AS time_factor
  FROM tasks t JOIN sacct s USING (cluster, native_id) JOIN rec r USING (process)),
p AS (
  SELECT process,
    sum(cpus_rec * elapsed_h * time_factor) / count(DISTINCT tag) AS alloc_cpuh,
    sum(anvil_billing(cpus_rec, mem_gb * attempt) * elapsed_h * time_factor) / count(DISTINCT tag) AS anvil_su,
    sum(alpine_billing(cpus_rec, mem_gb * attempt) * elapsed_h * time_factor) / count(DISTINCT tag) AS alpine_bu
  FROM x GROUP BY process)
SELECT round(sum(alloc_cpuh), 2) AS alloc_cpuh, round(sum(anvil_su), 2) AS anvil_su, round(sum(alpine_bu), 2) AS alpine_bu FROM p;

-- Amdahl split of 16-thread MetaPhlAn: serial wall S from realtime and %cpu; projected elapsed at 30 threads.
WITH x AS (SELECT t.process, s.elapsed_h, realtime_h AS rt, pct_cpu / 100 AS c
           FROM tasks t JOIN sacct s USING (cluster, native_id)
           WHERE process LIKE 'metaphlan_unknown_viruses_lists%' AND status = 'COMPLETED' AND attempt = 1),
y AS (SELECT *, greatest((16 * rt - c * rt) / 15, 0) AS S FROM x)
SELECT process, round(avg(S / rt), 2) AS serial_frac, round(avg(elapsed_h) * 60, 1) AS el_min_16t,
       round(avg(elapsed_h - rt + S + (rt - S) * 16 / 30) * 60, 1) AS el_min_30t_ideal
FROM y GROUP BY process;

-- Queue wait (submit -> start) by cluster and process.
SELECT c, p, count(*), round(median(w), 1) AS wait_med_min, round(quantile_cont(w, .9), 1) AS p90
FROM (SELECT (payload->'trace')->>'process' AS p,
             (((payload->'trace')->>'start')::DOUBLE - ((payload->'trace')->>'submit')::DOUBLE) / 60000 AS w,
             CASE WHEN (payload->'trace')->>'workdir' LIKE '/anvil/%' THEN 'anvil' ELSE 'alpine' END AS c
      FROM read_parquet('events.parquet') WHERE event = 'process_completed')
GROUP BY ALL ORDER BY 1, 4 DESC;
```

MetaPhlAn index sizes (vJan25 vs vJan26) come from HTTP `Content-Length` on `https://cmprod1.cibio.unitn.it/biobakery4/metaphlan_databases/bowtie2_indexes/<index>_bt2.tar` (35,057,633,280 vs 41,742,510,080 bytes) and `.../<index>.tar` (5,118,914,560 vs 6,014,115,840).
