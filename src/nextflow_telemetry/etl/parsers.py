"""File→row parsers for cmgd_nextflow published outputs.

Each parser is a plain function ``bytes -> Iterator[dict]`` of file-native fields.
The engine attaches the common columns (sample_id, study_name, ..., data_type,
and the spec's tags); parsers only know their file's own shape. Composition, not
inheritance — a genuinely new file shape is one new function.

Grounded in 2.2.1 output (July 2026): metaphlan uses ``|``-delimited clade
lineages with a matching taxid lineage; bracken is a normal-header TSV whose
``fraction_total_reads`` is a 0–1 fraction; card_kma.res is a ``#``-header TSV
with whitespace-padded numerics.
"""
from __future__ import annotations

import gzip
import json
from typing import Iterator

# metaphlan/bracken single-letter rank prefixes → canonical rank
_RANK = {
    "d": "domain", "k": "kingdom", "p": "phylum", "c": "class",
    "o": "order", "f": "family", "g": "genus", "s": "species", "t": "strain",
}
_BRACKEN_LVL = {
    "D": "domain", "K": "kingdom", "P": "phylum", "C": "class",
    "O": "order", "F": "family", "G": "genus", "S": "species",
}


def _lines(raw: bytes) -> list[str]:
    text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", "replace")
    return text.splitlines()


def _rows(raw: bytes, comment: str | None = "#"):
    """Yield tab-split fields, skipping blank and (optionally) comment lines."""
    for line in _lines(raw):
        if not line.strip():
            continue
        if comment and line.startswith(comment):
            continue
        yield line.split("\t")


def _as_int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() and s != "-1" else None


def _as_float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse_metaphlan_profile(raw: bytes) -> Iterator[dict]:
    """metaphlan marker_rel_ab_w_read_stats → taxonomic_profile_metaphlan rows.

    Columns (no real header — every leading line is a ``#`` comment):
    clade_name, clade_taxid (lineage of NCBI taxids), relative_abundance,
    coverage, estimated_reads. **Native units**: metaphlan `relative_abundance`
    is a percent (sums to ~100 within a rank) — kept as-reported, not normalized,
    since it lives in its own table now. The terminal taxid is the row's NCBI
    taxid; a ``t__SGB`` leaf carries the SGB id. ``coverage`` is ``-`` above the
    SGB leaves (→ None).
    """
    for f in _rows(raw):
        if len(f) < 3:
            continue
        clade = f[0]
        last = clade.split("|")[-1]
        rank = _RANK.get(last[0]) if len(last) > 2 and last[1] == "_" else None
        rel = _as_float(f[2])
        if rel is None:
            continue
        yield {
            "clade_name": clade,
            "rank": rank,
            "ncbi_taxid": _as_int(f[1].split("|")[-1]),
            "sgb_id": last if last.startswith("t__SGB") else None,
            "relative_abundance": rel,
            "coverage": _as_float(f[3]) if len(f) > 3 else None,
            "estimated_reads": int(float(f[4])) if len(f) > 4 and f[4].strip() not in ("", "-") else None,
        }


def parse_bracken(raw: bytes) -> Iterator[dict]:
    """bracken.species/genus → taxonomic_profile_bracken rows. Normal header row;
    ``fraction_total_reads`` is a read-count fraction (0–1, native) — a different
    interpretation from metaphlan's percent, which is why bracken has its own
    table."""
    rows = _rows(raw, comment=None)
    header = next(rows, None)
    if not header:
        return
    idx = {h: i for i, h in enumerate(header)}
    for f in rows:
        if len(f) < len(header):
            continue
        yield {
            "clade_name": f[idx["name"]],
            "rank": _BRACKEN_LVL.get(f[idx["taxonomy_lvl"]].strip()),
            "ncbi_taxid": _as_int(f[idx["taxonomy_id"]]),
            "fraction_total_reads": float(f[idx["fraction_total_reads"]]),
            "estimated_reads": _as_int(f[idx["new_est_reads"]]),
        }


def parse_resistome(raw: bytes) -> Iterator[dict]:
    """card_kma.res → resistome rows. ``#Template``-prefixed header; numeric
    fields are whitespace-padded (``float`` tolerates the leading spaces)."""
    rows = _rows(raw, comment=None)
    header = next(rows, None)
    if not header:
        return
    header = [h.lstrip("#").strip() for h in header]
    idx = {h: i for i, h in enumerate(header)}
    for f in rows:
        if len(f) < len(header):
            continue
        yield {
            "gene": f[idx["Template"]].strip(),
            "template_coverage": float(f[idx["Template_Coverage"]]),
            "template_identity": float(f[idx["Template_Identity"]]),
            "depth": float(f[idx["Depth"]]),
            "score": float(f[idx["Score"]]),
        }


def parse_marker_abundance(raw: bytes) -> Iterator[dict]:
    """metaphlan marker_abundance → (marker_name, value). Deferred from the
    default ingest (markers are ~89% of all rows)."""
    for f in _rows(raw):
        if len(f) < 2:
            continue
        try:
            yield {"marker_name": f[0], "value": float(f[1])}
        except ValueError:
            continue


def parse_marker_presence(raw: bytes) -> Iterator[dict]:
    """metaphlan marker_presence → membership. Degenerate as published (every
    listed marker is present), so the boolean value is dropped — presence is
    encoded by row existence. Deferred from the default ingest."""
    for f in _rows(raw):
        if f and f[0]:
            yield {"marker_name": f[0]}


def parse_qc(raw: bytes) -> Iterator[dict]:
    """manifest.json → one qc_metrics row (per sample, no data_type)."""
    d = json.loads(raw)
    ra = d.get("read_accounting", {}) or {}
    raw_, dec = ra.get("raw", {}) or {}, ra.get("decontaminated", {}) or {}
    prov, params = d.get("provenance", {}) or {}, d.get("parameters", {}) or {}
    yield {
        "reads_raw": raw_.get("number_reads"),
        "reads_decontaminated": dec.get("number_reads"),
        "bases_raw": raw_.get("number_bases"),
        "bases_decontaminated": dec.get("number_bases"),
        "reads_surviving_fraction": ra.get("reads_surviving_fraction"),
        "bases_surviving_fraction": ra.get("bases_surviving_fraction"),
        "metaphlan_index": params.get("metaphlan_index"),
        "pipeline_version": prov.get("pipeline_version"),
        "git_commit": prov.get("git_commit"),
        "run_ids": ";".join(prov.get("input_ids", []) or []),
    }
