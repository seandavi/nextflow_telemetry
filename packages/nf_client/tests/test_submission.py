"""Unit tests for nf_client.submission — focused on the retry helper.

submit_with_retry wraps subprocess-based submit_* functions with exponential
backoff so transient SLURM/PBS controller failures don't burn the run claim
(see issue #21).
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nf_client.submission import submit_pbs, submit_slurm, submit_with_retry


def test_submit_with_retry_first_attempt_success() -> None:
    """No retry if the first call succeeds."""
    fn = MagicMock(return_value="12345")
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        assert submit_with_retry(fn, label="sbatch") == "12345"
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_succeeds_after_transient_failures() -> None:
    """Two transient CalledProcessError raises then a success returns the value."""
    err = subprocess.CalledProcessError(1, ["sbatch"], stderr="controller temporarily unreachable")
    fn = MagicMock(side_effect=[err, err, "67890"])
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        assert submit_with_retry(fn, label="sbatch") == "67890"
    assert fn.call_count == 3
    # Two retries → two sleeps (none after the final attempt)
    assert sleep_mock.call_count == 2


def test_submit_with_retry_raises_after_max_attempts() -> None:
    """All attempts fail — the last exception propagates so the caller can record it."""
    err = subprocess.CalledProcessError(2, ["sbatch"], stderr="quota exceeded")
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep"):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            submit_with_retry(fn, max_attempts=3, label="sbatch")
    assert exc_info.value is err
    assert fn.call_count == 3


def test_submit_with_retry_exponential_backoff_schedule() -> None:
    """Backoff doubles between attempts: 2.0s, then 4.0s, then 8.0s, ..."""
    err = subprocess.CalledProcessError(1, ["sbatch"])
    fn = MagicMock(side_effect=[err, err, err, "ok"])
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        result = submit_with_retry(
            fn, max_attempts=4, initial_backoff=2.0, backoff_multiplier=2.0, label="sbatch"
        )
    assert result == "ok"
    assert [call.args[0] for call in sleep_mock.call_args_list] == [2.0, 4.0, 8.0]


def test_submit_with_retry_propagates_oserror() -> None:
    """FileNotFoundError (no sbatch in PATH) is treated as retryable."""
    fn = MagicMock(side_effect=[FileNotFoundError("sbatch: not found"), "abc"])
    with patch("nf_client.submission.time.sleep"):
        assert submit_with_retry(fn, label="sbatch") == "abc"
    assert fn.call_count == 2


def test_submit_with_retry_does_not_swallow_unrelated_exceptions() -> None:
    """A non-subprocess/non-OS exception (e.g. ValueError) propagates immediately."""
    fn = MagicMock(side_effect=ValueError("bad arg"))
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        with pytest.raises(ValueError):
            submit_with_retry(fn, label="sbatch")
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_single_attempt_no_sleep() -> None:
    """max_attempts=1 disables retry entirely — first failure raises with no sleep."""
    err = subprocess.CalledProcessError(1, ["sbatch"])
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        with pytest.raises(subprocess.CalledProcessError):
            submit_with_retry(fn, max_attempts=1, label="sbatch")
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_rejects_zero_or_negative_max_attempts() -> None:
    """max_attempts < 1 is a programming error — fail fast, don't silently no-op."""
    fn = MagicMock()
    with pytest.raises(ValueError, match=r"max_attempts must be >= 1"):
        submit_with_retry(fn, max_attempts=0, label="sbatch")
    with pytest.raises(ValueError, match=r"max_attempts must be >= 1"):
        submit_with_retry(fn, max_attempts=-3, label="sbatch")
    assert fn.call_count == 0


def test_submit_with_retry_preserves_original_traceback_on_final_failure() -> None:
    """The last attempt re-raises with bare `raise` (not `raise last_exc`) so the
    operator sees the underlying subprocess call's frame chain, not a synthetic
    `raise last_exc` frame inside this helper.
    """
    err = subprocess.CalledProcessError(2, ["sbatch"])
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep"):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            submit_with_retry(fn, max_attempts=2, label="sbatch")
    tb = exc_info.value.__traceback__
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    # The chain should pass through submit_with_retry's `try` callsite, not
    # through a synthetic `raise last_exc` block at the bottom of the function.
    assert "submit_with_retry" in frames


