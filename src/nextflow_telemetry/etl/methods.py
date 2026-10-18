"""METHODS.md for a deliverable (#302): paste-ready Methods prose, the software,
container and database tables, how to read each table, caveats and citations.

Versions, counts and read depths come from the delivered samples' manifests;
the step prose and citations below are hand-written, because the pipeline doesn't
record its key step parameters (cMN#129). A step is described, and its tools
cited, only when its ``software_versions`` key is in the manifests.

ponytail: the step parameters are the ones cMN 2.4.x modules pass; key STEPS by
registration if a pipeline version changes them.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from statistics import median
from string import Template

from . import publish
from .v2 import Registration

# software_versions key -> prose. $version is that tool's version; any other
# $slot is another tool's version or a DATABASES key ("not recorded" if absent).
STEPS: dict[str, Template] = {k: Template(v) for k, v in {
    "fasterq-dump": (
        "Raw reads for each sequencing run were downloaded from ENA (with an MD5 check), or "
        "from NCBI SRA with fasterq-dump $version when ENA did not serve the run. All FASTQ "
        "files of a sample, including both mates of paired-end runs, were concatenated and "
        "processed as single-end reads."),
    "kneaddata": (
        "Reads were quality-trimmed with Trimmomatic $trimmomatic (SLIDINGWINDOW:4:20, "
        "MINLEN:30) and depleted of human reads by alignment to the human reference genome "
        "with Bowtie 2 $bowtie2 (`--very-fast`) via KneadData $version; tandem-repeat "
        "filtering was bypassed."),
    "fastqc": "Read quality was assessed with FastQC $version.",
    "metaphlan": (
        "Taxonomic profiles were estimated from all quality-controlled reads with MetaPhlAn "
        "$version using the $metaphlan_index marker database."),
    "humann": (
        "Functional profiles were computed with HUMAnN $version, using that MetaPhlAn profile "
        "for the nucleotide prescreen, the ChocoPhlAn pangenome database ($chocophlan) for "
        "nucleotide search and UniRef90 ($uniref) with DIAMOND for translated search. HUMAnN "
        "reports gene-family, reaction and MetaCyc pathway abundances."),
    "baqlava": (
        "Viral profiles were estimated with BAQLaVa $version on the reads that HUMAnN's "
        "nucleotide search left unaligned, with nucleotide search by Bowtie 2 "
        "(`--very-sensitive`, query coverage ≥ 90%) and translated search by DIAMOND "
        "(`--top 1`, identity ≥ 80%, query coverage ≥ 90%, e-value ≤ 1.0), reporting "
        "abundance per viral genome bin (VGB)."),
}.items()}
QC_STEPS = ("fasterq-dump", "kneaddata", "fastqc")  # first paragraph; the rest is profiling

# software_versions key ("pipeline": always) -> (name, citation). Checked against
# Crossref and PubMed on 2026-10-17; $version as in STEPS. Kraken2/Bracken/KMA run
# but aren't delivered, so they aren't cited.
CITATIONS: dict[str, list[tuple[str, str]]] = {
    "pipeline": [
        ("Nextflow", "Di Tommaso P, Chatzou M, Floden EW, Barja PP, Palumbo E, Notredame C. "
                     "Nextflow enables reproducible computational workflows. Nat Biotechnol. "
                     "2017;35(4):316-319. doi:10.1038/nbt.3820"),
    ],
    "fasterq-dump": [("SRA Toolkit", "NCBI SRA Toolkit $version, https://github.com/ncbi/sra-tools")],
    "fastqc": [("FastQC", "Andrews S. FastQC $version. "
                          "https://www.bioinformatics.babraham.ac.uk/projects/fastqc/")],
    "kneaddata": [
        ("KneadData", "bioBakery KneadData $version (no paper), "
                      "https://github.com/biobakery/kneaddata; release files at "
                      "https://pypi.org/project/kneaddata/"),
        ("Trimmomatic", "Bolger AM, Lohse M, Usadel B. Trimmomatic: a flexible trimmer for "
                        "Illumina sequence data. Bioinformatics. 2014;30(15):2114-2120. "
                        "doi:10.1093/bioinformatics/btu170"),
        ("Bowtie 2", "Langmead B, Salzberg SL. Fast gapped-read alignment with Bowtie 2. "
                     "Nat Methods. 2012;9(4):357-359. doi:10.1038/nmeth.1923"),
    ],
    "metaphlan": [
        ("MetaPhlAn 4", "Blanco-Míguez A, Beghini F, Cumbo F, et al. Extending and improving "
                        "metagenomic taxonomic profiling with uncharacterized species using "
                        "MetaPhlAn 4. Nat Biotechnol. 2023;41(11):1633-1644. "
                        "doi:10.1038/s41587-023-01688-w"),
    ],
    "humann": [
        ("HUMAnN", "Beghini F, McIver LJ, Blanco-Míguez A, et al. Integrating taxonomic, "
                   "functional, and strain-level profiling of diverse microbial communities "
                   "with bioBakery 3. eLife. 2021;10:e65088. doi:10.7554/eLife.65088. "
                   "HUMAnN 4 has no paper yet: also cite release $version, "
                   "https://github.com/biobakery/humann (https://pypi.org/project/humann/)"),
        ("DIAMOND", "Buchfink B, Xie C, Huson DH. Fast and sensitive protein alignment using "
                    "DIAMOND. Nat Methods. 2015;12(1):59-60. doi:10.1038/nmeth.3176"),
        ("UniRef", "Suzek BE, Wang Y, Huang H, McGarvey PB, Wu CH; UniProt Consortium. UniRef "
                   "clusters: a comprehensive and scalable alternative for improving sequence "
                   "similarity searches. Bioinformatics. 2015;31(6):926-932. "
                   "doi:10.1093/bioinformatics/btu739"),
        ("MetaCyc", "Caspi R, Billington R, Keseler IM, et al. The MetaCyc database of "
                    "metabolic pathways and enzymes - a 2019 update. Nucleic Acids Res. "
                    "2020;48(D1):D445-D453. doi:10.1093/nar/gkz862"),
    ],
    "baqlava": [("BAQLaVa", "bioBakery BAQLaVa $version (no paper), "
                            "https://github.com/biobakery/baqlava/releases/tag/$version")],
}


def joined(c: Counter) -> str:
    """'a' or 'a (11 samples) / b (1 sample)'."""
    if len(c) == 1:
        return next(iter(c))
    return " / ".join(f"{v} ({n} sample{'s' * (n != 1)})" for v, n in c.most_common())


def _slots(vers: dict[str, Counter], dbs: dict[str, str]) -> defaultdict:
    s: defaultdict = defaultdict(lambda: "(not recorded)", dbs)
    s.update({t.replace("-", "_"): joined(c) for t, c in vers.items()})
    return s


def prose(manifests: Sequence[dict], vers: dict[str, Counter], dbs: dict[str, str],
          qc: Sequence[dict]) -> list[str]:
    """Methods paragraphs: pipeline and read depth, then one sentence per step."""
    revs = Counter(f"v{(m.get('provenance') or {}).get('pipeline_version')}, commit "
                   f"{((m.get('provenance') or {}).get('git_commit') or '')[:7]}"
                   for m in manifests)
    nf = Counter(str((m.get("provenance") or {}).get("nextflow_version")) for m in manifests)
    head = (f"Shotgun metagenomes were processed with the curatedMetagenomicData pipeline "
            f"(curatedMetagenomicsNextflow {joined(revs)}) under Nextflow {joined(nf)}.")
    reads = [r["reads_decontaminated"] for r in qc if r.get("reads_decontaminated")]
    frac = [r["reads_surviving_fraction"] for r in qc if r.get("reads_surviving_fraction")]
    tail = (f" A median of {median(frac) * 100:.1f}% of reads (range {min(frac) * 100:.1f}–"
            f"{max(frac) * 100:.1f}%) survived quality control and host removal, leaving a "
            f"median of {median(reads) / 1e6:.1f} million reads per sample (range "
            f"{min(reads) / 1e6:.1f}–{max(reads) / 1e6:.1f} million)." if reads and frac else "")
    slots = _slots(vers, dbs)
    steps = {k: t.substitute(slots, version=joined(vers[k])) for k, t in STEPS.items() if k in vers}
    qc_steps = " ".join(v for k, v in steps.items() if k in QC_STEPS)
    profiling = " ".join(v for k, v in steps.items() if k not in QC_STEPS)
    return [p for p in (f"{head} {qc_steps}".strip() + tail, profiling) if p]


# Olm's Microbiome Repo, the source of a deliverable's olm_* columns (CC-BY-4.0).
OLM_CITATION = ("Microbiome Repo, release R2026.25 (data package 1.20.0), OlmLab, 2026-10-12, "
                "CC-BY-4.0")


def citations(vers: dict[str, Counter], olm: bool = False) -> list[tuple[str, str]]:
    """(name, citation) for the tools in the manifests, curatedMetagenomicData first
    (then Olm's Microbiome Repo when ``olm``: the deliverable has ``olm_*`` columns)."""
    out = {"curatedMetagenomicData": publish.CITATION["cmgd"]}
    if olm:
        out["Microbiome Repo (`olm_*` sample metadata)"] = OLM_CITATION
    for k, cites in CITATIONS.items():
        if k == "pipeline" or k in vers:
            for name, text in cites:
                out.setdefault(name, Template(text).safe_substitute(
                    version=joined(vers[k]) if k in vers else ""))
    return list(out.items())


def md_table(header: Sequence[str], rows: Iterable[Sequence]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def render(name: str, today: date, expires: date, reg: Registration, cov: list[dict],
           n_samples: int, paragraphs: Sequence[str], vers: dict[str, Counter],
           containers: Sequence[Sequence[str]], dbs: dict[str, str],
           files: Sequence[tuple[str, str]], caveats: Sequence[str], olm: bool = False) -> str:
    quote = "\n>\n".join("> " + p for p in paragraphs)
    return f"""# Methods: curatedMetagenomicData deliverable {name} ({today})

How the tables in this deliverable were produced, for writing a Methods section
and analysing the data. Versions and counts were read from the delivered
samples' `manifest.json` files at build time; step parameters are those of the
pipeline revision that ran. Where they disagree, the per-sample values in
`samples.tsv.gz` are authoritative.

Registration `{reg.workflow_id}/{reg.version}` (id {reg.pk}). {n_samples} samples from
collections {', '.join(f"{c['collection']} ({c['delivered']})" for c in cov)}. Built
{today}, expires {expires}.

## 1. Methods text (ready to paste)

{quote}

Cite the tools and databases in section 5, and curatedMetagenomicData.

## 2. Software, containers and databases

### Software (from each sample's manifest)

{md_table(["tool", "version (samples)"], [[t, joined(c)] for t, c in sorted(vers.items())])}

### Containers

{md_table(["step", "image", "digest"], containers) if containers else "Not recorded."}

### Reference databases

{md_table(["database", "version"], dbs.items()) if dbs else "Not recorded for this registration."}

## 3. The tables

Every profile table is a gzipped TSV, one row per sample (`readset_id`) and one
column per feature; 0 means *not reported*, which is not a measured absence. Join
every table to `samples.tsv.gz` on `readset_id`.

{chr(10).join(f"- `{f}`: {doc}" for f, doc in files)}

## 4. Caveats

{chr(10).join(f"{i}. {c}" for i, c in enumerate(caveats, 1))}

## 5. How to cite

{md_table(["tool / database", "citation"], citations(vers, olm))}
"""
