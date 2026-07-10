"""SRR accession helpers — content-addressed sample identity.

Replicated (not imported) from the server's `nextflow_telemetry.utils` because
nf-client is a standalone, independently-deployable package with no dependency
on the server package. The algorithm is a frozen content-addressing spec — the
md5 of the sorted, deduplicated, semicolon-joined SRR set — so the two copies
must produce identical ids. `tests/test_srr.py` pins that parity with golden
values; if the server spec ever changes, both sides and the golden must change
together (which would rehash the entire catalog, so it effectively never does).
"""
from __future__ import annotations

import hashlib
import re

# A real SRA / ENA / DDBJ run accession (SRR/ERR/DRR + digits). Used to reject
# curation-TSV placeholders (e.g. "Not applicable") before registering.
RUN_ACCESSION = re.compile(r"\b[SED]RR\d+\b")


def normalize_srrs(srrs: list[str]) -> str:
    """Return canonical semicolon-separated SRR string: sorted and deduplicated."""
    return ";".join(sorted(set(s.strip() for s in srrs if s.strip())))


def srrs_to_sample_id(srrs: list[str]) -> str:
    """Return the md5 hex sample_id for a set of SRR accessions."""
    return hashlib.md5(normalize_srrs(srrs).encode()).hexdigest()


def parse_srrs(ncbi_accession: str) -> list[str]:
    """Parse a semicolon-separated SRR string into a list of accessions."""
    return [s.strip() for s in ncbi_accession.split(";") if s.strip()]


def derive_sample_id(ncbi_accession: str) -> str | None:
    """Derive the content-addressed sample_id from a raw ncbi_accession string.

    Returns None if the string carries no real run accession (curation TSVs use
    placeholders like "Not applicable" that would otherwise seed an unrunnable
    sample). Callers should skip rows for which this returns None.
    """
    if not ncbi_accession or not RUN_ACCESSION.search(ncbi_accession):
        return None
    srrs = parse_srrs(ncbi_accession)
    return srrs_to_sample_id(srrs) if srrs else None
