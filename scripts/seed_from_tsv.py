#!/usr/bin/env python3
"""Seed the telemetry DB from a curation TSV file.

Registers samples using content-addressed sample_ids (md5 of SRR accessions),
registers the nf_testing stub workflow, then calls reconcile to create pending jobs.

Usage:
    uv run python scripts/seed_from_tsv.py [--tsv PATH] [--server URL]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_TSV = REPO_ROOT / "ArtachoA_2021_sample.tsv"
DEFAULT_SERVER = "http://localhost:8000"

# A real SRA / ENA / DDBJ run accession (SRR/ERR/DRR + digits). Used to reject
# curation-TSV placeholders (e.g. "Not applicable") before seeding.
_RUN_ACCESSION = re.compile(r"\b[SED]RR\d+\b")

NF_TESTING_WORKFLOW = {
    "workflow_id": "nf_testing",
    "version": "0.1.0",
    "repository_url": str(REPO_ROOT / "nf_testing" / "main.nf"),
    "revision": "local",
    "profile": "test",
    "max_retries": 1,
    "description": "Stub metagenomics pipeline for E2E testing",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed telemetry DB from TSV")
    parser.add_argument("--tsv", type=Path, default=DEFAULT_TSV)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    args = parser.parse_args()

    # Import here so the script works without installing the package
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nextflow_telemetry.utils import srrs_to_sample_id, parse_srrs

    server = args.server.rstrip("/")
    client = httpx.Client(base_url=f"{server}/api/", timeout=30)

    try:
        httpx.get(f"{server}/health", timeout=10).raise_for_status()
    except Exception as e:
        print(f"ERROR: cannot reach {args.server}: {e}", file=sys.stderr)
        sys.exit(1)

    with args.tsv.open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    print(f"Loaded {len(rows)} rows from {args.tsv.name}")

    registered = skipped = 0
    for row in rows:
        ncbi_accession = row.get("ncbi_accession", "").strip()
        cohort = row.get("study_name", "").strip()

        # Hygiene: skip rows without a real run accession. Curation TSVs use
        # placeholders like "Not applicable" in ncbi_accession, which parse_srrs
        # is lenient enough to pass through — seeding one produces a sample whose
        # fasterq_dump can never succeed. Require at least one SRA/ENA/DDBJ run
        # accession token.
        if not ncbi_accession or not _RUN_ACCESSION.search(ncbi_accession):
            skipped += 1
            continue

        srrs = parse_srrs(ncbi_accession)
        sample_id = srrs_to_sample_id(srrs)

        resp = client.post("samples", json={
            "sample_id": sample_id,
            "ncbi_accession": ncbi_accession,
            "metadata": {"cohort": cohort},
        })
        if resp.status_code not in (200, 201):
            print(f"  WARN: {sample_id} ({ncbi_accession}): {resp.status_code} {resp.text}", file=sys.stderr)
        else:
            registered += 1

    print(f"Samples: {registered} registered, {skipped} skipped")

    resp = client.post("workflows", json=NF_TESTING_WORKFLOW)
    if resp.status_code in (200, 201):
        wf = resp.json()
        print(f"Workflow: {wf['workflow_id']} v{wf['version']} registered (id={wf['id']})")
    else:
        print(f"WARN: workflow: {resp.status_code} {resp.text}", file=sys.stderr)

    resp = client.post("admin/reconcile-jobs")
    resp.raise_for_status()
    result = resp.json()
    print(f"Reconcile: {result['jobs_created']} pending jobs created")
    print("Done.")


if __name__ == "__main__":
    main()
