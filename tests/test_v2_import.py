"""v2 ControlDO export import and verification (#376)."""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry import control_db as cdb
from nextflow_telemetry import v2_import
from nextflow_telemetry.utils import srrs_to_sample_id
from nextflow_telemetry.v2_import import (
    Snapshot,
    TargetNotEmpty,
    deadlines,
    import_snapshot,
    read_snapshot,
    readset_id_for_accession,
    to_pg_rows,
    verify,
)

ZELLER = ["ERR478958", "ERR478959", "ERR478960", "ERR478961", "ERR480454", "ERR480455", "ERR480456", "ERR480457"]
ZELLER_RS = "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"
EXPORT_ID = "20261030T000000000Z"
T = "2026-10-30T10:00:00.000Z"
SETTINGS = {"CLAIM_TTL_MINUTES": 5, "SUBMIT_BACKSTOP_HOURS": 168, "LIVENESS_MINUTES": 10}


def _sample(i: int, runs: list[str], metadata: str | None) -> dict:
    acc = ";".join(sorted(runs))
    return {"id": i, "sample_id": srrs_to_sample_id(runs), "ncbi_accession": acc,
            "readset_id": readset_id_for_accession(acc), "biosample_id": None, "metadata": metadata,
            "created_at": T, "updated_at": T}


def _workflow(i: int, version: str, collections: str | None, params: str, sample_key: str) -> dict:
    return {"id": i, "workflow_id": "cmgd", "version": version, "repository_url": "https://example.org/cmgd",
            "revision": "main", "manifest_version": None, "max_retries": 1, "status": "active",
            "description": None, "created_at": T, "updated_at": T, "params": params,
            "collections": collections, "sample_key": sample_key}


def _run(name: str, status: str, **kw) -> dict:
    row = dict.fromkeys(("run_id", "executor_job_id", "claimed_at", "submitted_at", "started_at", "completed_at",
                         "last_heartbeat_at", "wait_seconds", "wrapper_exit_code", "last_known_slurm_state",
                         "slurm_reason", "nextflow_log_uploaded_at"))
    row.update(run_name=name, workflow_id="cmgd", workflow_version="1", workflow_pk=1, revision="main",
               status=status, claimed_at=T)
    row.update(kw)
    return row


def _job(i: int, sample_id: str, workflow_pk: int, status: str, run_name: str | None, **kw) -> dict:
    row = {"id": i, "sample_id": sample_id, "workflow_pk": workflow_pk, "workflow_id": "cmgd",
           "workflow_version": "1", "status": status, "run_name": run_name, "retry_count": 0, "created_at": T,
           "completed_at": None, "failed_at": None, "failure_reason": None}
    row.update(kw)
    return row


