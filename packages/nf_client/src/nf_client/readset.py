"""Readset ids (ADR-0007): ``RS.`` + the refget Sequence Collections v1.0.0
digest of ``{"units": [...]}``, over the deduplicated, code-point-sorted set of
``insdc.sra:<run>`` CURIEs.

The control plane computes the ids it hands the pipeline (``cf/src/readset.ts``);
this is the Python twin for anyone naming readsets offline. Both are pinned by
the ADR's golden vectors (``tests/test_readset.py``, ``cf/test/readset.test.ts``),
as is metacurator's ``readset.py``.
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


def insdc_unit(run_accession: str) -> str:
    """``SRR1`` -> ``insdc.sra:SRR1``; only INSDC run accessions are units."""
    acc = run_accession.strip()
    if not RUN_ACCESSION.match(acc):
        raise ValueError(f"not an INSDC run accession: {run_accession!r}")
    return "insdc.sra:" + acc


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
