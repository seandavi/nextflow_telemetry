"""Shared utility functions."""
from __future__ import annotations

import hashlib


def normalize_srrs(srrs: list[str]) -> str:
    """Return canonical semicolon-separated SRR string: sorted and deduplicated."""
    return ";".join(sorted(set(s.strip() for s in srrs if s.strip())))


def srrs_to_sample_id(srrs: list[str]) -> str:
    """Return the md5 hex sample_id for a set of SRR accessions.

    The canonical form (sorted, deduplicated, semicolon-joined) is hashed so
    that the same input set always produces the same ID regardless of order.
    """
    canonical = normalize_srrs(srrs)
    return hashlib.md5(canonical.encode()).hexdigest()


def parse_srrs(ncbi_accession: str) -> list[str]:
    """Parse a semicolon-separated SRR string into a list of accessions."""
    return [s.strip() for s in ncbi_accession.split(";") if s.strip()]
