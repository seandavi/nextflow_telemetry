"""The generic ingest engine: fetch → gate → parse → attach common columns →
write → watermark. Written once; the per-version variation lives entirely in the
spec registry.
"""
from __future__ import annotations

from collections import defaultdict

import asyncpg  # type: ignore[import-untyped]
import duckdb

from . import lake, source, watermark
from .parsers import parse_qc
from .specs import BRANCHES, SPECS


async def process(pg: asyncpg.Connection, con: duckdb.DuckDBPyConnection,
                  sample_ids: list[str], workflow: str, version: str,
                  include_markers: bool = False) -> dict:
    specs = SPECS.get((workflow, version))
    if specs is None:
        raise ValueError(f"no OutputSpec registered for {workflow} {version}")

    studies = await watermark.study_map(pg, sample_ids)
    summary: dict = {"ingested": 0, "skipped_unpublished": 0, "tables": defaultdict(int)}

    for sid in sample_ids:
        if not source.is_published(workflow, version, sid):
            summary["skipped_unpublished"] += 1
            continue
        prefix = source.sample_prefix(workflow, version, sid)
        manifest = source.fetch(prefix, "manifest.json")
        if manifest is None:
            summary["skipped_unpublished"] += 1
            continue

        qc_row = next(parse_qc(manifest), {})
        common = {
            "sample_id": sid,
            "study_name": studies.get(sid),
            "run_ids": qc_row.get("run_ids"),
            "workflow": workflow,
            "version": version,
        }

        rows_by_table: dict[str, list[dict]] = defaultdict(list)
        for spec in specs:
            if spec.defer and not include_markers:
                continue
            if not spec.branched:
                if spec.table == "qc_metrics":
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
            counts = {t: lake.replace_sample(con, t, sid, workflow, version, rows)
                      for t, rows in rows_by_table.items()}
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        # lake is committed before the watermark: a crash here re-ingests next
        # run, and replace_sample makes that a no-op-equivalent replace.
        await watermark.mark_ingested(pg, sid, workflow, version, counts)
        summary["ingested"] += 1
        for t, n in counts.items():
            summary["tables"][t] += n

    summary["tables"] = dict(summary["tables"])
    return summary