def test_submit_slurm_through_retry_helper_end_to_end() -> None:
    """submit_with_retry wrapping submit_slurm survives one failure then succeeds.

    Patches subprocess.run at the submission module level so we exercise the real
    submit_slurm call path (cmd construction, --parsable, --export=NONE, etc.).
    """
    fail = subprocess.CalledProcessError(1, ["sbatch"], stderr="transient")
    ok = MagicMock(stdout="98765\n")
    with patch("nf_client.submission.subprocess.run", side_effect=[fail, ok]) as run_mock:
        with patch("nf_client.submission.time.sleep"):
            job_id = submit_with_retry(
                lambda: submit_slurm("#!/bin/bash\necho hi", export_none=True),
                label="sbatch",
            )
    assert job_id == "98765"
    assert run_mock.call_count == 2
    # The cmd shape didn't change across attempts.
    first_cmd = run_mock.call_args_list[0].args[0]
    assert first_cmd == ["sbatch", "--parsable", "--export=NONE"]


def test_submit_pbs_through_retry_helper_end_to_end() -> None:
    """Same path for qsub."""
    fail = subprocess.CalledProcessError(1, ["qsub"], stderr="transient")
    ok = MagicMock(stdout="11111.pbsserver\n")
    with patch("nf_client.submission.subprocess.run", side_effect=[fail, ok]) as run_mock:
        with patch("nf_client.submission.time.sleep"):
            job_id = submit_with_retry(lambda: submit_pbs("#!/bin/bash\necho hi"), label="qsub")
    assert job_id == "11111.pbsserver"
    assert run_mock.call_count == 2
    assert run_mock.call_args_list[0].args[0] == ["qsub"]


def test_slurm_template_gives_each_run_its_own_pipeline_checkout() -> None:
    """#192: a shared $NXF_HOME/assets checkout races on .git/index when runs start together."""
    from pathlib import Path

    from nf_client.submission import render_submission_script

    tmpl = Path(__file__).resolve().parents[3] / "templates" / "submit_slurm.sh.j2"
    ctx = {
        "mem": "8G", "cpus": 2, "time": "1:00:00", "partition": "p", "log_dir": "/l",
        "run_name": "r1", "sample_ids": "s", "workflow_repository": "o/r",
        "workflow_revision": "1.0", "profile": "x,r2", "server_url": "u",
        "weblog_url": "w", "workflow_id": "wf", "workflow_version": "1",
        "metadata_tsv_content": "",
    }
    script = render_submission_script(tmpl, ctx)
    assert "export NXF_ASSETS=$WORKDIR/assets" in script
    assert script.index("NXF_ASSETS=") < script.index("nextflow run")


def test_slurm_template_publishes_under_the_registered_workflow() -> None:
    """#220: the pipeline's manifest version splits one registration and merges bundles."""
    from pathlib import Path

    from nf_client.submission import render_submission_script

    tmpl = Path(__file__).resolve().parents[3] / "templates" / "submit_slurm.sh.j2"
    ctx = {
        "mem": "8G", "cpus": 2, "time": "1:00:00", "partition": "p", "log_dir": "/l",
        "run_name": "r1", "sample_ids": "s", "workflow_repository": "o/r",
        "workflow_revision": "2.2.3", "profile": "x,r2", "server_url": "u",
        "weblog_url": "w", "workflow_id": "cmgd_nextflow", "workflow_version": "2.2.1",
        "metadata_tsv_content": "",
    }
    assert "--publish_dir" not in render_submission_script(tmpl, ctx)
    script = render_submission_script(tmpl, {**ctx, "publish_base": "s3://cmgd-raw"})
    assert "--publish_dir s3://cmgd-raw/cmgd_nextflow/2.2.1 \\" in script