def v2_tables() -> dict[str, list[dict]]:
    s = [_sample(1, ZELLER, '{"study":"z"}'), _sample(2, ["SRR000001"], None), _sample(3, ["SRR000002"], '{"k":"v"}')]
    ids = [x["sample_id"] for x in s]
    jobs = [
        _job(5, ids[0], 1, "pending", None),
        _job(9, ids[1], 1, "claimed", "r-claimed"),
        _job(12, ids[2], 1, "submitted", "r-submitted"),
        _job(20, ids[0], 2, "running", "r-running"),
        _job(31, ids[1], 2, "completed", "r-completed", completed_at=T),
        _job(40, ids[2], 2, "failed", "r-completed", failed_at=T, failure_reason="boom"),
    ]
    return {
        "samples": s,
        "collections": [{"collection_id": c, "source": "manual", "label": None, "created_at": T, "updated_at": T}
                        for c in ("c1", "c2")],
        "collection_samples": [{"collection_id": c, "sample_id": ids[i], "created_at": T}
                               for c, i in (("c1", 0), ("c2", 0), ("c1", 1), ("c2", 1), ("c1", 2))],
        "workflows": [_workflow(1, "1", None, "{}", "sample_id"),
                      _workflow(2, "2", '["c1"]', '{"a":1}', "readset_id")],
        "jobs": jobs,
        "job_counts": [{"workflow_pk": 1, "status": "pending", "n": 2},  # drift: one pending job
                       {"workflow_pk": 1, "status": "claimed", "n": 1},
                       {"workflow_pk": 1, "status": "submitted", "n": 1},
                       {"workflow_pk": 2, "status": "running", "n": 1},
                       {"workflow_pk": 2, "status": "completed", "n": 1},
                       {"workflow_pk": 2, "status": "failed", "n": 1}],
        "runs": [
            _run("r-completed", "completed", submitted_at=T, started_at=T, completed_at=T, last_heartbeat_at=T),
            _run("r-claimed", "claimed"),
            _run("r-submitted", "submitted", submitted_at=T),
            _run("r-running", "running", submitted_at=T, started_at=T, last_heartbeat_at="2026-10-30T10:30:00.000Z"),
        ],
        "dead_letter": [{"id": 3, "job_id": 40, "run_name": "r-completed", "sample_id": ids[2],
                         "workflow_id": "cmgd", "workflow_version": "1", "reason": "boom", "created_at": T,
                         "resolved_at": None}],
        "daemons": [{"agent_id": "login07:cmgd", "hostname": "login07", "workflow_id": "cmgd", "profile": None,
                     "nf_client_version": None, "config_yaml": None, "mode": "slurm", "batch_size": 10,
                     "max_concurrent_runs": None, "active_runs": 0, "status": "idle", "last_seen_at": T,
                     "started_at": T}],
        "run_task_counts": [{"run_name": "r-completed", "status": "COMPLETED", "n": 3}],
        "run_failed_tasks": [{"run_name": "r-completed", "process": "P", "sample_id": ids[2], "exit_code": "1",
                              "task_hash": "ab/cd", "attempt": 1, "error_action": "RETRY", "utc_time": T},
                             {"run_name": "r-completed", "process": "Q", "sample_id": None, "exit_code": None,
                              "task_hash": None, "attempt": None, "error_action": None, "utc_time": T}],
    }


def write_export(root: Path, tables: dict[str, list[dict]], *, cursor: dict | None) -> Path:
    manifest = {"format": 1, "export_id": EXPORT_ID, "exported_at": T, "settings": SETTINGS,
                "sequences": {"jobs": 500, "samples": 10, "workflows": 3, "dead_letter": 7},
                "tables": {}, "events_cursor": cursor}
    for name, rows in tables.items():
        objects = []
        if rows:
            key = f"exports/{EXPORT_ID}/{name}/0000.ndjson.gz"
            (root / name).mkdir()
            (root / name / "0000.ndjson.gz").write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode()))
            objects.append(key)
        manifest["tables"][name] = {"rows": len(rows), "objects": objects}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


CURSOR = {"stream": "telemetry/events", "object_key": "telemetry/events/dt=2026-10-30/100000-00000007.ndjson.gz",
          "line_offset": 12}


@pytest.fixture()
def export_dir(tmp_path: Path) -> Path:
    return write_export(tmp_path, v2_tables(), cursor=CURSOR)


def with_engine(db_url: str, fn):
    async def go():
        engine = create_async_engine(db_url)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()
    return asyncio.run(go())


def import_and_verify(db_url: str, snapshot: Snapshot, *, replace: bool = False) -> dict:
    async def go(engine):
        await import_snapshot(engine, snapshot, replace=replace)
        return await verify(engine, snapshot)
    return with_engine(db_url, go)


def sql(db_url: str, statement: str, **params):
    async def go(engine):
        async with engine.begin() as con:
            return (await con.execute(text(statement), params)).all() if statement.lstrip().startswith("select") \
                else await con.execute(text(statement), params)
    return with_engine(db_url, go)


# --- unit -----------------------------------------------------------------

def test_readset_id_for_accession_matches_adr_0007_vectors():
    assert readset_id_for_accession("SRR000001") == "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"
    assert readset_id_for_accession(";".join(ZELLER)) == ZELLER_RS
    assert readset_id_for_accession(";".join(ZELLER) + ";") == ZELLER_RS


