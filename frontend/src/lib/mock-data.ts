import type {
  ProcessSummaryResponse,
  ProcessFailuresResponse,
  ProcessRetriesResponse,
  ProcessResourcesByAttemptResponse,
  ProcessFailureSignaturesResponse,
  WorkflowResponse,
  JobTotals,
  HealthResponse,
  SampleResponse,
} from '../types'

const PROCESSES = [
  'FASTQC','TRIM_GALORE','BWA_MEM','SAMTOOLS_SORT','SAMTOOLS_INDEX',
  'PICARD_MARKDUPLICATES','GATK_HAPLOTYPECALLER','GATK_GENOTYPEGVCFS',
  'BCFTOOLS_FILTER','MULTIQC','STAR_ALIGN','SALMON_QUANT',
  'DESEQ2_ANALYSIS','KALLISTO_QUANT','CUTADAPT','BOWTIE2_ALIGN',
  'KRAKEN2_CLASSIFY','BRACKEN_ESTIMATE','METAPHLAN_PROFILE','HUMANN_PROFILE',
]

const rnd  = (a: number, b: number) => +(Math.random() * (b - a) + a).toFixed(2)
const ri   = (a: number, b: number) => Math.floor(Math.random() * (b - a + 1)) + a
const pick = <T>(arr: T[]): T => arr[ri(0, arr.length - 1)]!

export const MOCK_SUMMARY: ProcessSummaryResponse = {
  generated_at_utc: new Date().toISOString(),
  window_days: 30,
  cards: {
    process_completed_rows: 48_320_000,
    distinct_runs: 102_400,
    distinct_processes: 20,
    success_rows: 46_260_000,
    failure_rows: 2_060_000,
    failure_pct: 4.26,
    retried_rows: 1_840_000,
    retry_pct: 3.81,
    retry_success_pct: 77.4,
    latest_process_completed_utc: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
  },
  event_mix: [
    { event: 'process_completed', rows: 48_320_000 },
    { event: 'process_started',   rows: 50_160_000 },
    { event: 'process_submitted', rows: 50_160_000 },
    { event: 'started',           rows: 102_400 },
    { event: 'completed',         rows: 101_108 },
    { event: 'error',             rows: 1_292 },
  ],
  top_failures: [
    { process: 'GATK_HAPLOTYPECALLER', total_completed: 7_200_000, failed: 540_000, failure_pct: 7.50 },
    { process: 'STAR_ALIGN',           total_completed: 3_600_000, failed: 252_000, failure_pct: 7.00 },
    { process: 'HUMANN_PROFILE',       total_completed: 2_250_000, failed: 146_250, failure_pct: 6.50 },
    { process: 'BWA_MEM',              total_completed: 7_200_000, failed: 360_000, failure_pct: 5.00 },
    { process: 'KRAKEN2_CLASSIFY',     total_completed: 2_250_000, failed: 101_250, failure_pct: 4.50 },
    { process: 'METAPHLAN_PROFILE',    total_completed: 2_250_000, failed:  90_000, failure_pct: 4.00 },
    { process: 'DESEQ2_ANALYSIS',      total_completed: 1_080_000, failed:  38_880, failure_pct: 3.60 },
    { process: 'TRIM_GALORE',          total_completed: 7_200_000, failed: 187_920, failure_pct: 2.61 },
  ],
  top_retries: [
    { process: 'GATK_HAPLOTYPECALLER', total_completed: 7_200_000, retried: 540_000, retried_pct: 7.50, retried_success: 421_920, retried_failed: 118_080 },
    { process: 'BWA_MEM',              total_completed: 7_200_000, retried: 360_000, retried_pct: 5.00, retried_success: 306_000, retried_failed:  54_000 },
    { process: 'STAR_ALIGN',           total_completed: 3_600_000, retried: 180_000, retried_pct: 5.00, retried_success: 144_000, retried_failed:  36_000 },
    { process: 'HUMANN_PROFILE',       total_completed: 2_250_000, retried: 135_000, retried_pct: 6.00, retried_success:  94_500, retried_failed:  40_500 },
    { process: 'KRAKEN2_CLASSIFY',     total_completed: 2_250_000, retried:  90_000, retried_pct: 4.00, retried_success:  72_000, retried_failed:  18_000 },
  ],
  top_failure_exit_codes: [
    { exit_code: '137', failures: 960_000 },
    { exit_code: '1',   failures: 600_000 },
    { exit_code: '2',   failures: 280_000 },
    { exit_code: '134', failures: 140_000 },
    { exit_code: 'null',failures:  80_000 },
  ],
}

