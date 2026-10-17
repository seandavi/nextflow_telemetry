"""Declarative per-(workflow, version) output specs.

The only thing that changes when a pipeline version adds/renames/moves a file is
this registry. Frozen dataclass, not pydantic: developer-authored config, not an
untrusted trust boundary.

A spec's ``subpath`` is relative to the branch dir (``full_data``/``rarefied_data``)
when ``branched`` is True, or to the sample root otherwise. ``tags`` are columns
the engine sets on every row of the spec (they override the common columns).
An ``indexed`` spec isn't loaded: the object gets one index row (key, size,
sha256, parsed row count) instead, which is how HUMAnN gene families are handled (#228).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import parsers


@dataclass(frozen=True)
class OutputSpec:
    subpath: str
    table: str
    parser: Callable[[bytes], Iterator[dict]]
    tags: dict = field(default_factory=dict)
    branched: bool = True
    indexed: bool = False


# Outputs every registration so far publishes: the main MetaPhlAn pass, bracken,
# resistome and the manifest. Same paths in 2.2.1 and 2.3.0.
CORE: list[OutputSpec] = [
    # Separate per-method tables — metaphlan (percent) and bracken (read-count
    # fraction) are different value interpretations, so they don't share a column.
    OutputSpec(
        "metaphlan_markers/marker_rel_ab_w_read_stats.tsv.gz",
        "taxonomic_profile_metaphlan", parsers.parse_metaphlan_profile,
    ),
    OutputSpec(
        "kraken/bracken.species.txt.gz",
        "taxonomic_profile_bracken", parsers.parse_bracken,
    ),
    OutputSpec(
        "kraken/bracken.genus.txt.gz",
        "taxonomic_profile_bracken", parsers.parse_bracken,
    ),
    OutputSpec(
        "resistome/card_kma.res.gz",
        "resistome", parsers.parse_resistome,
    ),
    OutputSpec(
        "manifest.json", "qc_metrics", parsers.parse_qc, branched=False,
    ),
    OutputSpec(
        "metaphlan_markers/marker_abundance.tsv.gz",
        "marker_abundance", parsers.parse_marker_abundance,
    ),
    OutputSpec(
        "metaphlan_markers/marker_presence.tsv.gz",
        "marker_presence", parsers.parse_marker_presence,
    ),
]


def humann(bundle: str, metaphlan_profile: str, genefamilies: str, pathabundance: str,
           pathcoverage: str | None) -> list[OutputSpec]:
    """A HUMAnN bundle's outputs under ``humann/<bundle>/`` (pipeline ADR-0016/0018):
    the bundle's own MetaPhlAn profile and the unnormalized tables, full-depth
    reads only. File names are HUMAnN's native ones, which differ by release.
    Gene families are 120k–1.7M rows/sample in the pilot (#88/#98), so they stay
    per-sample files in cmgd-raw and are only indexed (#228); pathways are
    ~0.4k–8k rows and go into the lake."""
    d = f"humann/{bundle}"
    tags = {"data_type": "full_data", "humann_bundle": bundle}
    specs = [
        OutputSpec(f"{d}/metaphlan/metaphlan_rel_ab_w_read_stats.tsv", "taxonomic_profile_metaphlan",
                   parsers.parse_metaphlan_profile, branched=False,
                   tags={**tags, "metaphlan_profile": metaphlan_profile}),
        OutputSpec(f"{d}/{genefamilies}", "humann_genefamilies_files",
                   parsers.parse_humann_genefamilies, branched=False, tags=tags, indexed=True),
        OutputSpec(f"{d}/{pathabundance}", "humann_pathabundance", parsers.parse_humann_pathabundance,
                   branched=False, tags=tags),
    ]
    if pathcoverage:
        specs.append(OutputSpec(f"{d}/{pathcoverage}", "humann_pathcoverage",
                                parsers.parse_humann_pathcoverage, branched=False, tags=tags))
    return specs


# Keyed by registration (ADR-0010): one bundle = one workflow_id.
SPECS: dict[tuple[str, str], list[OutputSpec]] = {
    ("cmgd_nextflow", "2.2.1"): CORE,
    ("cmgd_mpa4.2", "2.3.0"): CORE,
    ("cmgd_humann3.9", "2.3.0"): CORE + humann(
        "humann3.9", "mpa4.1.1_vJun23",
        "out_genefamilies.tsv.gz", "out_pathabundance.tsv.gz", "out_pathcoverage.tsv.gz"),
    # HUMAnN 4 numbers its tables and has no pathcoverage.
    ("cmgd_humann4a1", "2.3.0"): CORE + humann(
        "humann4.0.0a1", "mpa4.1.1_vOct22",
        "out_2_genefamilies.tsv.gz", "out_4_pathabundance.tsv.gz", None),
    # Same bundle files as a1. BAQLaVa (baqlava/<key>_BAQLaVa_profile.txt) and the
    # reaction tables are delivered (deliver.py) but have no lake table.
    ("cmgd_humann4a2", "2.4.2"): CORE + humann(
        "humann4.0.0a2", "mpa4.1.1_vOct22",
        "out_2_genefamilies.tsv.gz", "out_4_pathabundance.tsv.gz", None),
}

BRANCHES = ("full_data", "rarefied_data")

# The main MetaPhlAn pass's profile when a registration doesn't set
# `metaphlan_profile` (pipeline default since 2.3.0; 2.2.x ran the same
# MetaPhlAn 4.2.2 + vJan25 index).
DEFAULT_METAPHLAN_PROFILE = "mpa4.2.2_vJan25"

# Reference databases pinned in the pipeline's nextflow.config for a
# registration (ADR-0010: params are immutable per version). Per-sample
# manifests don't record the Kraken or CARD versions yet (plan L6), so a
# release's provenance block reads them here. Registrations without an entry
# publish ``databases: null``.
DATABASES: dict[tuple[str, str], dict[str, str]] = {
    ("cmgd_mpa4.2", "2.3.0"): {
        "metaphlan": "4.2.6",
        "metaphlan_index": "mpa_vJan26_CHOCOPhlAnSGB_202605",
        "kraken2_bracken": "k2_pluspf_16_GB_20260226",
        "card": "broadstreet-v4.0.1",
    },
    # cMN 2.4.2 conf/humann_bundles.config (humann4.0.0a2), conf/metaphlan_profiles.config
    # (mpa4.1.1_vOct22) and nextflow.config. The KneadData human_genome index and the
    # BAQLaVa DB come from their tools' download commands and are unversioned (cMN#129).
    ("cmgd_humann4a2", "2.4.2"): {
        "metaphlan": "4.1.1",
        "metaphlan_index": "mpa_vOct22_CHOCOPhlAnSGB_202403",
        "chocophlan": "full, vOct22_CHOCOPhlAnSGB_202403",
        "uniref": "uniref90_ec_filtered_diamond, release 2019_06",
        "utility_mapping": "full",
        "kraken2_bracken": "k2_pluspf_16_GB_20260226",
        "card": "broadstreet-v4.0.1",
        "human_genome": "KneadData human_genome Bowtie 2 index (unversioned)",
        "baqlava_db": "baqlava_database --download (unversioned)",
    },
}

# Images a step ran in that the manifest doesn't name, by registration, from the
# pipeline config at that revision. The manifest itself records the base image
# (provenance.container) and software_versions.*_image; cMN#129 asks the pipeline
# to record every image with its digest.
CONTAINERS: dict[tuple[str, str], dict[str, str]] = {
    ("cmgd_humann4a2", "2.4.2"): {
        "MetaPhlAn": "quay.io/biocontainers/metaphlan:4.1.1--pyhdfd78af_0",
        "HUMAnN": "ghcr.io/seandavi/humann-baqlava:4.0.0a2-1.2.0",
    },
}

# Registration-scoped caveats a deliverable can't derive from its data (Markdown).
CAVEATS: dict[tuple[str, str], list[str]] = {
    ("cmgd_humann4a2", "2.4.2"): [
        "**Revision moved during the run.** The registration's revision went from 2.4.2 to "
        "2.4.3 on 2026-10-17. 2.4.3 only deletes intermediate files earlier; results are "
        "unchanged (curatedMetagenomicsNextflow#126). `samples.tsv` gives each sample's "
        "revision.",
        "**Patched UniRef file name.** HUMAnN 4.0.0a2 checks that a protein database's file "
        "names carry its expected version tag, so BAQLaVa's DIAMOND database files were "
        "symlinked under names ending `_2019_06` (pipeline 2.4.2, #125; humann#82). The "
        "database contents are unchanged.",
        "**Paired-end reads merged.** All FASTQ files of a sample, both mates included, were "
        "concatenated and processed as single-end reads (the curatedMetagenomicData "
        "convention), so read counts in `samples.tsv` count each mate.",
        "**Unversioned databases.** The KneadData human reference index and the BAQLaVa "
        "database are fetched by their tools' download commands, which don't pin a release.",
    ],
}