@pytest.mark.parametrize("value", ["", None, "GSM123", "SRR000001;GSM123"])
def test_readset_id_for_accession_none_for_non_runs(value):
    assert readset_id_for_accession(value) is None


def test_to_pg_rows_converts_types(export_dir):
    rows = to_pg_rows(read_snapshot(export_dir))
    assert rows["runs"][0]["claimed_at"] == datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat("2026-10-30T23:37:55.294Z").microsecond == 294000
    assert rows["workflows"][0]["collections"] is None
    assert rows["workflows"][1]["collections"] == ["c1"]
    assert rows["workflows"][1]["params"] == {"a": 1}
    assert rows["samples"][1]["metadata"] == {}
    assert len(rows["readset_runs"]) == len(ZELLER) + 2


def test_to_pg_rows_empty_collections_list_becomes_none(export_dir):
    snapshot = read_snapshot(export_dir)
    snapshot.tables["workflows"][1]["collections"] = "[]"
    assert to_pg_rows(snapshot)["workflows"][1]["collections"] is None


def test_to_pg_rows_rejects_unknown_column(export_dir):
    snapshot = read_snapshot(export_dir)
    snapshot.tables["jobs"][0]["surprise"] = 1
    with pytest.raises(ValueError, match="jobs.*surprise"):
        to_pg_rows(snapshot)


def test_to_pg_rows_rejects_run_under_two_readsets(export_dir):
    snapshot = read_snapshot(export_dir)
    snapshot.tables["samples"].append({**snapshot.tables["samples"][1], "id": 4, "sample_id": "other",
                                       "readset_id": "RS.other"})
    with pytest.raises(ValueError, match="SRR000001"):
        to_pg_rows(snapshot)


def test_deadlines_by_run_status(export_dir):
    rows = {r["run_name"]: r for r in to_pg_rows(read_snapshot(export_dir))["runs"]}
    assert {k: v for k, v in deadlines(rows["r-claimed"], SETTINGS).items() if v} == {
        "claim_expires_at": datetime(2026, 10, 30, 10, 5, tzinfo=timezone.utc)}
    assert {k: v for k, v in deadlines(rows["r-submitted"], SETTINGS).items() if v} == {
        "submit_deadline": datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)}
    assert {k: v for k, v in deadlines(rows["r-running"], SETTINGS).items() if v} == {
        "liveness_deadline": datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc)}
    assert not any(deadlines(rows["r-completed"], SETTINGS).values())
    local = {**rows["r-running"], "last_heartbeat_at": None}
    assert {k: v for k, v in deadlines(local, SETTINGS).items() if v} == {
        "submit_deadline": datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)}


def test_read_snapshot_rejects_bad_exports(export_dir):
    manifest = json.loads((export_dir / "manifest.json").read_text())

    def rewrite(m):
        (export_dir / "manifest.json").write_text(json.dumps(m))

    rewrite({**manifest, "format": 2})
    with pytest.raises(ValueError, match="format"):
        read_snapshot(export_dir)

    short = copy.deepcopy(manifest)
    short["tables"]["jobs"]["rows"] = 99
    rewrite(short)
    with pytest.raises(ValueError, match="jobs"):
        read_snapshot(export_dir)

    rewrite(manifest)
    (export_dir / "jobs" / "0000.ndjson.gz").unlink()
    with pytest.raises(ValueError, match="missing"):
        read_snapshot(export_dir)


# --- integration ----------------------------------------------------------

