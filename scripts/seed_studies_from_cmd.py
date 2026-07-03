#!/usr/bin/env python3
"""Seed samples from curatedMetagenomicData curation studies.

Fetches each study's `<study>_sample.tsv` straight from the waldronlab
curatedMetagenomicDataCuration repo, registers up to `--limit` samples per study
(content-addressed sample_id = md5 of the SRR set), tags each with
`metadata.cohort = <study>`, and reconciles once at the end.

Only rows with a real SRA/ENA/DDBJ run accession are seeded — curation TSVs
carry placeholders like "Not applicable" in `ncbi_accession`, and seeding one
produces a sample whose fasterq_dump can never succeed.

Usage:
    uv run --with httpx python scripts/seed_studies_from_cmd.py \
        --study WirbelJ_2018 --study GuptaA_2019 --limit 25 \
        --server https://nf-telemetry.cancerdatasci.org
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_BASE = (
    "https://raw.githubusercontent.com/waldronlab/"
    "curatedMetagenomicDataCuration/master/inst/curated"
)
_RUN_ACCESSION = re.compile(r"\b[SED]RR\d+\b")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed samples from cMD curation studies")
    ap.add_argument("--study", action="append", required=True, help="Study name (repeatable).")
    ap.add_argument("--limit", type=int, default=25, help="Max samples per study (default 25).")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--base", default=DEFAULT_BASE, help="Raw base URL for curated TSVs.")
    ap.add_argument("--no-reconcile", action="store_true", help="Skip the final reconcile call.")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nextflow_telemetry.utils import parse_srrs, srrs_to_sample_id

    server = args.server.rstrip("/")
    client = httpx.Client(base_url=f"{server}/api/", timeout=60)
    try:
        httpx.get(f"{server}/health", timeout=10).raise_for_status()
    except Exception as e:
        print(f"ERROR: cannot reach {server}: {e}", file=sys.stderr)
        sys.exit(1)

    total = rejected = 0
    for study in args.study:
        try:
            raw = urllib.request.urlopen(f"{args.base}/{study}/{study}_sample.tsv", timeout=30).read().decode()
        except Exception as e:
            print(f"  {study}: FETCH FAILED {e}", file=sys.stderr)
            continue
        rows = list(csv.DictReader(io.StringIO(raw), delimiter="\t"))
        seeded = rej = 0
        for row in rows:
            if seeded >= args.limit:
                break
            acc = (row.get("ncbi_accession") or "").strip()
            if not acc or not _RUN_ACCESSION.search(acc):
                if acc:
                    rej += 1
                continue
            srrs = parse_srrs(acc)
            if not srrs:
                continue
            sid = srrs_to_sample_id(srrs)
            resp = client.post("samples", json={
                "sample_id": sid, "ncbi_accession": acc, "metadata": {"cohort": study},
            })
            if resp.status_code in (200, 201):
                seeded += 1
            else:
                print(f"    WARN {study} {sid}: {resp.status_code} {resp.text[:100]}", file=sys.stderr)
        print(f"  {study:24s} seeded {seeded}  (rejected {rej} non-accession rows)")
        total += seeded
        rejected += rej

    print(f"TOTAL seeded {total}, rejected {rejected} non-accession rows")

    if not args.no_reconcile:
        r = client.post("admin/reconcile-jobs")
        r.raise_for_status()
        print(f"Reconcile: {r.json().get('jobs_created')} pending jobs created")


if __name__ == "__main__":
    main()
