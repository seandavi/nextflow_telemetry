#!/usr/bin/env python3
"""Format an add-study dry-run receipt (JSON on stdin) into a GitHub issue comment.

Used by .github/workflows/add-study.yml so the approver sees the sample counts
plus the ENA library composition — a technical sanity check that the study is
shotgun WGS metagenomics, not 16S/amplicon. Advisory only; never blocks.

    nf-client submit-study PRJEBxxxxx --dry-run --json | python scripts/format_study_preview.py PRJEBxxxxx
"""
from __future__ import annotations

import json
import sys


def _fmt(d: dict) -> str:
    return ", ".join(f"{k} ({n})" for k, n in d.items()) or "—"


def render(accession: str, receipt: dict) -> str:
    comp = receipt.get("library_composition") or {}
    warnings = receipt.get("warnings") or []
    lines = [
        f"🔍 **Dry-run: `{accession}`** — found **{receipt['samples_found']}** samples "
        f"(**{receipt['samples_added']}** new, **{receipt['samples_existing']}** already registered).",
        "",
    ]
    if comp:
        lines += [
            "**Library composition** (sanity check — expect shotgun WGS, not 16S/amplicon):",
            f"- library_strategy: {_fmt(comp.get('library_strategy', {}))}",
            f"- library_selection: {_fmt(comp.get('library_selection', {}))}",
            f"- library_source: {_fmt(comp.get('library_source', {}))}",
            f"- instrument_platform: {_fmt(comp.get('instrument_platform', {}))}",
            "",
        ]
    for w in warnings:
        lines.append(f"⚠️ {w}")
    if warnings:
        lines.append("")
    lines.append("A maintainer: add the **`approved`** label to register these for real.")
    return "\n".join(lines)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "?"
    print(render(acc, json.load(sys.stdin)))
