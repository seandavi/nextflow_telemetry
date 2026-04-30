#!/usr/bin/env nextflow
/*
 * Metagenomics stub pipeline for exercising the weblog telemetry contract.
 *
 * Simulates a realistic curatedMetagenomics processing shape without requiring
 * any real bioinformatics tools. All processes produce plausible-looking output
 * files and honour the tag and MARK_COMPLETE semaphore conventions expected by
 * the telemetry server.
 *
 * Usage:
 *   nextflow run nf_testing/main.nf \
 *     -name <uuid7-run-name> \
 *     -with-weblog http://localhost:8000/telemetry \
 *     --sample_ids SRR123,SRR456 \
 *     --workflow_id curatedMetagenomics \
 *     --workflow_version 1.0.0
 *
 * Inject a deliberate failure:
 *   --fail_at PROFILE_TAXA   (process name, case-sensitive)
 */

nextflow.enable.dsl = 2

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------
params.sample_ids       = "SRR000001"          // comma-separated SRA accessions
params.workflow_id      = "curatedMetagenomics"
params.workflow_version = "1.0.0"
params.outdir           = "results"
params.fail_at          = ""                   // inject failure at named process

// The run name is set externally via -name (client-generated UUID7).
// We expose it as a param so it appears in weblog metadata.params for
// server-side correlation.
params.run_name         = workflow.runName

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
def tag(sample_id) {
    "${sample_id}:${workflow.runName}"
}

def shouldFail(process_name) {
    params.fail_at && params.fail_at == process_name
}

// ---------------------------------------------------------------------------
// Processes
// ---------------------------------------------------------------------------

process FETCH_READS {
    tag "${tag(sample_id)}"

    input:
    val sample_id

    output:
    tuple val(sample_id), path("${sample_id}_reads.fastq.gz")

    script:
    if (shouldFail("FETCH_READS"))
        """
        echo "Simulated fetch failure for ${sample_id}" >&2
        exit 1
        """
    else
        """
        echo "@${sample_id}.1 simulated read" > reads.fastq
        echo "ACGTACGTACGTACGT"              >> reads.fastq
        echo "+"                             >> reads.fastq
        echo "IIIIIIIIIIIIIIII"              >> reads.fastq
        gzip -c reads.fastq > ${sample_id}_reads.fastq.gz
        """
}

process QC_READS {
    tag "${tag(sample_id)}"

    input:
    tuple val(sample_id), path(reads)

    output:
    tuple val(sample_id), path("${sample_id}_qc.fastq.gz"), path("${sample_id}_qc_report.txt")

    script:
    if (shouldFail("QC_READS"))
        """
        echo "Simulated QC failure for ${sample_id}" >&2
        exit 1
        """
    else
        """
        # Simulate quality trimming — just copy the reads through
        cp ${reads} ${sample_id}_qc.fastq.gz
        printf "Sample: ${sample_id}\\nTotal reads: 1000\\nPassed QC: 980\\n" \
            > ${sample_id}_qc_report.txt
        """
}

process PROFILE_TAXA {
    tag "${tag(sample_id)}"

    input:
    tuple val(sample_id), path(reads), path(qc_report)

    output:
    tuple val(sample_id), path("${sample_id}_profile.tsv")

    script:
    if (shouldFail("PROFILE_TAXA"))
        """
        echo "Simulated profiling failure for ${sample_id}" >&2
        exit 1
        """
    else
        """
        printf "clade_name\\trelative_abundance\\n"  > ${sample_id}_profile.tsv
        printf "k__Bacteria\\t0.95\\n"              >> ${sample_id}_profile.tsv
        printf "k__Archaea\\t0.05\\n"               >> ${sample_id}_profile.tsv
        """
}

process AGGREGATE_RESULTS {
    tag "${tag(sample_id)}"

    input:
    tuple val(sample_id), path(profile)

    output:
    tuple val(sample_id), path("${sample_id}_summary.json")

    publishDir "${params.outdir}/${params.workflow_id}/${params.workflow_version}/${sample_id}",
        mode: 'copy'

    script:
    if (shouldFail("AGGREGATE_RESULTS"))
        """
        echo "Simulated aggregation failure for ${sample_id}" >&2
        exit 1
        """
    else
        """
        cat <<JSON > ${sample_id}_summary.json
        {
          "sample_id": "${sample_id}",
          "workflow_id": "${params.workflow_id}",
          "workflow_version": "${params.workflow_version}",
          "run_name": "${params.run_name}",
          "status": "success"
        }
        JSON
        """
}

/*
 * Semaphore process — authoritative per-sample completion signal.
 *
 * The telemetry server watches for a process_completed event whose
 * trace.process ends with "MARK_COMPLETE" and trace.status == "COMPLETED".
 * Only when this event arrives is the (sample_id, workflow_id, version)
 * execution marked complete in the DB.
 */
process MARK_COMPLETE {
    tag "${tag(sample_id)}"

    input:
    tuple val(sample_id), path(summary)

    output:
    tuple val(sample_id), path("${sample_id}.done")

    script:
    """
    echo "${sample_id} complete at \$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > ${sample_id}.done
    """
}

// ---------------------------------------------------------------------------
// Workflow
// ---------------------------------------------------------------------------
workflow {
    // Parse comma-separated sample IDs into a channel
    samples_ch = Channel.of(params.sample_ids.tokenize(','))
                        .flatten()
                        .map { it.trim() }

    reads_ch   = FETCH_READS(samples_ch)
    qc_ch      = QC_READS(reads_ch)
    profile_ch = PROFILE_TAXA(qc_ch)
    summary_ch = AGGREGATE_RESULTS(profile_ch)
    MARK_COMPLETE(summary_ch)
}
