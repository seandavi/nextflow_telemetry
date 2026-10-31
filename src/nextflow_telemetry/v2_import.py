"""Load a v2 ControlDO export (POST /admin/export) into the control-plane schema
and verify it (ADR-0013 §8, #376). For scratch and staging databases and the M8
cutover; deleted with ControlDO in M10."""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, func, insert, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from . import control_db as cdb
from .db import metadata
from .meta.store import readset_id
from .utils import parse_srrs, srrs_to_sample_id

EXPORT_FORMAT = 1
EVENTS_STREAM = "telemetry/events"
# Insert order (FK parents first). job_counts is read only for the drift note.
IMPORT_TABLES = ("samples", "collections", "collection_samples", "workflows", "runs", "jobs",
                 "dead_letter", "daemons", "run_task_counts", "run_failed_tasks")
SERIAL_TABLES = ("samples", "workflows", "jobs", "dead_letter", "run_failed_tasks")
INSERT_CHUNK = 5_000
EXAMPLES = 20
ROW_EXAMPLES = 5
_RUN = re.compile(r"^[SED]RR\d+$")
_DEADLINES = ("claim_expires_at", "submit_deadline", "liveness_deadline")


class TargetNotEmpty(RuntimeError):
    """The target already holds control-plane rows and --replace was not given."""


@dataclass(frozen=True)
class Snapshot:
    manifest: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]  # raw v2 rows, as exported; every EXPORT table


def read_snapshot(directory: Path) -> Snapshot:
    """Read manifest.json and every object it lists.

    Raises ValueError if any of these hold: format != EXPORT_FORMAT; a table in
    IMPORT_TABLES or job_counts is missing; a listed object is missing; a table's
    line count differs from its manifest `rows`."""
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != EXPORT_FORMAT:
        raise ValueError(f"unsupported export format {manifest.get('format')!r}, expected {EXPORT_FORMAT}")
    prefix = f"exports/{manifest['export_id']}/"
    tables: dict[str, list[dict[str, Any]]] = {}
    for name, entry in manifest["tables"].items():
        rows: list[dict[str, Any]] = []
        for key in entry["objects"]:
            path = directory / key.removeprefix(prefix)
            if not path.is_file():
                raise ValueError(f"export object missing: {path}")
            with gzip.open(path, "rt") as f:
                rows.extend(json.loads(line) for line in f if line.strip())
        if len(rows) != entry["rows"]:
            raise ValueError(f"table {name}: {len(rows)} rows read, manifest says {entry['rows']}")
        tables[name] = rows
    for name in (*IMPORT_TABLES, "job_counts"):
        if name not in tables:
            raise ValueError(f"export has no table {name}")
    return Snapshot(manifest, tables)


def readset_id_for_accession(ncbi_accession: str | None) -> str | None:
    """v2's rule (cf/src/readset.ts readsetIdForRuns): split on ';', drop empties.
    None if no runs or any entry is not an INSDC run accession; else readset_id(runs)."""
    runs = [r for r in (ncbi_accession or "").split(";") if r]
    if not runs or not all(_RUN.match(r) for r in runs):
        return None
    return readset_id(runs)


def deadlines(run: dict[str, Any], settings: dict[str, float]) -> dict[str, datetime | None]:
    """claim_expires_at / submit_deadline / liveness_deadline for one converted run row
    (see Deadline derivation). At most one is set."""
    out: dict[str, datetime | None] = dict.fromkeys(_DEADLINES)
    status = run["status"]

    def after(column: str, delta: timedelta) -> datetime:
        base = run[column]
        if base is None:
            raise ValueError(f"run {run['run_name']}: {status} run has no {column}")
        return base + delta

    if status == "claimed":
        out["claim_expires_at"] = after("claimed_at", timedelta(minutes=settings["CLAIM_TTL_MINUTES"]))
    elif status in ("submitted", "running"):
        # Heartbeat decides, not status: only /dispatch/submitted arms the backstop and only
        # wrapper_started/heartbeat arm liveness (a started weblog event arms nothing).
        if run["last_heartbeat_at"] is not None:
            out["liveness_deadline"] = after("last_heartbeat_at", timedelta(minutes=settings["LIVENESS_MINUTES"]))
        elif run["submitted_at"] is not None:
            out["submit_deadline"] = after("submitted_at", timedelta(hours=settings["SUBMIT_BACKSTOP_HOURS"]))
    return out


