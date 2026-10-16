#!/usr/bin/env python3
"""Rank Olm Microbiome Repo gut studies as add-study candidates (#269, plan §2 a′).

Reads one unpacked Olm data package (gut_studies, gut_runs,
gut_sandpiper_sample_summary parquet), drops studies already registered on v2
(GET /cohorts), drops studies where more than 10 % of profiled samples carry a
Sandpiper QC flag or whose samples are too shallow or too few to be useful
(median < 1 Gbp, < 10 samples), and ranks the rest by median Gbp per sample (cost), then
metadata coverage (age, sex, health condition). Prints a table and one
`gh issue create` command per candidate. It never files issues itself.

    uv run python -I scripts/olm_candidates.py <olm_pkg_dir> [--limit 20] [--only PRJNA1,PRJEB2]

Run it from outside the package directory: the package is downloaded data.
ponytail: reads the parquet files directly; point it at the lake's cmgd_meta.olm_* tables
once Phase 1's `nf-meta` lands, and make it `nf-meta candidates`.
"""
from __future__ import annotations

import argparse
import json
import shlex
import urllib.request
from pathlib import Path

import duckdb

V2 = "https://nf-telemetry.seandavi.workers.dev/api"
CORE_H_PER_SAMPLE = 24  # same rehearsal constant as services/submission.py
MAX_FLAGGED_SHARE = 0.10

SQL = """
with runs as (
  select study_accession, sample_key, sum(base_count::double) / 1e9 as gbp
  from read_parquet($runs)
  where sample_key is not null and library_strategy = 'WGS'
    and instrument_platform in ('ILLUMINA', 'BGISEQ', 'DNBSEQ')
  group by all
), size as (
  select study_accession, count(*) as samples, sum(gbp) as gbp, median(gbp) as median_gbp
  from runs group by all
), qc as (
  select study_accession,
         avg((coalesce(qc_low_depth, false) or coalesce(qc_low_complexity, false)
              or coalesce(qc_non_metagenome_strict, false))::int) as flagged_share
  from read_parquet($sandpiper) group by all
)
select s.study_accession, coalesce(s.short_title, s.study_title) as title,
       z.samples, round(z.gbp, 1) as gbp, round(z.median_gbp, 2) as median_gbp,
       round((coalesce(s.cov_age_at_collection_days, 0) + coalesce(s.cov_sex, 0)
              + coalesce(s.cov_health_condition, 0)) / 3, 2) as coverage,
       round(coalesce(q.flagged_share, 0), 3) as flagged_share
from read_parquet($studies) s
join size z using (study_accession)
left join qc q using (study_accession)
where s.release_retired is null
  and coalesce(q.flagged_share, 0) <= $max_flagged
  and z.median_gbp >= $min_gbp and z.samples >= $min_samples
  and s.study_accession not in (select unnest($registered::varchar[]))
  and (len($only::varchar[]) = 0 or s.study_accession in (select unnest($only::varchar[])))
order by median_gbp, coverage desc
limit $limit
"""


def registered_collections(api: str = V2) -> list[str]:
    req = urllib.request.Request(f"{api}/cohorts", headers={"User-Agent": "cmgd-olm-candidates"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return [c["collection_id"] for c in json.load(r)]


def candidates(pkg: Path, registered: list[str], only: list[str], limit: int,
               min_gbp: float = 1.0, min_samples: int = 10) -> list[dict]:
    con = duckdb.connect()
    rel = con.execute(SQL, {
        "runs": str(pkg / "gut_runs.parquet"),
        "studies": str(pkg / "gut_studies.parquet"),
        "sandpiper": str(pkg / "gut_sandpiper_sample_summary.parquet"),
        "max_flagged": MAX_FLAGGED_SHARE,
        "min_gbp": min_gbp,
        "min_samples": min_samples,
        "registered": registered,
        "only": only,
        "limit": limit,
    })
    cols = [d[0] for d in rel.description]
    return [dict(zip(cols, row)) for row in rel.fetchall()]


def issue_command(c: dict) -> str:
    body = (
        f"### Accession\n\n{c['study_accession']}\n\n### Notes (optional)\n\n"
        f"From scripts/olm_candidates.py: {c['samples']} samples, {c['gbp']} Gbp "
        f"(median {c['median_gbp']}), Olm metadata coverage {c['coverage']}, "
        f"Sandpiper QC-flagged share {c['flagged_share']}. {c['title']}"
    )
    return (f"gh issue create --title {shlex.quote('[add-study] ' + c['study_accession'])} "
            f"--label add-study --body {shlex.quote(body)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pkg", type=Path, help="unpacked Olm data package directory")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--only", default="", help="comma-separated BioProjects to restrict to")
    ap.add_argument("--api", default=V2)
    a = ap.parse_args()
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    rows = candidates(a.pkg, registered_collections(a.api), only, a.limit)
    print(f"{'study':<14} {'samples':>7} {'Gbp':>8} {'med':>6} {'cov':>5} {'flag':>6} {'core-h':>7}  title")
    for c in rows:
        print(f"{c['study_accession']:<14} {c['samples']:>7} {c['gbp']:>8} {c['median_gbp']:>6} "
              f"{c['coverage']:>5} {c['flagged_share']:>6} {CORE_H_PER_SAMPLE * c['samples']:>7}  "
              f"{(c['title'] or '')[:60]}")
    print()
    for c in rows:
        print(issue_command(c))


if __name__ == "__main__":
    main()