def test_slurm_template_passes_registration_params_as_a_params_file() -> None:
    """#222: a registration's params reach Nextflow via -params-file; orchestrator params stay on the CLI."""
    import json
    from pathlib import Path

    from nf_client.config import ClientConfig
    from nf_client.models import DispatchBatchResponse
    from nf_client.submission import build_submission_context, render_submission_script

    tmpl = Path(__file__).resolve().parents[3] / "templates" / "submit_slurm.sh.j2"
    batch = DispatchBatchResponse.model_validate({
        "run_name": "r1", "workflow_id": "cmgd_humann4a1", "workflow_version": "2.3.0",
        "workflow_pk": 1, "repository_url": "o/r", "revision": "2.3.0",
        "params": {"humann_bundle": "humann4.0.0a1", "skip_humann": False},
        "jobs": [{"sample_id": "s1", "ncbi_accession": "SRR1"}],
    })
    cfg = ClientConfig.model_validate({
        "server_url": "u", "weblog_url": "w",
        "submission": {"mode": "slurm", "defaults": {
            "mem": "8G", "cpus": 2, "time": "1:00:00", "partition": "p", "log_dir": "/l",
            "publish_base": "s3://cmgd-raw",
        }},
    })
    script = render_submission_script(tmpl, build_submission_context(batch, cfg, ["s1"]))

    body = script.split("cat << 'PARAMSJSON' > params.json\n", 1)[1].split("\nPARAMSJSON\n", 1)[0]
    # A JSON boolean, not the string "false", which Groovy reads as true.
    assert json.loads(body) == {"humann_bundle": "humann4.0.0a1", "skip_humann": False}
    run = script[script.index("nextflow run"):]
    assert "    -params-file params.json \\" in run
    for owned in ("--metadata_tsv metadata.tsv", "--run_name r1", "--publish_dir s3://cmgd-raw/cmgd_humann4a1/2.3.0"):
        assert owned in run

    plain = render_submission_script(
        tmpl, build_submission_context(batch.model_copy(update={"params": {}}), cfg, ["s1"])
    )
    assert "params.json" not in plain


def test_local_command_puts_registration_params_before_orchestrator_params() -> None:
    from nf_client.models import DispatchBatchResponse
    from nf_client.submission import build_nextflow_command

    batch = DispatchBatchResponse.model_validate({
        "run_name": "r1", "workflow_id": "wf", "workflow_version": "1", "workflow_pk": 1,
        "repository_url": "o/r", "revision": "main",
        "params": {"skip_humann": True, "run_name": "x"}, "jobs": [{"sample_id": "s1"}],
    })
    cmd = build_nextflow_command(batch=batch, profile="p", weblog_url="w")
    assert cmd[cmd.index("--skip_humann") + 1] == "true"
    # Nextflow keeps the last value of a repeated param, so the orchestrator's --run_name wins.
    assert cmd.index("--run_name") < cmd.index("--sample_ids") < len(cmd) - 1 - cmd[::-1].index("--run_name")


def test_register_workflow_param_types() -> None:
    import typer

    from nf_client.cli import _parse_param

    assert _parse_param("skip_humann=false") == ("skip_humann", False)
    assert _parse_param("threads=8") == ("threads", 8)
    assert _parse_param("humann_bundle=humann3.9") == ("humann_bundle", "humann3.9")
    assert _parse_param("v=4.0") == ("v", "4.0")
    assert _parse_param("id=007") == ("id", "007")
    assert _parse_param("expr=a=b") == ("expr", "a=b")
    with pytest.raises(typer.BadParameter):
        _parse_param("novalue")


