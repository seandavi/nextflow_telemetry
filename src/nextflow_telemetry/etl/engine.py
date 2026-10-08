"""The generic ingest engine: locate → gate → parse → attach common columns →
write → watermark. Written once; the per-registration variation lives entirely
in the spec registry.
"""
from __future__ import annotations

from collections import defaultdict

import asyncpg  # type: ignore[import-untyped]
import duckdb

from . import lake, source, watermark
from .parsers import parse_qc
from .specs import BRANCHES, DEFAULT_METAPHLAN_PROFILE, SPECS
from .v2 import CompletedJob, Registration


def pending(jobs: list[CompletedJob], ingested: set[str]) -> list[CompletedJob]:
    """Completed jobs not yet in the watermark, oldest completion first."""
    return sorted((j for j in jobs if j.sample_key not in ingested),
                  key=lambda j: j.completed_at or "")


async def process(pg: asyncpg.Connection, con: duckdb.DuckDBPyConnection,
                  reg: Registration, jobs: list[CompletedJob],
                  include_deferred: bool = False) -> dict:
    specs = SPECS.get((reg.workflow_id, reg.version))
    if specs is None:
        raise ValueError(f"no OutputSpec registered for {reg.workflow_id} {reg.version}")
    profile = reg.params.get("metaphlan_profile", DEFAULT_METAPHLAN_PROFILE)

    summary: dict = {"ingested": 0, "skipped_unpublished": 0, "tables": defaultdict(int)}
    for job in jobs:
        prefix = source.locate(reg.workflow_id, reg.version, job.sample_key)
        manifest = source.fetch(prefix, "manifest.json") if prefix else None
        if prefix is None or manifest is None:
            summary["skipped_unpublished"] += 1
            continue

        qc_row = next(parse_qc(manifest), {})
        common = {
            "sample_key": job.sample_key,
            "sample_id": job.sample_id,
            "readset_id": job.readset_id,
            "study_name": job.study_name,
            "run_ids": qc_row.get("run_ids"),
            "workflow_id": reg.workflow_id,
            "version": reg.version,
            "metaphlan_profile": profile,
        }

        rows_by_table: dict[str, list[dict]] = defaultdict(list)
        for spec in specs:
            if spec.defer and not include_deferred:
                continue
            if not spec.branched:
                if spec.table == "qc_metrics":
                    # The manifest's own profile/bundle win over the registration's.
                    rows_by_table["qc_metrics"].append({**common, **qc_row})
                    continue
                data = source.fetch(prefix, spec.subpath)
                if data:
                    for r in spec.parser(data):
                        rows_by_table[spec.table].append({**common, **spec.tags, **r})
                continue
            for branch in BRANCHES:
                data = source.fetch(f"{prefix}/{branch}", spec.subpath)
                if not data:
                    continue
                for r in spec.parser(data):
                    rows_by_table[spec.table].append(
                        {**common, "data_type": branch, **spec.tags, **r})

        con.execute("BEGIN TRANSACTION")
        try:
            counts = {t: lake.replace_sample(con, t, job.sample_key, reg.workflow_id, reg.version, rows)
                      for t, rows in rows_by_table.items()}
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        # lake is committed before the watermark: a crash here re-ingests next
        # run, and replace_sample makes that a no-op-equivalent replace.
        await watermark.mark_ingested(pg, job.sample_key, reg.workflow_id, reg.version, counts)
        summary["ingested"] += 1
        for t, n in counts.items():
            summary["tables"][t] += n

    summary["tables"] = dict(summary["tables"])
    return summary
