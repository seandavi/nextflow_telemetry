# Nextflow Telemetry

Telemetry and pull-mode HPC orchestration backend for running Nextflow workflows
across many samples and reporting on their progress. This glossary pins the terms
that are easy to conflate; it is not a spec.

## Language

**Collection**:
A named, many-to-many set of samples — the canonical grouping concept. A collection
may be *study-based* (derived from an accession such as a BioProject/SRA study) or
*logical* (an arbitrary curated set). "Collection" is deliberately neutral: it carries
no biological or medical interpretation, which is why it subsumes the others. A
collection is identified by an exact-string natural key (its name; for study-based
collections, the accession) — there is no surrogate id, so two references to the same
string are the same collection.
Purpose is operational, not analytic: a collection exists so the team can watch how
close a set of samples is to completion. Nothing computes on collections, so membership
overlap (a sample in several collections) is fine and expected.
_Avoid_: cohort, study (as a top-level concept), curated study

**Study**:
Not a first-class concept — a *source* of a collection. A study-based collection is a
collection whose provenance is an accession. Use "collection" for the thing itself.

**Sample**:
The content-addressed unit of work: identity is the md5 of its sorted, deduplicated
SRR set, so the same SRRs always resolve to the same sample. A sample can belong to
many collections. The sample row is *identity only* (sample_id, SRR accessions) — it
is not a place to hang descriptive attributes. Anything descriptive (cohort labels,
curated annotations) lives in a separate table keyed by sample, never as a column or
`metadata` key on the sample.
_Avoid_: run, dataset

**Curated metadata**:
Descriptive per-sample annotations (originating from curated TSV imports), kept in
their own table, many-to-one with Sample. Deliberately outside the sample row so the
telemetry/jobs core stays lean. Currently a partial island (`curated_sample_annotations`);
future work joins it in.

**Membership**:
The link between a sample and a collection (`collection_samples`). Many-to-many: one
sample in several collections, one collection over many samples. The single source of
truth for "what collection is this sample in" — not `metadata.cohort`, which is a
legacy scalar shadow being retired.