def test_slurm_template_keeps_task_dirs_under_the_run_dir() -> None:
    """workDir must be run-local so the end-of-run rm -rf $WORKDIR cleans task dirs."""
    from pathlib import Path

    from nf_client.submission import render_submission_script

    tmpl = Path(__file__).resolve().parents[3] / "templates" / "submit_slurm.sh.j2"
    ctx = {
        "mem": "8G", "cpus": 2, "time": "1:00:00", "partition": "p", "log_dir": "/l",
        "run_name": "r1", "sample_ids": "s", "workflow_repository": "o/r",
        "workflow_revision": "1.0", "profile": "anvil,r2", "server_url": "u",
        "weblog_url": "w", "workflow_id": "wf", "workflow_version": "1",
        "metadata_tsv_content": "",
    }
    script = render_submission_script(tmpl, ctx)
    override = script.split("cat << NFOVERRIDE > nextflow_override.config", 1)[1].split("NFOVERRIDE", 1)[0]
    assert "workDir = 'work'" in override
    assert "trap cleanup EXIT" in script


def _run_rendered(tmp_path, wrapper_body: str, env_extra: dict, signal_after: float | None = None):
    """Render the real template, stub nf-client, run the batch script under bash."""
    import os
    import signal as sig
    import subprocess
    import time
    from pathlib import Path

    from nf_client.submission import render_submission_script

    home, scratch, logs, bindir = (tmp_path / d for d in ("home", "scratch", "logs", "bin"))
    for d in (home, scratch, logs, bindir):
        d.mkdir()
    (home / ".nf_tel.env").write_text(
        f"export NF_TEL_SCRATCH={scratch} NF_TEL_LOGS={logs} NF_TEL_STORE={tmp_path}/store "
        f"NF_TEL_SIF_CACHE={tmp_path}/sif NF_TEL_NXF_HOME={tmp_path}/nxf NF_TEL_MODULES=\n")
    (bindir / "nf-client").write_text("#!/bin/bash\n" + wrapper_body)
    (bindir / "nf-client").chmod(0o755)
    tmpl = Path(__file__).resolve().parents[3] / "templates" / "submit_slurm.sh.j2"
    ctx = {"mem": "1G", "cpus": 1, "time": "1:00:00", "partition": "p", "log_dir": str(logs),
           "run_name": "r1", "sample_ids": "s", "workflow_repository": "o/r",
           "workflow_revision": "1.0", "profile": "local", "server_url": "u",
           "weblog_url": "w", "workflow_id": "wf", "workflow_version": "1",
           "metadata_tsv_content": ""}
    script = tmp_path / "job.sh"
    script.write_text(render_submission_script(tmpl, ctx))
    env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "SLURM_JOB_ID": "4242", **env_extra}
    p = subprocess.Popen(["bash", str(script)], env=env, cwd=tmp_path,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if signal_after is not None:
        time.sleep(signal_after)
        os.kill(p.pid, sig.SIGUSR1)
    out, _ = p.communicate(timeout=30)
    return p.returncode, out, scratch / "4242"


def test_run_dir_removed_on_success(tmp_path) -> None:
    rc, out, rundir = _run_rendered(tmp_path, "mkdir -p work/ab; echo x > work/ab/f; exit 0\n", {})
    assert rc == 0 and not rundir.exists(), out


def test_failed_run_dir_kept_only_with_keep_failed(tmp_path) -> None:
    rc, out, rundir = _run_rendered(tmp_path, "exit 3\n", {"NF_TEL_KEEP_FAILED": "1"})
    assert rc == 3 and (rundir / ".keep_failed").exists(), out
    (tmp_path / "plain").mkdir()
    rc, out, rundir2 = _run_rendered(tmp_path / "plain", "exit 3\n", {})
    assert rc == 3 and not rundir2.exists(), out


def test_walltime_signal_stops_wrapper_and_cleans_up(tmp_path) -> None:
    # The stub records the TERM forwarded by the batch script, then exits like the wrapper would.
    body = "trap 'echo got-term > $NF_TEL_SCRATCH/../term_seen; exit 143' TERM\nsleep 20 & wait\n"
    rc, out, rundir = _run_rendered(tmp_path, body, {}, signal_after=1.5)
    assert rc == 143, out
    assert (tmp_path / "term_seen").exists(), out
    assert not rundir.exists(), out
