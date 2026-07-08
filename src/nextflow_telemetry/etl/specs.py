"""Declarative per-(workflow, version) output specs.

The only thing that changes when a pipeline version adds/renames/moves a file is
this registry. Frozen dataclass, not pydantic: developer-authored config, not an
untrusted trust boundary.

A spec's ``subpath`` is relative to the branch dir (``full_data``/``rarefied_data``)
when ``branched`` is True, or to the sample root otherwise. ``defer=True`` marks
the marker tables (~89% of all rows) — spec'd for completeness but skipped by the
default ingest until the marker-store decision is made.
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


SPECS: dict[tuple[str, str], list[OutputSpec]] = {
    ("cmgd_nextflow", "2.2.1"): [
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
    ],
}

BRANCHES = ("full_data", "rarefied_data")
