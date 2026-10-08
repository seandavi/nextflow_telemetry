"""The generic ingest engine: locate → gate → parse → attach common columns →
stage → write one batch per ``ops.run``. Written once; the per-registration
variation lives entirely in the spec registry.
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
from cdsci.lake import ops  # type: ignore[import-untyped]

from . import lake, source
from .parsers import parse_qc
from .specs import BRANCHES, DEFAULT_METAPHLAN_PROFILE, SPECS
from .v2 import CompletedJob, Registration

BATCH_SIZE = 500  # samples per lake write: ~one file per table per batch
FETCH_WORKERS = 8  # samples fetched + parsed concurrently


def pending(jobs: list[CompletedJob], ingested: set[str]) -> list[CompletedJob]:
    """Completed jobs not yet in the lake, oldest completion first."""
    return sorted((j for j in jobs if j.sample_key not in ingested),
                  key=lambda j: j.completed_at or "")


def _collect(reg: Registration, job: CompletedJob, profile: str) -> dict[str, list[dict]] | None:
    """Fetch and parse one sample's outputs, rows by table; None if unpublished."""
    prefix = source.locate(reg.workflow_id, reg.version, job.sample_key)
    manifest = source.fetch(prefix, "manifest.json") if prefix else None
    if prefix is None or manifest is None:
        return None

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
    rel = f"/{reg.workflow_id}/{reg.version}/{job.sample_key}"
    base = prefix.removesuffix(rel)

    rows_by_table: dict[str, list[dict]] = defaultdict(list)
    for spec in SPECS[(reg.workflow_id, reg.version)]:
        if spec.table == "qc_metrics":
            # The manifest's own profile/bundle win over the registration's.
            rows_by_table["qc_metrics"].append({**common, **qc_row})
            continue
        branches = [(f"{prefix}/{b}", {"data_type": b}) for b in BRANCHES] if spec.branched \
            else [(prefix, {})]
        for p, branch in branches:
            data = source.fetch(p, spec.subpath)
            if not data:
                continue
            if spec.indexed:  # the object's index row, not its contents
                tags = {**common, **branch, **spec.tags}
                rows_by_table[spec.table].append({
                    **tags, "branch": tags.get("data_type"),
                    "key": f"{p}/{spec.subpath}".removeprefix(f"{base}/"),
                    "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "rows": sum(1 for _ in spec.parser(data))})
            else:
                rows_by_table[spec.table].extend(
                    {**common, **branch, **spec.tags, **r} for r in spec.parser(data))
    return rows_by_table


def process(con: duckdb.DuckDBPyConnection, reg: Registration, jobs: list[CompletedJob],
            batch_size: int = BATCH_SIZE) -> dict:
    """Ingest ``jobs`` in batches of ``batch_size`` samples. Each batch is one
    ``ops.run`` (one ledger row) and one attributed lake snapshot; the
    registration's watermark records progress after each batch."""
    if (reg.workflow_id, reg.version) not in SPECS:
        raise ValueError(f"no OutputSpec registered for {reg.workflow_id} {reg.version}")
    profile = reg.params.get("metaphlan_profile", DEFAULT_METAPHLAN_PROFILE)
    label = f"{reg.workflow_id}/{reg.version}"
    ingested = lake.ingested_keys(con, reg.workflow_id, reg.version)

    summary: dict = {"ingested": 0, "skipped_unpublished": 0, "tables": defaultdict(int),
                     "runs": []}
    start = time.monotonic()
    for i in range(0, len(jobs), batch_size):
        lake.drop_stage(con)
        batch = jobs[i:i + batch_size]
        staged = []
        # Fetching is rclone-bound (~0.4 s per object), so samples are fetched and
        # parsed in parallel; staging stays on the one DuckDB connection, in
        # completion order so one slow sample doesn't buffer the rest in memory.
        with ThreadPoolExecutor(FETCH_WORKERS) as pool:
            futures = {pool.submit(_collect, reg, j, profile): j for j in batch}
            for fut in as_completed(futures):
                j, rows_by_table = futures[fut], fut.result()
                if rows_by_table is None:
                    continue
                for table, rows in rows_by_table.items():
                    lake.stage(con, table, rows)
                staged.append(j)
        summary["skipped_unpublished"] += len(batch) - len(staged)
        if not staged:
            continue
        keys = [j.sample_key for j in staged]
        with ops.run(con, source=lake.SOURCE.name, target=f"lake.{lake.SCHEMA}",
                     version=label) as r:
            counts = lake.write_batch(con, r, reg.workflow_id, reg.version,
                                      [k for k in keys if k in ingested])
            r.rows = sum(counts.values())
            ingested.update(keys)
            ops.set_watermark(con, lake.SOURCE.name, label, {
                "samples": len(ingested),
                "last_completed_at": max(j.completed_at or "" for j in staged) or None,
            }, run_id=r.run_id)
        summary["ingested"] += len(staged)
        summary["runs"].append(r.run_id)
        for t, n in counts.items():
            summary["tables"][t] += n

    summary["tables"] = dict(summary["tables"])
    summary["seconds"] = round(time.monotonic() - start, 1)
    summary["rows_per_s"] = round(sum(summary["tables"].values()) / max(summary["seconds"], 0.1))
    return summary
