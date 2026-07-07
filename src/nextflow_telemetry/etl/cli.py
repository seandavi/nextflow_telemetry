"""nf-etl — the ETL command line. Plain scripts, no orchestrator.

  nf-etl status                     backlog + ingested counts
  nf-etl parse   --sample <id>      dry-run: per-table row counts (no DB/lake)
  nf-etl ingest  [--limit N]        ingest pending completed samples
  nf-etl tick    [--threshold 500]  ingest iff backlog >= threshold or age fallback
  nf-etl freeze  --out cmgd.duckdb  publish a frozen DuckDB-catalog snapshot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections import defaultdict

import duckdb

from . import engine, lake, source, watermark
from .parsers import parse_qc
from .specs import BRANCHES, SPECS

WORKFLOW = "cmgd_nextflow"


async def _version(pg, workflow: str, version: str | None) -> str:
    if version:
        return version
    v = await pg.fetchval(
        "SELECT version FROM workflows WHERE workflow_id = $1 AND status = 'active'", workflow)
    if not v:
        raise SystemExit(f"no active version for {workflow}; pass --version")
    return v


async def cmd_status(a) -> None:
    pg = await watermark.connect()
    await watermark.ensure_table(pg)
    version = await _version(pg, a.workflow, a.version)
    backlog = await watermark.backlog_count(pg, a.workflow, version)
    ingested = await pg.fetchval(
        "SELECT count(*) FROM etl_ingested WHERE workflow_id = $1 AND workflow_version = $2",
        a.workflow, version)
    print(f"workflow={a.workflow} version={version}")
    print(f"  ingested samples : {ingested}")
    print(f"  backlog          : {backlog}  (completed, not yet ingested)")
    await pg.close()


async def cmd_ingest(a) -> None:
    pg = await watermark.connect()
    await watermark.ensure_table(pg)
    version = await _version(pg, a.workflow, a.version)
    sids = await watermark.pending(pg, a.workflow, version, limit=a.limit)
    if not sids:
        print("nothing to ingest")
        await pg.close()
        return
    con = lake.connect()
    lake.ensure_schema(con)
    summary = await engine.process(pg, con, sids, a.workflow, version, include_markers=a.include_markers)
    con.close()
    await pg.close()
    print(json.dumps(summary))


async def cmd_tick(a) -> None:
    pg = await watermark.connect()
    await watermark.ensure_table(pg)
    version = await _version(pg, a.workflow, a.version)
    backlog = await watermark.backlog_count(pg, a.workflow, version)
    oldest_h = await pg.fetchval(
        """
        SELECT extract(epoch FROM (now() - min(j.completed_at))) / 3600 FROM jobs j
        WHERE j.status = 'completed' AND j.workflow_id = $1 AND j.workflow_version = $2
          AND NOT EXISTS (SELECT 1 FROM etl_ingested e WHERE e.sample_id = j.sample_id
              AND e.workflow_id = j.workflow_id AND e.workflow_version = j.workflow_version)
        """, a.workflow, version)
    trigger = backlog >= a.threshold or (backlog > 0 and oldest_h and oldest_h >= a.max_age_hours)
    if not trigger:
        age = f"{oldest_h:.1f}h" if oldest_h else "-"
        print(f"backlog {backlog} < {a.threshold} (oldest {age} < {a.max_age_hours}h) — skipping")
        await pg.close()
        return
    sids = await watermark.pending(pg, a.workflow, version, limit=a.batch)
    con = lake.connect()
    lake.ensure_schema(con)
    summary = await engine.process(pg, con, sids, a.workflow, version, include_markers=a.include_markers)
    con.close()
    await pg.close()
    print(json.dumps(summary))


def cmd_parse(a) -> None:
    """Dry-run: fetch + parse one sample, print per-table row counts. No DB, no lake."""
    specs = SPECS.get((a.workflow, a.version))
    if specs is None:
        raise SystemExit(f"no spec for {a.workflow} {a.version}")
    prefix = source.sample_prefix(a.workflow, a.version, a.sample)
    counts: dict[str, int] = defaultdict(int)
    for spec in specs:
        if spec.defer and not a.include_markers:
            continue
        if not spec.branched:
            data = source.fetch(prefix, spec.subpath)
            if data:
                counts[spec.table] += sum(1 for _ in spec.parser(data))
            continue
        for branch in BRANCHES:
            data = source.fetch(f"{prefix}/{branch}", spec.subpath)
            if data:
                counts[spec.table] += sum(1 for _ in spec.parser(data))
    print(json.dumps(dict(counts)))


def cmd_freeze(a) -> None:
    """Snapshot the working catalog into a frozen DuckDB-catalog file whose data
    references resolve over public HTTPS. DuckLake stores relative file paths but
    pins data_path in the catalog, so we copy the catalog and rewrite data_path to
    the public base (the parquet is the same shared R2 objects, read over https)."""
    shutil.copy(lake.CATALOG, a.out)
    con = duckdb.connect(a.out)
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'", [a.https_base])
    con.close()
    print(f"frozen catalog -> {a.out} (data_path={a.https_base})")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nf-etl", description="cMD output-catalog ETL")
    p.add_argument("--workflow", default=WORKFLOW)
    p.add_argument("--version", default=None, help="pipeline version (default: active)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    ing = sub.add_parser("ingest")
    ing.add_argument("--limit", type=int, default=None)
    ing.add_argument("--include-markers", action="store_true")
    tick = sub.add_parser("tick")
    tick.add_argument("--threshold", type=int, default=500)
    tick.add_argument("--max-age-hours", type=float, default=24.0)
    tick.add_argument("--batch", type=int, default=1000)
    tick.add_argument("--include-markers", action="store_true")
    pr = sub.add_parser("parse")
    pr.add_argument("--sample", required=True)
    pr.add_argument("--include-markers", action="store_true")
    fr = sub.add_parser("freeze")
    fr.add_argument("--out", required=True)
    fr.add_argument("--https-base", required=True, help="public HTTPS base for the parquet data")

    a = p.parse_args(argv)
    if a.cmd == "parse":
        cmd_parse(a)
    elif a.cmd == "freeze":
        cmd_freeze(a)
    else:
        asyncio.run({"status": cmd_status, "ingest": cmd_ingest, "tick": cmd_tick}[a.cmd](a))


if __name__ == "__main__":
    main()
