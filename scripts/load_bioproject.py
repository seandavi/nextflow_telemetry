#!/usr/bin/env python3
"""Load samples for a BioProject from the omicidx SRA parquet.

Pulls (sra_sample, biosample, bioproject, sra_study, srr) rows from
https://data-omicidx.cancerdatasci.org/sra/parquet/sra_accessions.parquet,
groups SRRs into a sorted semicolon-joined list per sample, and either
writes a TSV (same shape as ArtachoA_2021_sample.tsv) or POSTs directly
to /api/samples on a running telemetry server.

Usage:
    uv run python scripts/load_bioproject.py PRJNA000000 -o samples.tsv
    uv run python scripts/load_bioproject.py PRJNA000000 --server http://localhost:8000
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import click
import duckdb
import httpx

REPO_ROOT = Path(__file__).parent.parent
PARQUET_URL = "https://data-omicidx.cancerdatasci.org/sra/parquet/sra_accessions.parquet"

QUERY = """
WITH filtered AS (
    SELECT DISTINCT
        sample AS sra_sample,
        biosample,
        bioproject,
        study AS sra_study,
        accession AS srr
    FROM read_parquet(?)
    WHERE bioproject = ? AND type = 'RUN'
)
SELECT
    sra_sample,
    biosample,
    bioproject,
    sra_study,
    string_agg(srr, ';' ORDER BY srr) AS ncbi_accession
FROM filtered
GROUP BY sra_sample, biosample, bioproject, sra_study
ORDER BY sra_sample
"""

COLUMNS = ["ncbi_accession", "sra_sample", "biosample", "bioproject", "sra_study"]


def fetch_rows(bioproject: str) -> list[dict[str, str]]:
    with duckdb.connect(":memory:") as con:
        con.execute("SET enable_progress_bar = false")
        cur = con.execute(QUERY, [PARQUET_URL, bioproject])
        rows = cur.fetchall()
        fields = [d[0] for d in cur.description]
    return [dict(zip(fields, r)) for r in rows]


def write_tsv(rows: list[dict[str, str]], out: Path | None) -> None:
    f = out.open("w", newline="") if out else sys.stdout
    try:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    finally:
        if out:
            f.close()


def upload_rows(rows: list[dict[str, str]], server: str, cohort: str | None) -> None:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nextflow_telemetry.utils import parse_srrs, srrs_to_sample_id

    server = server.rstrip("/")

    with httpx.Client(base_url=server, timeout=30) as client:
        try:
            client.get("/health", timeout=10).raise_for_status()
        except Exception as e:
            raise click.ClickException(f"cannot reach {server}: {e}")

        registered = failed = 0
        for row in rows:
            srrs = parse_srrs(row["ncbi_accession"])
            if not srrs:
                continue
            sample_id = srrs_to_sample_id(srrs)
            metadata = {
                "bioproject": row["bioproject"],
                "sra_study": row["sra_study"],
                "sra_sample": row["sra_sample"],
            }
            if cohort:
                metadata["cohort"] = cohort

            resp = client.post(
                "/api/samples",
                json={
                    "sample_id": sample_id,
                    "ncbi_accession": row["ncbi_accession"],
                    "biosample_id": row["biosample"],
                    "metadata": metadata,
                },
            )
            if resp.status_code in (200, 201):
                registered += 1
            else:
                failed += 1
                click.echo(
                    f"  WARN: {sample_id} ({row['sra_sample']}): {resp.status_code} {resp.text}",
                    err=True,
                )

    click.echo(f"Uploaded: {registered} registered, {failed} failed")


@click.command()
@click.argument("bioproject")
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write TSV here (default: stdout if --server not given).",
)
@click.option(
    "--server",
    help="Telemetry server URL. If set, uploads via POST /api/samples instead of writing TSV.",
)
@click.option("--cohort", help="Cohort label added to each sample's metadata.")
def main(bioproject: str, output: Path | None, server: str | None, cohort: str | None) -> None:
    """Load samples for BIOPROJECT (e.g. PRJNA694605) from the omicidx parquet."""
    if output and server:
        raise click.UsageError("--output and --server are mutually exclusive")

    rows = fetch_rows(bioproject)
    click.echo(f"Found {len(rows)} samples for {bioproject}", err=True)
    if not rows:
        return

    if server:
        upload_rows(rows, server, cohort)
    else:
        write_tsv(rows, output)
        if output:
            click.echo(f"Wrote {output}", err=True)


if __name__ == "__main__":
    main()
