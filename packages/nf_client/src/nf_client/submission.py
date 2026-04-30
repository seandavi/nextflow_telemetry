"""Submission utilities: Jinja template rendering and executor invocation.

These are convenience helpers. The caller (daemon, cron, operator) decides
when to call them — nf_client does not drive execution scheduling.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 13):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import DispatchBatchResponse


def generate_run_name() -> str:
    """Return a time-sortable UUID7 suitable for use as a Nextflow -name value.

    The "r" prefix satisfies Nextflow's ^[a-z]... run-name constraint, since
    UUID7 begins with a hex digit.
    """
    return "r" + str(_uuid7())


def render_submission_script(
    template_path: Path,
    context: dict[str, Any],
) -> str:
    """Render a Jinja2 submission script template with the given context.

    The template has access to all keys in *context* plus the standard
    Jinja2 filters. Use ``{{ value | default('fallback') }}`` for optional vars.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template(template_path.name)
    return tmpl.render(**context)


def build_nextflow_command(
    *,
    batch: DispatchBatchResponse,
    weblog_url: str,
    extra_params: dict[str, str] | None = None,
) -> list[str]:
    """Build the nextflow run command for a dispatched batch.

    All workflow execution details (repository, revision, profile, run_name)
    come from the server's dispatch response — no local workflow config needed.
    Using batch.run_name directly ensures telemetry correlation is never broken
    by a caller passing a mismatched name.

    Returns a list suitable for subprocess.run / subprocess.Popen.
    """
    run_name = batch.run_name
    sample_ids = ",".join(j.sample_id for j in batch.jobs)
    cmd = ["nextflow", "run", batch.repository_url]
    # -revision only applies to remote repos; skip for local paths
    if not batch.repository_url.startswith(("/", ".")):
        cmd += ["-revision", batch.revision]
    cmd += [
        "-profile", batch.profile,
        "-name", run_name,
        "-with-weblog", weblog_url,
        "--sample_ids", sample_ids,
        "--workflow_id", batch.workflow_id,
        "--workflow_version", batch.workflow_version,
        "--run_name", run_name,
    ]
    for key, value in (extra_params or {}).items():
        cmd.extend([f"--{key}", value])
    return cmd


def submit_local(cmd: list[str], log_file: Path | None = None) -> str:
    """Run nextflow directly in a subprocess and return its PID as the job ID.

    Non-blocking: the nextflow process runs in the background.
    """
    if log_file:
        log_fh = open(log_file, "w")
        stdout = log_fh
    else:
        log_fh = None
        stdout = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if log_fh:
            log_fh.close()

    return str(proc.pid)


def submit_slurm(script_content: str) -> str:
    """Submit a rendered sbatch script and return the SLURM job ID."""
    result = subprocess.run(
        ["sbatch", "--parsable"],
        input=script_content,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def submit_pbs(script_content: str) -> str:
    """Submit a rendered qsub script and return the PBS job ID."""
    result = subprocess.run(
        ["qsub"],
        input=script_content,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()