export const MOCK_FAILURES: ProcessFailuresResponse = {
  generated_at_utc: new Date().toISOString(),
  window_days: 30,
  rows: PROCESSES.map(p => {
    const total = ri(500_000, 7_500_000)
    const failed = ri(5_000, Math.floor(total * 0.10))
    return {
      process: p, total_completed: total,
      success: total - failed, failed,
      failure_pct: +((failed / total) * 100).toFixed(2),
      modal_failure_exit_code: pick(['137','1','2','134','null']),
    }
  }).sort((a, b) => b.failed - a.failed),
}

export const MOCK_RETRIES: ProcessRetriesResponse = {
  generated_at_utc: new Date().toISOString(),
  window_days: 30,
  summary: {
    process_completed_rows: 48_320_000,
    retried_rows: 1_840_000,
    retried_pct: 3.81,
    retry_success_rows: 1_424_160,
    retry_failure_rows:   415_840,
    retry_success_pct: 77.4,
  },
  by_attempt: [
    { attempt: 1, rows: 48_320_000, success: 46_480_000, failed: 1_840_000 },
    { attempt: 2, rows:  1_424_160, success:  1_110_845, failed:   313_315 },
    { attempt: 3, rows:    102_525, success:     77_918, failed:    24_607 },
  ],
  by_process: PROCESSES.map(p => {
    const total = ri(500_000, 7_500_000)
    const retried = ri(10_000, Math.floor(total * 0.08))
    const rs = Math.floor(retried * rnd(0.6, 0.88))
    return {
      process: p, total_completed: total, retried,
      retried_pct: +((retried / total) * 100).toFixed(2),
      retried_success: rs, retried_failed: retried - rs,
      max_attempt: pick([2,2,2,3,3]),
    }
  }).sort((a, b) => b.retried - a.retried),
}

export const MOCK_RESOURCES: ProcessResourcesByAttemptResponse = {
  generated_at_utc: new Date().toISOString(),
  window_days: 30,
  rows: PROCESSES.flatMap(p => [1, 2].map(attempt => ({
    process: p, attempt,
    rows: ri(300_000, 3_000_000),
    success: ri(280_000, 2_900_000),
    failed: ri(5_000, 100_000),
    avg_requested_cpus: pick([1,2,4,8,16]),
    avg_requested_memory_gb: pick([4,8,16,32,64]),
    avg_requested_time_min: rnd(30, 480),
    avg_pct_cpu: rnd(20, 95),
    p95_pct_cpu: rnd(60, 110),
    avg_pct_mem: rnd(25, 88),
    p95_pct_mem: rnd(55, 105),
    avg_peak_rss_gb: rnd(1, 28),
    p95_peak_rss_gb: rnd(4, 56),
    avg_read_gb: rnd(0.5, 40),
    avg_write_gb: rnd(0.2, 20),
  }))),
}

export const MOCK_SIGNATURES: ProcessFailureSignaturesResponse = {
  generated_at_utc: new Date().toISOString(),
  window_days: 30,
  rows: PROCESSES.flatMap(p =>
    ['137','1','2','134'].slice(0, ri(1,4)).map(code => ({
      process: p, exit_code: code, failures: ri(1_000, 200_000),
    }))
  ).sort((a, b) => b.failures - a.failures),
}

