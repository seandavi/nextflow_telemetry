#!/usr/bin/env python3
"""Format an add-study dry-run receipt (JSON on stdin) into a GitHub issue comment.

Used by .github/workflows/add-study.yml so the approver sees the sample counts,
the processing cost estimate, the work already queued, and the ENA library
composition — a technical sanity check that the study is shotgun WGS
metagenomics, not 16S/amplicon. Advisory only; never blocks.

    nf-client submit-study PRJEBxxxxx --dry-run --json | python scripts/format_study_preview.py PRJEBxxxxx

With $NF_V2_URL set (the v2 Worker API base), the comment also shows the
unfinished jobs across active registrations: the approved-but-unfinished budget.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

UNFINISHED = ("pending", "claimed", "submitted", "running")


def _fmt(d: dict) -> str:
    return ", ".join(f"{k} ({n})" for k, n in d.items()) or "—"


def backlog(api: str) -> int:
    """Unfinished jobs across active workflow registrations on v2 (GETs are public)."""
    def get(path: str):
        # Cloudflare rejects urllib's default User-Agent with a 403.
        req = urllib.request.Request(f"{api.rstrip('/')}/{path}", headers={"User-Agent": "cmgd-add-study"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    total = 0
    for w in get("workflows?status=active"):
        counts = get(f"workflows/{w['id']}/job-summary")
        total += sum(int(counts.get(s) or 0) for s in UNFINISHED)
    return total


def render(accession: str, receipt: dict, queued: int | None = None) -> str:
    comp = receipt.get("library_composition") or {}
    warnings = receipt.get("warnings") or []
    est = receipt.get("estimate")
    lines = [
        f"🔍 **Dry-run: `{accession}`** — found **{receipt['samples_found']}** samples "
        f"(**{receipt['samples_added']}** new, **{receipt['samples_existing']}** already registered).",
        "",
    ]
    if est:
        lines += [
            f"**Estimate** ({est['samples']} samples, {est['gbp']} Gbp, median {est['median_gbp']} Gbp/sample): "
            f"**{est['core_h']:,} core-h** (24/sample), **{est['anvil_su']:,} Anvil SU** (depth model), "
            f"peak scratch {est['peak_scratch_gb_per_sample']} GB per running sample.",
            "",
        ]
    if queued is not None:
        lines += [f"**Already queued:** {queued:,} unfinished jobs ≈ {24 * queued:,} core-h.", ""]
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
    receipt = json.load(sys.stdin)
    queued = None
    if os.environ.get("NF_V2_URL"):
        try:
            queued = backlog(os.environ["NF_V2_URL"])
        except Exception as e:  # advisory only: the preview still posts
            print(f"backlog lookup failed: {e}", file=sys.stderr)
    print(render(acc, receipt, queued))