def test_import_is_clean_and_keeps_ids_and_timestamps(db_url, export_dir):
    report = import_and_verify(db_url, read_snapshot(export_dir))
    assert report["clean"], report["failures"]
    assert report["v2_job_counts_drift"] == [{"workflow_pk": 1, "status": "pending", "job_counts": 2, "jobs": 1}]

    async def read(engine):
        async with engine.connect() as con:
            jobs = (await con.execute(select(cdb.jobs_tbl.c.id, cdb.jobs_tbl.c.created_at))).all()
            cursor = (await con.execute(select(cdb.ingest_cursor_tbl))).one()
            n_runs = (await con.execute(text("select count(*) from readset_runs"))).scalar_one()
            running = (await con.execute(select(cdb.runs_tbl.c.liveness_deadline)
                                         .where(cdb.runs_tbl.c.run_name == "r-running"))).scalar_one()
        async with engine.begin() as con:
            await con.execute(cdb.jobs_tbl.delete().where(cdb.jobs_tbl.c.id == 5))
            new_id = (await con.execute(insert(cdb.jobs_tbl).values(
                sample_id=v2_tables()["samples"][0]["sample_id"], workflow_pk=1, workflow_id="cmgd",
                workflow_version="1", priority=1, status="pending").returning(cdb.jobs_tbl.c.id))).scalar_one()
        return jobs, cursor, n_runs, running, new_id

    jobs, cursor, n_runs, running, new_id = with_engine(db_url, read)
    assert sorted(j.id for j in jobs) == [5, 9, 12, 20, 31, 40]
    assert {j.created_at for j in jobs} == {datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)}
    assert (cursor.stream, cursor.object_key, cursor.line_offset) == tuple(CURSOR.values())
    assert n_runs == len(ZELLER) + 2
    assert running == datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc)
    assert new_id > 500


def test_second_import_needs_replace(db_url, export_dir):
    snapshot = read_snapshot(export_dir)
    assert import_and_verify(db_url, snapshot)["clean"]
    with pytest.raises(TargetNotEmpty):
        import_and_verify(db_url, snapshot)
    assert import_and_verify(db_url, snapshot, replace=True)["clean"]


@pytest.mark.parametrize("statement, failure", [
    ("update samples set readset_id = 'RS.wrong' where id = 2", "identity"),
    ("update workflows set params = jsonb_build_object('a', 2) where id = 2", "workflows"),
    ("delete from collection_samples where collection_id = 'c1' and sample_id = (select sample_id from samples where id = 3)",
     "collections"),
    ("update jobs set status = 'submitted' where id = 20", "jobs_by_workflow_status"),
])
def test_verify_reports_tampering(db_url, export_dir, statement, failure):
    snapshot = read_snapshot(export_dir)
    assert import_and_verify(db_url, snapshot)["clean"]
    sql(db_url, statement)
    report = with_engine(db_url, lambda engine: verify(engine, snapshot))
    assert not report["clean"]
    assert any(f.startswith(failure) for f in report["failures"]), report["failures"]


def test_import_and_verify_carry_job_priority(db_url, tmp_path):
    tables = v2_tables()
    for job in tables["jobs"]:
        job["priority"] = -1 if job["id"] == 5 else 0
    snapshot = read_snapshot(write_export(tmp_path, tables, cursor=CURSOR))
    assert import_and_verify(db_url, snapshot)["clean"]
    assert sql(db_url, "select priority from jobs where id = 5")[0][0] == -1
    sql(db_url, "update jobs set priority = 0 where id = 5")
    report = with_engine(db_url, lambda engine: verify(engine, snapshot))
    assert not report["clean"]
    assert len([f for f in report["failures"] if f.startswith("tables.jobs")]) == 1, report["failures"]


def test_missing_events_cursor_imports_but_is_not_clean(db_url, tmp_path):
    snapshot = read_snapshot(write_export(tmp_path, v2_tables(), cursor=None))
    report = import_and_verify(db_url, snapshot)
    assert not report["clean"]
    assert any("no event cursor" in f for f in report["failures"])
    assert sql(db_url, "select count(*) from ingest_cursor")[0][0] == 0


# --- CLI --------------------------------------------------------------------

def test_main_prints_report_and_exits_zero(db_url, export_dir, monkeypatch, capsys):
    monkeypatch.setenv("SQLALCHEMY_URI", db_url)
    with pytest.raises(SystemExit) as exit_info:
        v2_import.main([str(export_dir)])
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


def test_main_requires_database_uri(export_dir, monkeypatch):
    monkeypatch.delenv("SQLALCHEMY_URI", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        v2_import.main([str(export_dir)])
    assert exit_info.value.code == 2
