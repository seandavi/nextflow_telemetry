"""nf-etl — the ETL command line. Plain scripts, no orchestrator.

Registrations and their completed jobs come from the v2 API (``NF_TELEMETRY_URL``);
every registration with an OutputSpec is processed, narrowed by ``--workflow`` /
``--version``. Writes go to ``lake.cmgd.*`` through cdsci.lake (backend from its
``CU_OPENALEX_*`` settings; docs/etl-runbook.md).

  nf-etl status                                     per-registration ingested + backlog
  nf-etl --workflow W --version V parse --sample K  dry-run: per-table row counts (no lake)
  nf-etl ingest  [--limit N] [--batch-size 500]     ingest pending completed samples
  nf-etl tick    [--threshold 500]                  ingest iff backlog >= threshold or age fallback
  nf-etl volumes                                    measured sizes + extrapolations (markdown)
  nf-etl publish --registration W/V [--out DIR]     build a public release into the local store
  nf-etl publish --registration W/V --sync          upload the built dataset to r2:cmgd-public
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from cdsci.lake import ops  # type: ignore[import-untyped]

from . import engine, lake, publish, source, v2
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


def _label(reg: v2.Registration) -> str:
    return f"{reg.workflow_id} {reg.version}"


def _each(a, run) -> None:
    """Open the lake once, call ``run(con, reg, ingested, todo)`` per registration."""
    con = lake.connect()
    try:
        for reg in _registrations(a):
            ingested = lake.ingested_keys(con, reg.workflow_id, reg.version)
            run(con, reg, ingested, _pending(reg, ingested))
    finally:
        con.close()


def cmd_status(a) -> None:
    def run(con, reg, ingested, todo):
        wm = ops.get_watermark(con, lake.SOURCE.name, f"{reg.workflow_id}/{reg.version}")
        print(f"{_label(reg)}: ingested {len(ingested)}, backlog {len(todo)}, watermark {json.dumps(wm)}")
    _each(a, run)


def cmd_ingest(a) -> None:
    def run(con, reg, _ingested, todo):
        todo = todo[: a.limit] if a.limit is not None else todo
        if not todo:
            print(f"{_label(reg)}: nothing to ingest")
            return
        print(f"{_label(reg)}: {json.dumps(engine.process(con, reg, todo, a.batch_size))}")
    _each(a, run)


def cmd_tick(a) -> None:
    def run(con, reg, _ingested, todo):
        oldest_h = _age_hours(todo[0]) if todo else None
        if not (len(todo) >= a.threshold or (todo and oldest_h and oldest_h >= a.max_age_hours)):
            age = f"{oldest_h:.1f}h" if oldest_h else "-"
            print(f"{_label(reg)}: backlog {len(todo)} < {a.threshold} "
                  f"(oldest {age} < {a.max_age_hours}h) — skipping")
            return
        summary = engine.process(con, reg, todo[: a.limit], a.batch_size)
        print(f"{_label(reg)}: {json.dumps(summary)}")
    _each(a, run)


def cmd_parse(a) -> None:
    """Dry-run: fetch + parse one sample, print per-table row counts. No lake."""
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
        for p in ([prefix] if not spec.branched else [f"{prefix}/{b}" for b in BRANCHES]):
            data = source.fetch(p, spec.subpath)
            if data:
                counts[spec.table] += 1 if spec.indexed else sum(1 for _ in spec.parser(data))
    print(json.dumps({"prefix": prefix, **counts}))


def cmd_volumes(a) -> None:
    con = lake.connect()
    try:
        print(lake.volumes(con))
    finally:
        con.close()


def cmd_publish(a) -> None:
    """Build one public release of a registration (ADR-0011), or with --sync upload
    the already-built local dataset. Building never touches the bucket."""
    workflow_id, version = publish.parse_registration(a.registration)
    if a.sync:
        publish.sync(a.out, publish.dataset_id(workflow_id, version), a.remote, dry_run=a.dry_run)
        return
    from cdsci.lake import lake_connect  # type: ignore[import-untyped]

    con = lake_connect(read_only=True)
    try:
        m = publish.publish(con, workflow_id, version, a.out)
    finally:
        con.close()
    rows = {t.name: t.row_count for t in m.tables}
    print(f"{m.dataset} {m.release} -> {a.out}/{m.dataset}/{m.release}: {json.dumps(rows)}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nf-etl", description="cMD output-catalog ETL")
    p.add_argument("--workflow", default=None, help="registration workflow_id (default: all with a spec)")
    p.add_argument("--version", default=None, help="registration version (default: all)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    ing = sub.add_parser("ingest")
    ing.add_argument("--limit", type=int, default=None, help="max samples per registration")
    tick = sub.add_parser("tick")
    tick.add_argument("--threshold", type=int, default=500)
    tick.add_argument("--max-age-hours", type=float, default=24.0)
    tick.add_argument("--limit", type=int, default=1000, help="max samples per registration")
    for sp in (ing, tick):
        sp.add_argument("--batch-size", type=int, default=engine.BATCH_SIZE,
                        help="samples per lake write (one ops run, one snapshot)")
    pr = sub.add_parser("parse")
    pr.add_argument("--sample", required=True)
    sub.add_parser("volumes")
    pub = sub.add_parser("publish", help="public release of one registration (docs/data-access.md)")
    pub.add_argument("--registration", required=True, help="<workflow_id>/<version>")
    pub.add_argument("--out", default=publish.PUBLISH_ROOT, help="local release store")
    pub.add_argument("--sync", action="store_true",
                     help="upload the already-built local dataset to --remote (no build)")
    pub.add_argument("--remote", default=publish.SYNC_REMOTE)
    pub.add_argument("--dry-run", action="store_true", help="with --sync: print the rclone commands")

    a = p.parse_args(argv)
    {"status": cmd_status, "ingest": cmd_ingest, "tick": cmd_tick, "parse": cmd_parse,
     "volumes": cmd_volumes, "publish": cmd_publish}[a.cmd](a)


if __name__ == "__main__":
    main()