def _convert(table: str, row: dict[str, Any]) -> dict[str, Any]:
    columns = metadata.tables[table].c
    out: dict[str, Any] = {}
    for name, value in row.items():
        if name not in columns:
            raise ValueError(f"table {table}: v2 column {name!r} has no Postgres column")
        kind = columns[name].type
        if value is None:
            out[name] = {} if isinstance(kind, JSONB) and table == "samples" else None
        elif isinstance(kind, DateTime):
            out[name] = datetime.fromisoformat(value)
        elif isinstance(kind, JSONB):
            out[name] = json.loads(value)
        elif isinstance(kind, ARRAY):
            out[name] = json.loads(value) or None
        else:
            out[name] = value
    return out


def to_pg_rows(snapshot: Snapshot) -> dict[str, list[dict[str, Any]]]:
    """Every IMPORT_TABLES table plus "readset_runs", as rows for the control_db Tables.

    Conversion is driven by the Postgres column type in metadata.tables[t]:
    - DateTime: datetime.fromisoformat (UTC-aware); None stays None.
    - JSONB: json.loads of the text. samples.metadata None becomes {}.
    - ARRAY: json.loads of the text. None and [] both become None (ck_workflows_collections).
    - Anything else is passed through.

    A v2 column with no Postgres column raises ValueError naming table and column.
    runs rows get the three deadline columns (deadlines()). readset_runs is one
    row per run accession of every sample whose readset_id is not None:
    {run_accession, readset_id}. A run under two different readsets raises ValueError
    naming the run and both readsets; a run repeated under the same readset is kept once."""
    rows = {t: [_convert(t, r) for r in snapshot.tables[t]] for t in IMPORT_TABLES}
    settings = snapshot.manifest["settings"]
    for run in rows["runs"]:
        run.update(deadlines(run, settings))

    owner: dict[str, str] = {}
    for sample in rows["samples"]:
        if sample.get("readset_id") is None:
            continue
        for run_accession in parse_srrs(sample["ncbi_accession"]):
            if owner.setdefault(run_accession, sample["readset_id"]) != sample["readset_id"]:
                raise ValueError(f"run {run_accession} is under readsets {owner[run_accession]} "
                                 f"and {sample['readset_id']}")
    rows["readset_runs"] = [{"run_accession": r, "readset_id": rs} for r, rs in owner.items()]
    return rows


def _table(name: str):
    return metadata.tables[name]


async def import_snapshot(engine: AsyncEngine, snapshot: Snapshot, *, replace: bool) -> None:
    """One transaction (engine.begin()):
    1. If replace: TRUNCATE (one statement, no CASCADE) IMPORT_TABLES + readset_runs, then
       DELETE FROM ingest_cursor WHERE stream = EVENTS_STREAM. Otherwise, if any of
       those tables has a row, or the cursor row exists, raise TargetNotEmpty.
    2. Insert to_pg_rows() in IMPORT_TABLES order, then readset_runs, each via
       con.execute(insert(table), chunk) in INSERT_CHUNK chunks.
    3. If manifest events_cursor is not None: insert ingest_cursor(stream, object_key,
       line_offset). updated_at uses its server default.
    4. Reset each SERIAL_TABLES sequence (SQL below)."""
    rows = to_pg_rows(snapshot)
    order = (*IMPORT_TABLES, "readset_runs")
    cursor = snapshot.manifest["events_cursor"]
    async with engine.begin() as con:
        if replace:
            await con.execute(text("truncate " + ", ".join(order)))
            await con.execute(cdb.ingest_cursor_tbl.delete().where(cdb.ingest_cursor_tbl.c.stream == EVENTS_STREAM))
        else:
            for name in order:
                if (await con.execute(select(_table(name)).limit(1))).first() is not None:
                    raise TargetNotEmpty(f"table {name} already has rows; use --replace on a scratch database")
            found = await con.execute(select(cdb.ingest_cursor_tbl).where(cdb.ingest_cursor_tbl.c.stream == EVENTS_STREAM))
            if found.first() is not None:
                raise TargetNotEmpty("ingest_cursor already has a row; use --replace on a scratch database")
        for name in order:
            for i in range(0, len(rows[name]), INSERT_CHUNK):
                await con.execute(insert(_table(name)), rows[name][i:i + INSERT_CHUNK])
        if cursor is not None:
            await con.execute(insert(cdb.ingest_cursor_tbl).values(
                stream=cursor["stream"], object_key=cursor["object_key"], line_offset=cursor["line_offset"]))
        for name in SERIAL_TABLES:  # name comes from the constant above
            await con.execute(text(
                f"select setval(pg_get_serial_sequence(cast(:t as text), 'id'),"
                f" greatest(cast(:hw as bigint), (select coalesce(max(id), 0) from {name}), 1),"
                f" greatest(cast(:hw as bigint), (select coalesce(max(id), 0) from {name})) > 0)"),
                {"t": name, "hw": snapshot.manifest["sequences"].get(name, 0)})


