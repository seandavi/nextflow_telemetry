"""v2_replay (#375): compare() classification, the end-to-end replay on fixture exports, and the CLI guards."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest

from nextflow_telemetry import v2_replay
from nextflow_telemetry.services.deadlines import LIVENESS_REASON
from nextflow_telemetry.v2_import import Snapshot, read_snapshot, to_pg_rows
from nextflow_telemetry.v2_replay import compare
from test_v2_import import CURSOR, ZELLER_RS, v2_tables, with_engine, write_export

EVENTS_KEY = CURSOR["object_key"]
T11 = "2026-10-30T11:00:00.000Z"


def rows(tmp_path: Path, tables: dict[str, list[dict]]) -> dict[str, list[dict]]:
    root = tmp_path / "x"
    root.mkdir()
    snapshot = read_snapshot(write_export(root, tables, cursor=CURSOR))
    return to_pg_rows(snapshot)


def keyed(pg: dict[str, list[dict]]) -> tuple[dict, dict]:
    return ({r["run_name"]: copy.deepcopy(r) for r in pg["runs"]}, {j["id"]: copy.deepcopy(j) for j in pg["jobs"]})


def test_compare_matches(tmp_path: Path) -> None:
    pg = rows(tmp_path, v2_tables())
    report = compare(pg, pg, *keyed(pg))
    assert report == {"runs_compared": 4, "jobs_compared": 5, "differences": {"deadline": 0, "reclaimed": 0, "archived": 0},
                      "mismatches": 0, "examples": []}


def test_compare_classifies_documented_differences(tmp_path: Path) -> None:
    before = rows(tmp_path, v2_tables())
    after = copy.deepcopy(before)
    # v2's alarm failed r-running and its job went back to pending; the replay has no sweep.
    next(r for r in after["runs"] if r["run_name"] == "r-running").update(status="failed", slurm_reason=LIVENESS_REASON)
    next(j for j in after["jobs"] if j["id"] == 20).update(status="pending", run_name=None, retry_count=1)
    # job 9 was re-claimed by a run minted in the window; the replay sent it back to pending.
    next(j for j in after["jobs"] if j["id"] == 9).update(run_name="r-new", retry_count=1)
    # job 12's version was retired and archived.
    after["jobs"] = [j for j in after["jobs"] if j["id"] != 12]
    replay_runs, replay_jobs = keyed(before)
    replay_jobs[9].update(status="pending", run_name=None, retry_count=1)

    report = compare(before, after, replay_runs, replay_jobs)

    assert report["differences"] == {"deadline": 2, "reclaimed": 1, "archived": 1}
    assert (report["mismatches"], report["examples"]) == (0, [])


def test_compare_reports_real_mismatches(tmp_path: Path) -> None:
    before = rows(tmp_path, v2_tables())
    after = copy.deepcopy(before)
    next(j for j in after["jobs"] if j["id"] == 31).update(status="failed")
    next(r for r in after["runs"] if r["run_name"] == "r-completed").update(completed_at=None)
    after["runs"] = [r for r in after["runs"] if r["run_name"] != "r-claimed"]
    # A re-claimed job that the replay still holds in flight is not explained away.
    next(j for j in after["jobs"] if j["id"] == 9).update(run_name="r-new")

    report = compare(before, after, *keyed(before))

    assert report["mismatches"] == 4
    assert {(e["table"], e["key"], e["field"]) for e in report["examples"]} == {
        ("jobs", 31, "status"), ("runs", "r-completed", "completed_at"), ("runs", "r-claimed", "<row>"),
        ("jobs", 9, "<reclaimed>"),
    }
    stamp = next(e for e in report["examples"] if e["field"] == "completed_at")
    assert (stamp["replay"], stamp["v2"]) == (True, False)


def event_object(root: Path, lines: list[dict]) -> None:
    path = root / EVENTS_KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress("".join(json.dumps(line) + "\n" for line in lines).encode()))


@pytest.fixture()
def exports(tmp_path: Path) -> tuple[Path, Path, Path]:
    before_dir, after_dir, events_dir = (tmp_path / n for n in ("before", "after", "events"))
    for d in (before_dir, after_dir):
        d.mkdir()
    write_export(before_dir, v2_tables(), cursor={**CURSOR, "line_offset": 0})
    after = v2_tables()
    run = next(r for r in after["runs"] if r["run_name"] == "r-running")
    run.update(status="completed", completed_at=T11, wrapper_exit_code=0)
    next(j for j in after["jobs"] if j["id"] == 20).update(status="completed", completed_at=T11)
    write_export(after_dir, after, cursor={**CURSOR, "line_offset": 2})
    mark = {"process": "x:MARK_COMPLETE", "status": "COMPLETED", "tag": ZELLER_RS, "hash": "aa/bbbbbb"}
    event_object(events_dir, [
        {"source": "weblog", "received_at": T11, "payload": {"runName": "r-running", "event": "process_completed", "trace": mark}},
        {"source": "run_event", "received_at": T11, "run_name": "r-running", "payload": {"type": "wrapper_exited", "exit_code": 0}},
    ])
    return before_dir, after_dir, events_dir


def test_replay_of_fixture_exports_is_clean(db_url: str, exports: tuple[Path, Path, Path]) -> None:
    before_dir, after_dir, events_dir = exports
    events = v2_replay.blob_store(str(events_dir), r2_account_id="", r2_access_key_id="", r2_secret_access_key="")

    async def go(engine):
        return await v2_replay.replay(engine, read_snapshot(before_dir), read_snapshot(after_dir), events)

    report = with_engine(db_url, go)

    assert report["clean"] is True
    assert (report["lines_applied"], report["lines_rejected"], report["mismatches"]) == (2, 0, 0)


def test_cli_exit_codes(db_url: str, exports: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:
    args = [str(p) for p in exports]
    monkeypatch.setenv("SQLALCHEMY_URI", db_url)
    with pytest.raises(SystemExit) as clean:
        v2_replay.main(args)
    assert clean.value.code == 0 and json.loads(capsys.readouterr().out)["clean"] is True

    # An AFTER export that disagrees with what the events produce.
    tampered = exports[1].parent / "tampered"
    tampered.mkdir()
    after = v2_tables()
    next(j for j in after["jobs"] if j["id"] == 31).update(status="failed")
    write_export(tampered, after, cursor={**CURSOR, "line_offset": 2})
    with pytest.raises(SystemExit) as dirty:
        v2_replay.main([args[0], str(tampered), args[2]])
    assert dirty.value.code == 1 and json.loads(capsys.readouterr().out)["mismatches"] > 0


@pytest.mark.parametrize("uri", ["postgresql://u:p@localhost:5432/nf_telemetry",
                                 "postgresql+asyncpg://u:p@localhost:5432/nf_telemetry", "sqlite:///x.db", ""])
def test_cli_refuses_production_and_non_postgres(uri: str, exports: tuple[Path, Path, Path],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLALCHEMY_URI", uri)
    with pytest.raises(SystemExit) as refused:
        v2_replay.main([str(p) for p in exports])
    assert refused.value.code == 2
