"""Readset ids (ADR-0007) and the canonical run list a sample is keyed on.

Twins: ``packages/nf_client/src/nf_client/readset.py`` and ``cf/src/readset.ts``. All
three are pinned by the ADR's golden vectors (``tests/test_readset.py``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

RUN_ACCESSION = re.compile(r"^[SED]RR\d+$")
UNIT = re.compile(r"^insdc\.sra:[SED]RR\d+$")


def sha512t24u(data: bytes) -> str:
    """seqcol footnote F3: base64url of the first 24 bytes of SHA-512."""
    return base64.urlsafe_b64encode(hashlib.sha512(data).digest()[:24]).decode("ascii")


def canonical_json(obj: Any) -> bytes:
    """RFC 8785 canonical JSON for strings, integers, lists and dicts (no floats), as UTF-8."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")


def readset_id(units: Iterable[str]) -> str:
    """``RS.`` + seqcol digest of the deduplicated, code-point-sorted unit set."""
    u = sorted(set(units))
    if not u:
        raise ValueError("readset has no units")
    for x in u:
        if not UNIT.match(x):
            raise ValueError(f"invalid readset unit: {x!r}")
    level1 = {"units": sha512t24u(canonical_json(u))}
    return "RS." + sha512t24u(canonical_json(level1))


def normalize_runs(ncbi_accession: str) -> str:
    """Sorted, deduplicated, ``;``-joined run list (the canonical ``ncbi_accession``)."""
    parts = (p.strip() for p in re.split(r"[;,\s]+", ncbi_accession))
    return ";".join(sorted({p for p in parts if p}))


def readset_id_for_runs(normalized: str) -> str | None:
    """Readset id of a canonical run list, or None when it is empty or holds a non-run entry."""
    runs = [r for r in normalized.split(";") if r]
    if not runs or not all(RUN_ACCESSION.match(r) for r in runs):
        return None
    return readset_id("insdc.sra:" + r for r in runs)