def _dump(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def _counter_diff(a: Counter, b: Counter) -> tuple[int, list[str]]:
    diff = (a - b) + (b - a)
    return sum(diff.values()), list(diff)[:ROW_EXAMPLES]


async def verify(engine: AsyncEngine, snapshot: Snapshot) -> dict[str, Any]:
    """The verification report (below). Read-only."""
    rows = to_pg_rows(snapshot)
    manifest = snapshot.manifest
    failures: list[str] = []
    report: dict[str, Any] = {
        "export_id": manifest["export_id"], "exported_at": manifest["exported_at"],
    }
    async with engine.connect() as con:
        async def fetch(query) -> list[dict[str, Any]]:
            return [dict(r._mapping) for r in await con.execute(query)]

        # tables: full-row comparison on the v2 columns only
        report["tables"] = {}
        for name in IMPORT_TABLES:
            v2_columns = sorted({c for r in rows[name] for c in r} - set(_DEADLINES if name == "runs" else ()))
            expected = Counter(_dump({c: r[c] for c in v2_columns}) for r in rows[name])
            table = _table(name)
            actual_rows = await fetch(select(*(table.c[c] for c in v2_columns))) if v2_columns else []
            mismatches, examples = _counter_diff(expected, Counter(_dump(r) for r in actual_rows))
            count = (await con.execute(select(func.count()).select_from(table))).scalar_one()
            report["tables"][name] = {"snapshot": len(rows[name]), "postgres": count,
                                      "row_mismatches": mismatches, "examples": examples}
            if count != len(rows[name]):
                failures.append(f"tables.{name}: {len(rows[name])} rows in snapshot, {count} in Postgres")
            if mismatches:
                failures.append(f"tables.{name}: {mismatches} row mismatches")

        # readset_runs recomputed from the stored samples
        stored = await fetch(select(cdb.samples_tbl.c.sample_id, cdb.samples_tbl.c.readset_id,
                                    cdb.samples_tbl.c.ncbi_accession))
        want = {(r, readset_id_for_accession(s["ncbi_accession"]))
                for s in stored if readset_id_for_accession(s["ncbi_accession"]) is not None
                for r in parse_srrs(s["ncbi_accession"])}
        have = {(r["run_accession"], r["readset_id"])
                for r in await fetch(select(cdb.readset_runs_tbl.c.run_accession, cdb.readset_runs_tbl.c.readset_id))}
        diff = sorted(want ^ have)
        report["readset_runs"] = {"expected": len(want), "postgres": len(have), "mismatches": len(diff),
                                  "examples": [{"run_accession": r, "readset_id": rs} for r, rs in diff[:EXAMPLES]]}
        if diff:
            failures.append(f"readset_runs: {len(diff)} mismatches")

        # collection member counts
        members = Counter(r["collection_id"] for r in snapshot.tables["collection_samples"])
        in_pg = {r["collection_id"]: r["n"] for r in await fetch(
            select(cdb.collection_samples_tbl.c.collection_id, func.count().label("n"))
            .group_by(cdb.collection_samples_tbl.c.collection_id))}
        bad = [{"collection_id": c["collection_id"], "snapshot": members[c["collection_id"]],
                "postgres": in_pg.get(c["collection_id"], 0)}
               for c in snapshot.tables["collections"] if members[c["collection_id"]] != in_pg.get(c["collection_id"], 0)]
        report["collections"] = {"checked": len(snapshot.tables["collections"]), "mismatches": len(bad),
                                 "examples": bad[:EXAMPLES]}
        if bad:
            failures.append(f"collections: {len(bad)} member count mismatches")

        # jobs by workflow/status and runs by status
        jobs_snapshot = Counter(f"{j['workflow_pk']}/{j['status']}" for j in snapshot.tables["jobs"])
        jobs_pg = Counter({f"{r['workflow_pk']}/{r['status']}": r["n"] for r in await fetch(
            select(cdb.jobs_tbl.c.workflow_pk, cdb.jobs_tbl.c.status, func.count().label("n"))
            .group_by(cdb.jobs_tbl.c.workflow_pk, cdb.jobs_tbl.c.status))})
        runs_snapshot = Counter(r["status"] for r in snapshot.tables["runs"])
        runs_pg = Counter({r["status"]: r["n"] for r in await fetch(
            select(cdb.runs_tbl.c.status, func.count().label("n")).group_by(cdb.runs_tbl.c.status))})
        for section, label, a, b in (("jobs_by_workflow_status", "jobs by workflow/status", jobs_snapshot, jobs_pg),
                                     ("runs_by_status", "runs by status", runs_snapshot, runs_pg)):
            n = sum(1 for k in a.keys() | b.keys() if a[k] != b[k])
            report[section] = {"snapshot": dict(a), "postgres": dict(b), "mismatches": n}
            if n:
                failures.append(f"{section}: {n} {label} mismatches")

        # identity recomputed from ncbi_accession
        readset_bad = [s["sample_id"] for s in stored
                       if readset_id_for_accession(s["ncbi_accession"]) != s["readset_id"]]
        sample_bad = [s["sample_id"] for s in stored if s["ncbi_accession"] is not None
                      and srrs_to_sample_id(parse_srrs(s["ncbi_accession"])) != s["sample_id"]]
        report["identity"] = {
            "samples_checked": len(stored), "readset_id_mismatches": len(readset_bad),
            "sample_id_mismatches": len(sample_bad),
            "examples": [{"sample_id": s, "field": "readset_id"} for s in readset_bad[:EXAMPLES]]
                        + [{"sample_id": s, "field": "sample_id"} for s in sample_bad[:EXAMPLES]],
        }
        if readset_bad or sample_bad:
            failures.append(f"identity: {len(readset_bad)} readset_id and {len(sample_bad)} sample_id mismatches")

        # workflow scope and params
        in_pg_wf = {r["id"]: r for r in await fetch(select(cdb.workflows_tbl))}
        converted = {w["id"]: w for w in rows["workflows"]}
        bad_wf = [{"id": pk, "workflow_id": w["workflow_id"], "version": w["version"], "field": field}
                  for pk, w in converted.items() for field in ("params", "collections", "sample_key")
                  if pk not in in_pg_wf or in_pg_wf[pk][field] != w[field]]
        report["workflows"] = {"checked": len(converted), "mismatches": len(bad_wf), "examples": bad_wf[:EXAMPLES]}
        if bad_wf:
            failures.append(f"workflows: {len(bad_wf)} scope/params mismatches")

        # event cursor
        row = (await fetch(select(cdb.ingest_cursor_tbl).where(cdb.ingest_cursor_tbl.c.stream == EVENTS_STREAM)))
        pg_cursor = {k: row[0][k] for k in ("stream", "object_key", "line_offset")} if row else None
        report["events_cursor"] = {"manifest": manifest["events_cursor"], "postgres": pg_cursor}
        if manifest["events_cursor"] is None:
            failures.append("events_cursor: no event cursor: the sink had never flushed")
        elif manifest["events_cursor"] != pg_cursor:
            failures.append("events_cursor: differs from the ingest_cursor row")

    # informational: v2's own counter table against its own jobs
    counts = {(c["workflow_pk"], c["status"]): c["n"] for c in snapshot.tables["job_counts"]}
    jobs_by = Counter((j["workflow_pk"], j["status"]) for j in snapshot.tables["jobs"])
    report["v2_job_counts_drift"] = [
        {"workflow_pk": k[0], "status": k[1], "job_counts": counts.get(k, 0), "jobs": jobs_by[k]}
        for k in sorted(counts.keys() | jobs_by.keys()) if counts.get(k, 0) != jobs_by[k]]

    report["failures"] = failures
    report["clean"] = not failures
    return report


async def _run(snapshot_dir: Path, uri: str, replace: bool) -> dict[str, Any]:
    snapshot = read_snapshot(snapshot_dir)
    engine = create_async_engine(uri)
    try:
        await import_snapshot(engine, snapshot, replace=replace)
        return await verify(engine, snapshot)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    """CLI: python -m nextflow_telemetry.v2_import SNAPSHOT_DIR [--replace]."""
    parser = argparse.ArgumentParser(prog="v2_import", description=__doc__)
    parser.add_argument("snapshot_dir", type=Path)
    parser.add_argument("--replace", action="store_true",
                        help="truncate the control-plane tables first; scratch and staging databases only")
    args = parser.parse_args(argv)

    uri = os.environ.get("SQLALCHEMY_URI", "")
    if not uri.startswith(("postgresql://", "postgresql+asyncpg://")):
        print("v2 import: set SQLALCHEMY_URI to the target PostgreSQL URI", file=sys.stderr)
        raise SystemExit(2)
    try:
        report = asyncio.run(_run(args.snapshot_dir, uri.replace("postgresql://", "postgresql+asyncpg://", 1),
                                  args.replace))
    except (ValueError, OSError, TargetNotEmpty) as e:
        print(f"v2 import: {e}", file=sys.stderr)
        raise SystemExit(2)
    except SQLAlchemyError:
        print("v2 import: database operation failed; check connectivity and migrations", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["clean"] else 1)


if __name__ == "__main__":
    main()
