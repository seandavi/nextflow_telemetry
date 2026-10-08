"""Declarative per-(workflow, version) output specs.

The only thing that changes when a pipeline version adds/renames/moves a file is
this registry. Frozen dataclass, not pydantic: developer-authored config, not an
untrusted trust boundary.

A spec's ``subpath`` is relative to the branch dir (``full_data``/``rarefied_data``)
when ``branched`` is True, or to the sample root otherwise. ``tags`` are columns
the engine sets on every row of the spec (they override the common columns).
``defer=True`` marks the big tables (markers, HUMAnN gene families) — spec'd for
completeness but skipped by the default ingest until the marker-store decision
is made.
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
    defer: bool = False


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
    # Deferred — markers are ~89% of all rows; not on the low-latency path.
    OutputSpec(
        "metaphlan_markers/marker_abundance.tsv.gz",
        "marker_abundance", parsers.parse_marker_abundance, defer=True,
    ),
    OutputSpec(
        "metaphlan_markers/marker_presence.tsv.gz",
        "marker_presence", parsers.parse_marker_presence, defer=True,
    ),
]


def humann(bundle: str, metaphlan_profile: str, genefamilies: str, pathabundance: str,
           pathcoverage: str | None) -> list[OutputSpec]:
    """A HUMAnN bundle's outputs under ``humann/<bundle>/`` (pipeline ADR-0016/0018):
    the bundle's own MetaPhlAn profile and the unnormalized tables, full-depth
    reads only. File names are HUMAnN's native ones, which differ by release.
    Gene families are 120k–1.7M rows/sample in the pilot (#88/#98), so they are
    deferred like the markers; pathways are ~0.4k–8k."""
    d = f"humann/{bundle}"
    tags = {"data_type": "full_data", "humann_bundle": bundle}
    specs = [
        OutputSpec(f"{d}/metaphlan/metaphlan_rel_ab_w_read_stats.tsv", "taxonomic_profile_metaphlan",
                   parsers.parse_metaphlan_profile, branched=False,
                   tags={**tags, "metaphlan_profile": metaphlan_profile}),
        OutputSpec(f"{d}/{genefamilies}", "humann_genefamilies", parsers.parse_humann_genefamilies,
                   branched=False, defer=True, tags=tags),
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
}

BRANCHES = ("full_data", "rarefied_data")

# The main MetaPhlAn pass's profile when a registration doesn't set
# `metaphlan_profile` (pipeline default since 2.3.0; 2.2.x ran the same
# MetaPhlAn 4.2.2 + vJan25 index).
DEFAULT_METAPHLAN_PROFILE = "mpa4.2.2_vJan25"
