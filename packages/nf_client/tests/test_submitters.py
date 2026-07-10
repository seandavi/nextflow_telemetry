"""Unit tests for the scheduler Submitter seam in nf_client.submission.

Covers the pieces that used to be duplicated verbatim between cli.py's
submit() and daemon() commands: context-building, scheduler dispatch, and
the two "can't even try" signals (missing template_path, unwired mode).
No real sbatch/qsub — subprocess.run is monkeypatched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nf_client.config import ClientConfig
from nf_client.models import DispatchBatchResponse, DispatchedJob
from nf_client.submission import (
    SCHEDULER_SUBMITTERS,
    TemplatePathMissingError,
    UnwiredSchedulerError,
    build_submission_context,
    submit_to_scheduler,
)


def _batch() -> DispatchBatchResponse:
    return DispatchBatchResponse(
        run_name="r-test-001",
        workflow_id="curatedMetagenomics",
        workflow_version="1.0.0",
        workflow_pk=1,
        repository_url="https://github.com/org/pipeline",
        revision="main",
        jobs=[
            DispatchedJob(sample_id="SRR001", ncbi_accession="SRR001"),
            DispatchedJob(sample_id="SRR002", ncbi_accession="SRR002"),
        ],
    )


def _cfg(mode: str, template_path: Path | None) -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "server_url": "http://test.local",
            "weblog_url": "http://test.local/telemetry",
            "submission": {
                "mode": mode,
                "template_path": str(template_path) if template_path else None,
                "defaults": {"account": "abc123"},
            },
        }
    )


def test_build_submission_context_has_expected_keys() -> None:
    batch = _batch()
    cfg = _cfg("slurm", Path("/tmp/x.sh.j2"))
    ctx = build_submission_context(batch, cfg, [j.sample_id for j in batch.jobs])

    assert ctx["run_name"] == "r-test-001"
    assert ctx["sample_ids"] == "SRR001,SRR002"
    assert ctx["workflow_repository"] == "https://github.com/org/pipeline"
    assert ctx["workflow_revision"] == "main"
    assert ctx["profile"] == cfg.profile
    assert ctx["server_url"] == "http://test.local"
    assert ctx["weblog_url"] == "http://test.local/telemetry"
    assert ctx["workflow_id"] == "curatedMetagenomics"
    assert ctx["workflow_version"] == "1.0.0"
    assert "SRR001\tSRR001" in ctx["metadata_tsv_content"]
    # cfg.submission.defaults keys are merged in.
    assert ctx["account"] == "abc123"


def test_submit_to_scheduler_missing_template_path_raises() -> None:
    batch = _batch()
    cfg = _cfg("slurm", None)
    with pytest.raises(TemplatePathMissingError):
        submit_to_scheduler("slurm", batch, cfg, ["SRR001"])


def test_submit_to_scheduler_unwired_mode_raises(tmp_path: Path) -> None:
    tmpl = tmp_path / "submit.sh.j2"
    tmpl.write_text("#!/bin/bash\necho {{ run_name }}\n")
    batch = _batch()
    cfg = _cfg("lsf", tmpl)
    with pytest.raises(UnwiredSchedulerError):
        submit_to_scheduler("lsf", batch, cfg, ["SRR001"])


def test_scheduler_submitters_registry_has_slurm_and_pbs_only() -> None:
    assert set(SCHEDULER_SUBMITTERS) == {"slurm", "pbs"}


def test_submit_to_scheduler_dispatches_slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmpl = tmp_path / "submit.sh.j2"
    tmpl.write_text("#!/bin/bash\n#SBATCH --job-name={{ run_name }}\n")
    batch = _batch()
    cfg = _cfg("slurm", tmpl)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="12345\n", stderr="")

    monkeypatch.setattr("nf_client.submission.subprocess.run", fake_run)

    job_id = submit_to_scheduler("slurm", batch, cfg, [j.sample_id for j in batch.jobs])

    assert job_id == "12345"
    assert calls == [["sbatch", "--parsable", "--export=NONE"]]


def test_submit_to_scheduler_dispatches_pbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmpl = tmp_path / "submit.sh.j2"
    tmpl.write_text("#!/bin/bash\n# {{ run_name }}\n")
    batch = _batch()
    cfg = _cfg("pbs", tmpl)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="999.pbsserver\n", stderr="")

    monkeypatch.setattr("nf_client.submission.subprocess.run", fake_run)

    job_id = submit_to_scheduler("pbs", batch, cfg, [j.sample_id for j in batch.jobs])

    assert job_id == "999.pbsserver"
    assert calls == [["qsub"]]


def test_submit_to_scheduler_slurm_respects_export_none_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmpl = tmp_path / "submit.sh.j2"
    tmpl.write_text("#!/bin/bash\n#SBATCH --job-name={{ run_name }}\n")
    batch = _batch()
    cfg = ClientConfig.model_validate(
        {
            "server_url": "http://test.local",
            "submission": {"mode": "slurm", "template_path": str(tmpl), "slurm_export_none": False},
        }
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")

    monkeypatch.setattr("nf_client.submission.subprocess.run", fake_run)

    submit_to_scheduler("slurm", batch, cfg, ["SRR001"])

    assert calls == [["sbatch", "--parsable"]]


def test_submit_to_scheduler_propagates_failure_after_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduler failure that exhausts retries propagates as-is (not wrapped) —
    submit() lets it crash uncaught, daemon() catches (SubprocessError, OSError)
    and continues.
    """
    tmpl = tmp_path / "submit.sh.j2"
    tmpl.write_text("#!/bin/bash\necho {{ run_name }}\n")
    batch = _batch()
    cfg = _cfg("slurm", tmpl)

    def always_fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="quota exceeded")

    monkeypatch.setattr("nf_client.submission.subprocess.run", always_fail)
    monkeypatch.setattr("nf_client.submission.time.sleep", lambda *_: None)

    with pytest.raises(subprocess.CalledProcessError):
        submit_to_scheduler("slurm", batch, cfg, ["SRR001"])
