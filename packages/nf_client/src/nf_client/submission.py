"""Submission utilities: Jinja template rendering and executor invocation.

These are convenience helpers. The caller (daemon, cron, operator) decides
when to call them — nf_client does not drive execution scheduling.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]  # Python 3.14+
except ImportError:
    from uuid_extensions import uuid7 as _uuid7
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import DispatchBatchResponse, DispatchedJob


def generate_run_name() -> str:
    """Return a time-sortable UUID7 suitable for use as a Nextflow -name value.

    The "r" prefix satisfies Nextflow's ^[a-z]... run-name constraint, since
    UUID7 begins with a hex digit.
    """
    return "r" + str(_uuid7())


def generate_metadata_tsv(jobs: list[DispatchedJob]) -> str:
    """Return TSV content with header sample_id\\tNCBI_accession.

    Each row maps a biosample ID to its semicolon-separated SRR accessions
    sourced from job.metadata['ncbi_accession'].
    """
    lines = ["sample_id\tNCBI_accession"]
    for job in jobs:
        accession = job.metadata.get("ncbi_accession", "")
        lines.append(f"{job.sample_id}\t{accession}")
    return "\n".join(lines)


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
    profile: str,
    weblog_url: str,
    extra_params: dict[str, str] | None = None,
) -> list[str]:
    """Build the nextflow run command for a dispatched batch.

    Workflow identity and repository details come from the server's dispatch
    response. ``profile`` is execution-environment-specific and must be
    supplied by the caller from ClientConfig.profile.

    Returns a list suitable for subprocess.run / subprocess.Popen.
    """
    run_name = batch.run_name
    sample_ids = ",".join(j.sample_id for j in batch.jobs)
    cmd = ["nextflow", "run", batch.repository_url]
    # -revision only applies to remote repos; skip for local paths
    if not batch.repository_url.startswith(("/", ".")):
        cmd += ["-revision", batch.revision]
    cmd += [
        "-profile", profile,
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


def submit_slurm(script_content: str, *, export_none: bool = True) -> str:
    """Submit a rendered sbatch script and return the SLURM job ID.

    export_none=True (default) adds --export=NONE, which prevents the
    submitting shell's environment from leaking into the compute node.
    Required on systems (e.g. Alpine) where SLURM's lmod prologue initialises
    the 'module' function via /etc/profile.d on a clean environment.
    Set export_none=False on systems (e.g. Anvil) where this is unnecessary
    or causes problems.
    """
    cmd = ["sbatch", "--parsable"]
    if export_none:
        cmd.append("--export=NONE")
    result = subprocess.run(
        cmd,
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
