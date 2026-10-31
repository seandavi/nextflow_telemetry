"""Replay check for the v2 -> Postgres cutover (ADR-0013, #375). Deleted with ControlDO in M10.

Imports export BEFORE into a scratch database, ingests the R2 events up to AFTER's
cursor, and compares the resulting run and job states with AFTER (what v2 held).
Timestamps are compared only as set or unset: v2 stamps them when ControlDO is called,
the ingester at SinkDO's ``received_at`` a few milliseconds later.

    python -m nextflow_telemetry.v2_replay BEFORE_DIR AFTER_DIR EVENTS_DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obstore.store import ObjectStore
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from . import control_db as cdb
from .services import event_ingest
from .services.blobs import blob_store
from .services.deadlines import BACKSTOP_REASON, CLAIM_EXPIRED_REASON, LIVENESS_REASON
from .services.transitions import RUN_TERMINAL
from .v2_import import Snapshot, import_snapshot, read_snapshot, to_pg_rows

PRODUCTION_DATABASE = "nf_telemetry"
DEADLINE_REASONS = (CLAIM_EXPIRED_REASON, BACKSTOP_REASON, LIVENESS_REASON)
RUN_FIELDS = ("status", "run_id", "executor_job_id", "wait_seconds", "wrapper_exit_code",
              "last_known_slurm_state", "slurm_reason")
RUN_STAMPS = ("submitted_at", "started_at", "completed_at", "nextflow_log_uploaded_at")  # set vs unset only
JOB_FIELDS = ("status", "run_name", "retry_count", "failure_reason")
EXAMPLES = 20


def compare(before: dict[str, list[dict]], after: dict[str, list[dict]],
            replay_runs: dict[str, dict], replay_jobs: dict[int, dict]) -> dict[str, Any]:
    """Classify every BEFORE run and assigned job against AFTER; see the module docstring."""
    after_runs = {r["run_name"]: r for r in after["runs"]}
    after_jobs = {j["id"]: j for j in after["jobs"]}
    before_runs = {r["run_name"] for r in before["runs"]}
    differences = {"deadline": 0, "reclaimed": 0, "archived": 0}
    mismatches = 0
    examples: list[dict[str, Any]] = []

    def mismatch(table: str, key: Any, field: str, replay: Any, v2: Any) -> None:
        nonlocal mismatches
        mismatches += 1
        if len(examples) < EXAMPLES:
            examples.append({"table": table, "key": key, "field": field, "replay": replay, "v2": v2})

    deadline_runs: set[str] = set()
    for name in sorted(before_runs):
        replayed, v2 = replay_runs.get(name), after_runs.get(name)
        if v2 is None or replayed is None:
            mismatch("runs", name, "<row>", None if replayed is None else "present",
                     None if v2 is None else "present")
        elif (v2["status"] in RUN_TERMINAL and v2["slurm_reason"] in DEADLINE_REASONS
                and replayed["status"] not in RUN_TERMINAL):
            differences["deadline"] += 1  # the replay doesn't run the sweep
            deadline_runs.add(name)
        else:
            for f in RUN_FIELDS:
                if replayed[f] != v2[f]:
                    mismatch("runs", name, f, replayed[f], v2[f])
            for f in RUN_STAMPS:
                if (replayed[f] is None) != (v2[f] is None):
                    mismatch("runs", name, f, replayed[f] is not None, v2[f] is not None)

    jobs_compared = 0
    for job in sorted((j for j in before["jobs"] if j["run_name"] is not None), key=lambda j: j["id"]):
        jobs_compared += 1
        jid = job["id"]
        replayed, v2 = replay_jobs.get(jid), after_jobs.get(jid)
        if v2 is None:
            differences["archived"] += 1  # retired-version archive
        elif job["run_name"] in deadline_runs:
            differences["deadline"] += 1
        elif replayed is None:
            mismatch("jobs", jid, "<row>", None, "present")
        elif v2["run_name"] is not None and v2["run_name"] not in before_runs:
            # Retried and re-claimed by a run minted in the window; claims aren't events.
            if (replayed["status"] == "pending" and replayed["run_name"] is None
                    and replayed["retry_count"] == v2["retry_count"]):
                differences["reclaimed"] += 1
            else:
                mismatch("jobs", jid, "<reclaimed>", replayed["status"], v2["status"])
        else:
            for f in JOB_FIELDS:
                if replayed[f] != v2[f]:
                    mismatch("jobs", jid, f, replayed[f], v2[f])

    return {"runs_compared": len(before_runs), "jobs_compared": jobs_compared,
            "differences": differences, "mismatches": mismatches, "examples": examples}


async def replay(engine: AsyncEngine, before: Snapshot, after: Snapshot, events: ObjectStore) -> dict[str, Any]:
    cursor = after.manifest["events_cursor"]
    if cursor is None:
        raise ValueError("export AFTER has no events cursor")
    await import_snapshot(engine, before, replace=True)
    result = await event_ingest.ingest_once(
        engine, events, now=datetime.now(timezone.utc), until=(cursor["object_key"], cursor["line_offset"]))
    if result.skipped:
        raise RuntimeError("another ingester holds the cursor of the scratch database")
    async with engine.connect() as con:
        replay_runs = {r.run_name: dict(r._mapping) for r in await con.execute(select(cdb.runs_tbl))}
        replay_jobs = {r.id: dict(r._mapping) for r in await con.execute(select(cdb.jobs_tbl))}
    report = compare(to_pg_rows(before), to_pg_rows(after), replay_runs, replay_jobs)
    report["lines_applied"] = result.lines
    report["lines_rejected"] = result.rejected
    report["clean"] = report["mismatches"] == 0 and result.rejected == 0
    return report


async def _run(before_dir: Path, after_dir: Path, events_dir: Path, uri: str) -> dict[str, Any]:
    before, after = read_snapshot(before_dir), read_snapshot(after_dir)
    events = blob_store(str(events_dir), r2_account_id="", r2_access_key_id="", r2_secret_access_key="")
    engine = create_async_engine(uri)
    try:
        return await replay(engine, before, after, events)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="v2_replay", description=__doc__)
    for name in ("before_dir", "after_dir", "events_dir"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args(argv)

    uri = os.environ.get("SQLALCHEMY_URI", "")
    if not uri.startswith(("postgresql://", "postgresql+asyncpg://")):
        print("v2 replay: set SQLALCHEMY_URI to a scratch PostgreSQL database", file=sys.stderr)
        raise SystemExit(2)
    uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    if make_url(uri).database == PRODUCTION_DATABASE:
        print(f"v2 replay: refusing to replace the control plane of {PRODUCTION_DATABASE}", file=sys.stderr)
        raise SystemExit(2)
    try:
        report = asyncio.run(_run(args.before_dir, args.after_dir, args.events_dir, uri))
    except (ValueError, OSError, RuntimeError) as e:
        print(f"v2 replay: {e}", file=sys.stderr)
        raise SystemExit(2)
    except SQLAlchemyError:
        print("v2 replay: database operation failed; check connectivity and migrations", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["clean"] else 1)


if __name__ == "__main__":
    main()
