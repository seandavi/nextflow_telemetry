#!/usr/bin/env python3
"""Seed the telemetry DB from a curation TSV file.

Registers samples (sample_id + ncbi_accession + cohort metadata), registers the
nf_testing stub workflow, then calls reconcile to create pending jobs.

Usage:
    uv run python scripts/seed_from_tsv.py [--tsv PATH] [--server URL]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import httpx
import typer

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_TSV = REPO_ROOT / "ArtachoA_2021_sample.tsv"
DEFAULT_SERVER = "http://localhost:8000"

NF_TESTING_WORKFLOW = {
    "workflow_id": "nf_testing",
    "version": "0.1.0",
    "repository_url": str(REPO_ROOT / "nf_testing" / "main.nf"),
    "revision": "local",
    "profile": "test",
    "max_retries": 1,
    "description": "Stub metagenomics pipeline for E2E testing",
}

app = typer.Typer(add_completion=False)


@app.command()
def main(
    tsv: Path = typer.Option(DEFAULT_TSV, "--tsv", help="Path to sample TSV file"),
    server: str = typer.Option(DEFAULT_SERVER, "--server", help="API base URL"),
) -> None:
    """Register samples and workflow from TSV, then reconcile jobs."""
    client = httpx.Client(base_url=server, timeout=30)

    # Health check
    try:
        client.get("/health").raise_for_status()
    except Exception as e:
        typer.echo(f"ERROR: cannot reach {server}: {e}", err=True)
        raise typer.Exit(1)

    # Load TSV
    with tsv.open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    typer.echo(f"Loaded {len(rows)} rows from {tsv.name}")

    # Register samples
    registered = skipped = 0
    for row in rows:
        sample_id = row["sample_id"].strip()
        ncbi_accession = row["ncbi_accession"].strip()
        cohort = row["study_name"].strip()

        if not sample_id:
            skipped += 1
            continue

        resp = client.post("/samples", json={
            "sample_id": sample_id,
            "metadata": {
                "ncbi_accession": ncbi_accession,
                "cohort": cohort,
            },
        })
        if resp.status_code not in (200, 201):
            typer.echo(f"  WARN: failed to register {sample_id}: {resp.status_code} {resp.text}", err=True)
        else:
            registered += 1

    typer.echo(f"Samples: {registered} registered, {skipped} skipped")

    # Register workflow
    resp = client.post("/workflows", json=NF_TESTING_WORKFLOW)
    if resp.status_code in (200, 201):
        wf = resp.json()
        typer.echo(f"Workflow: {wf['workflow_id']} v{wf['version']} registered (id={wf['id']})")
    else:
        typer.echo(f"WARN: workflow registration returned {resp.status_code}: {resp.text}", err=True)

    # Reconcile
    resp = client.post("/admin/reconcile-jobs")
    resp.raise_for_status()
    result = resp.json()
    typer.echo(f"Reconcile: {result['jobs_created']} pending jobs created")

    typer.echo("Done.")


if __name__ == "__main__":
    app()
