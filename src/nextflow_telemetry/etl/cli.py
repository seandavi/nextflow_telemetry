"""nf-etl — the ETL command line. Plain scripts, no orchestrator.

Registrations and their completed jobs come from the v2 API (``NF_TELEMETRY_URL``);
every registration with an OutputSpec is processed, narrowed by ``--workflow`` /
``--version``.

  nf-etl status                                     per-registration backlog + ingested
  nf-etl --workflow W --version V parse --sample K  dry-run: per-table row counts (no DB/lake)
  nf-etl ingest  [--limit N]                        ingest pending completed samples
  nf-etl tick    [--threshold 500]                  ingest iff backlog >= threshold or age fallback
  nf-etl freeze  --out cmgd.duckdb                  publish a frozen DuckDB-catalog snapshot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections import defaultdict
from datetime import UTC, datetime

import duckdb
import httpx

from . import engine, lake, source, v2, watermark
from .specs import BRANCHES, SPECS


def _registrations(a) -> list[v2.Registration]:
    with httpx.Client(timeout=60) as client:
        regs = v2.registrations(client)
    return [r for r in regs
            if (r.workflow_id, r.version) in SPECS
            and a.workflow in (None, r.workflow_id) and a.version in (None, r.version)]


def _pending(reg: v2.Registration, ingested: set[str]) -> list[v2.CompletedJob]:
    with httpx.Client(timeout=60) as client:
        return engine.pending(v2.completed_jobs(client, reg), ingested)


def _age_hours(job: v2.CompletedJob) -> float | None:
    if not job.completed_at:
        return None
    return (datetime.now(UTC) - datetime.fromisoformat(job.completed_at)).total_seconds() / 3600


async def _each(a, run) -> None:
    """Open the watermark + (lazily) the lake once, call ``run`` per registration."""
    pg = await watermark.connect()
    await watermark.ensure_table(pg)
    con = None

    def lake_con():
        nonlocal con
        if con is None:
            con = lake.connect()
            lake.ensure_schema(con)
        return con

    try:
        for reg in _registrations(a):
            todo = _pending(reg, await watermark.ingested_keys(pg, reg.workflow_id, reg.version))
            await run(pg, lake_con, reg, todo)
    finally:
        if con is not None:
            con.close()
        await pg.close()


def _label(reg: v2.Registration) -> str:
    return f"{reg.workflow_id} {reg.version}"


async def cmd_status(a) -> None:
    async def run(pg, _lake, reg, todo):
        ingested = len(await watermark.ingested_keys(pg, reg.workflow_id, reg.version))
        print(f"{_label(reg)}: ingested {ingested}, backlog {len(todo)}")
    await _each(a, run)


async def cmd_ingest(a) -> None:
    async def run(pg, lake_con, reg, todo):
        todo = todo[: a.limit] if a.limit is not None else todo
        if not todo:
            print(f"{_label(reg)}: nothing to ingest")
            return
        summary = await engine.process(pg, lake_con(), reg, todo, include_deferred=a.include_deferred)
        print(f"{_label(reg)}: {json.dumps(summary)}")
    await _each(a, run)


async def cmd_tick(a) -> None:
    async def run(pg, lake_con, reg, todo):
        oldest_h = _age_hours(todo[0]) if todo else None
        if not (len(todo) >= a.threshold or (todo and oldest_h and oldest_h >= a.max_age_hours)):
            age = f"{oldest_h:.1f}h" if oldest_h else "-"
            print(f"{_label(reg)}: backlog {len(todo)} < {a.threshold} "
                  f"(oldest {age} < {a.max_age_hours}h) — skipping")
            return
        summary = await engine.process(pg, lake_con(), reg, todo[: a.batch],
                                       include_deferred=a.include_deferred)
        print(f"{_label(reg)}: {json.dumps(summary)}")
    await _each(a, run)


def cmd_parse(a) -> None:
    """Dry-run: fetch + parse one sample, print per-table row counts. No DB, no lake."""
    if not (a.workflow and a.version):
        raise SystemExit("parse needs --workflow and --version")
    specs = SPECS.get((a.workflow, a.version))
    if specs is None:
        raise SystemExit(f"no spec for {a.workflow} {a.version}")
    prefix = source.locate(a.workflow, a.version, a.sample)
    if prefix is None:
        raise SystemExit(f"{a.sample}: no MARK_COMPLETE under any source base")
    counts: dict[str, int] = defaultdict(int)
    for spec in specs:
        if spec.defer and not a.include_deferred:
            continue
        for p in ([prefix] if not spec.branched else [f"{prefix}/{b}" for b in BRANCHES]):
            data = source.fetch(p, spec.subpath)
            if data:
                counts[spec.table] += sum(1 for _ in spec.parser(data))
    print(json.dumps({"prefix": prefix, **counts}))


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
    p.add_argument("--workflow", default=None, help="registration workflow_id (default: all with a spec)")
    p.add_argument("--version", default=None, help="registration version (default: all)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    ing = sub.add_parser("ingest")
    ing.add_argument("--limit", type=int, default=None)
    ing.add_argument("--include-deferred", action="store_true",
                     help="also ingest deferred tables (markers, HUMAnN gene families)")
    tick = sub.add_parser("tick")
    tick.add_argument("--threshold", type=int, default=500)
    tick.add_argument("--max-age-hours", type=float, default=24.0)
    tick.add_argument("--batch", type=int, default=1000)
    tick.add_argument("--include-deferred", action="store_true",
                      help="also ingest deferred tables (markers, HUMAnN gene families)")
    pr = sub.add_parser("parse")
    pr.add_argument("--sample", required=True)
    pr.add_argument("--include-deferred", action="store_true",
                    help="also count deferred tables (markers, HUMAnN gene families)")
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