export const MOCK_WORKFLOWS: WorkflowResponse[] = [
  { id: 1, workflow_id: 'curatedMetagenomics', version: '3.2.1',
    repository_url: 'https://github.com/nf-core/taxprofiler',
    revision: 'main', profile: 'slurm', manifest_version: '3.2.1',
    max_retries: 3, status: 'active',
    description: 'Metagenomic profiling with Kraken2, Bracken, MetaPhlAn, and HUMAnN.',
    created_at: '2024-11-01T08:00:00Z', updated_at: '2025-04-15T14:22:00Z',
    job_stats: { total: 98_200, pending: 1_840, running: 420, completed: 94_100, failed: 1_840 },
  },
  { id: 2, workflow_id: 'rnaseqAnalysis', version: '2.1.0',
    repository_url: 'https://github.com/nf-core/rnaseq',
    revision: 'release/2.1', profile: 'slurm', manifest_version: '2.1.0',
    max_retries: 2, status: 'active',
    description: 'RNA-seq alignment and quantification with STAR and Salmon.',
    created_at: '2024-09-15T09:00:00Z', updated_at: '2025-03-30T11:00:00Z',
    job_stats: { total: 96_400, pending: 2_100, running: 380, completed: 92_600, failed: 1_320 },
  },
  { id: 3, workflow_id: 'variantCalling', version: '4.0.0',
    repository_url: 'https://github.com/nf-core/sarek',
    revision: 'main', profile: 'docker', manifest_version: '4.0.0',
    max_retries: 3, status: 'active',
    description: 'Germline and somatic variant calling with GATK4.',
    created_at: '2025-01-10T10:00:00Z', updated_at: '2025-04-28T09:15:00Z',
    job_stats: { total: 45_000, pending: 800, running: 240, completed: 43_200, failed: 760 },
  },
  { id: 4, workflow_id: 'rnaseqAnalysis', version: '2.0.1',
    repository_url: 'https://github.com/nf-core/rnaseq',
    revision: 'release/2.0', profile: 'slurm', manifest_version: '2.0.1',
    max_retries: 2, status: 'paused',
    description: 'RNA-seq v2.0 (superseded by 2.1.0).',
    created_at: '2024-06-01T00:00:00Z', updated_at: '2025-03-30T11:00:00Z',
    job_stats: { total: 88_000, pending: 0, running: 0, completed: 85_200, failed: 2_800 },
  },
  { id: 5, workflow_id: 'amplicon16S', version: '1.0.0',
    repository_url: 'https://github.com/nf-core/ampliseq',
    revision: 'v1.0.0', profile: 'standard', manifest_version: '1.0.0',
    max_retries: 1, status: 'retired',
    description: '16S rRNA amplicon analysis with DADA2 and QIIME2.',
    created_at: '2024-01-10T00:00:00Z', updated_at: '2024-11-01T00:00:00Z',
    job_stats: { total: 12_000, pending: 0, running: 0, completed: 11_400, failed: 600 },
  },
]

export const MOCK_SAMPLE_TOTAL = 103_847
export const MOCK_SAMPLE_COHORTS = ['IBD-PRISM','HMP2','PEDIATRIC-CD','HEALTHY-CTRL','T2D-COHORT']
export const MOCK_SAMPLE_PHENOTYPES = ['CD','UC','healthy','unclassified','T2D']
export const MOCK_SAMPLE_SOURCES = ['stool','biopsy','saliva','blood']

export function genSamplePage(page = 0, pageSize = 50, _search = '', cohort = ''): SampleResponse[] {
  const seed = page * 1000
  const lcg = (n: number) => ((n * 1664525 + 1013904223) & 0xffffffff) >>> 0
  const rows: SampleResponse[] = []
  for (let i = 0; i < pageSize; i++) {
    const idx = seed + i
    const s0  = lcg(idx + 1)
    const s1  = lcg(s0)
    const s2  = lcg(s1)
    const s3  = lcg(s2)
    const s4  = lcg(s3)
    const cohortVal = MOCK_SAMPLE_COHORTS[s1 % MOCK_SAMPLE_COHORTS.length]!
    const phenotype = MOCK_SAMPLE_PHENOTYPES[s2 % MOCK_SAMPLE_PHENOTYPES.length]!
    const source    = MOCK_SAMPLE_SOURCES[s3 % MOCK_SAMPLE_SOURCES.length]!
    if (cohort && cohortVal !== cohort) continue
    rows.push({
      id: page * pageSize + i + 1,
      sample_id: `SRR${(10000000 + idx * 137) % 100000000}`,
      metadata: { cohort: cohortVal, phenotype, source, read_count: 10_000_000 + (s4 % 110_000_000) },
      created_at: new Date(Date.now() - (idx % 400) * 86_400_000).toISOString(),
      updated_at: new Date(Date.now() - (idx %  10) * 86_400_000).toISOString(),
    })
  }
  return rows
}

export const MOCK_JOB_TOTALS: JobTotals = {
  total:     492_235,
  pending:    8_140,
  claimed:      420,
  running:    2_840,
  completed: 474_875,
  failed:      5_960,
  dead_letter:   820,
  sparkline: Array.from({ length: 30 }, (_, i) =>
    12_000 + Math.floor(Math.sin(i / 4) * 3_000 + Math.random() * 2_000)),
}

export const MOCK_HEALTH: HealthResponse = {
  message: 'App Started', status: 'Healthy', database: 'Connected',
}
