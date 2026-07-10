"""Submission utilities: Jinja template rendering and executor invocation.

These are convenience helpers. The caller (daemon, cron, operator) decides
when to call them — nf_client does not drive execution scheduling.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

try:
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]  # Python 3.14+
except ImportError:
    from uuid_extensions import uuid7 as _uuid7
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import ClientConfig
from .models import DispatchBatchResponse, DispatchedJob


def generate_run_name() -> str:
    """Return a time-sortable UUID7 suitable for use as a Nextflow -name value.

    The "r" prefix satisfies Nextflow's ^[a-z]... run-name constraint, since
    UUID7 begins with a hex digit.
    """
    return "r" + str(_uuid7())


def generate_metadata_tsv(jobs: list[DispatchedJob]) -> str:
    """Return TSV content with header sample_id\\tNCBI_accession.

    Uses the top-level ncbi_accession field; falls back to metadata dict
    for compatibility with older server responses.
    """
    lines = ["sample_id\tNCBI_accession"]
    for job in jobs:
        accession = job.ncbi_accession or job.metadata.get("ncbi_accession", "")
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


def submit_with_retry(
    submit_callable: Callable[[], str],
    *,
    max_attempts: int = 3,
    initial_backoff: float = 2.0,
    backoff_multiplier: float = 2.0,
    label: str = "submit",
) -> str:
    # Three attempts with 2s and 4s waits between covers the bulk of real-world
    # transient failures (controller momentary unresponsiveness, NFS hiccup, sshd
    # restart on a login node) without making the daemon block for minutes on a
    # genuinely permanent failure like an exhausted account quota.
    if max_attempts < 1:
        # A loop that never runs would otherwise fall through to a
        # post-loop `raise`, which under `python -O` strips the assert
        # and surfaces as a bare UnboundLocalError. Better to fail fast.
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    for attempt in range(1, max_attempts + 1):
        try:
            return submit_callable()
        except (subprocess.SubprocessError, OSError) as e:
            if attempt == max_attempts:
                # Bare `raise` preserves the original exception's traceback
                # so the operator sees where the underlying subprocess call
                # actually failed, not a synthetic frame inside this helper.
                raise
            delay = initial_backoff * (backoff_multiplier ** (attempt - 1))
            print(
                f"  {label} attempt {attempt}/{max_attempts} failed: {e}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    # Unreachable: the loop always exits via `return` (success) or `raise`
    # (final-attempt failure). Kept as a defensive guard for type-checkers
    # that can't prove the invariant.
    raise RuntimeError("submit_with_retry exited loop without returning or raising")


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


# ── Scheduler Submitter seam ────────────────────────────────────────────────
#
# submit() and daemon() in cli.py both need: build the same Jinja context,
# render the template, then dispatch via the right scheduler CLI. This is
# that "same context, dispatch differs by mode" logic in one place.
#
# Failure signalling is split into two named exceptions because the two
# callers handle these two cases *differently* (submit() exits nonzero for
# both; daemon() exits nonzero for a missing template_path but skips the
# batch — via `continue`, letting the server's TTL sweep requeue it — for an
# unwired mode). A genuine submission failure (e.g. sbatch down after
# retries) is NOT wrapped here: it propagates as whatever submit_with_retry
# raises, exactly as it did before this refactor, so submit() still lets it
# crash uncaught while daemon()'s existing `except Exception: continue`
# still catches it.


class TemplatePathMissingError(Exception):
    """cfg.submission.template_path is unset for a scheduler mode that requires it."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f"submission.template_path required for mode={mode}")


class UnwiredSchedulerError(Exception):
    """*mode* is a recognised scheduler mode but has no submit path wired yet (e.g. lsf)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f"mode={mode!r} has no submit path wired yet")


def build_submission_context(
    batch: DispatchBatchResponse,
    cfg: ClientConfig,
    sample_ids: list[str],
) -> dict[str, Any]:
    """Build the Jinja2 template context for a scheduler submission script.

    Identical for every scheduler mode — only how the rendered script gets
    submitted (sbatch vs qsub) differs.
    """
    return {
        **cfg.submission.defaults,
        "run_name": batch.run_name,
        "sample_ids": ",".join(sample_ids),
        "workflow_repository": batch.repository_url,
        "workflow_revision": batch.revision,
        "profile": cfg.profile,
        "server_url": cfg.server_url,
        "weblog_url": cfg.weblog_url,
        "workflow_id": batch.workflow_id,
        "workflow_version": batch.workflow_version,
        "metadata_tsv_content": generate_metadata_tsv(batch.jobs),
    }


def _dispatch_slurm(script: str, cfg: ClientConfig) -> str:
    return submit_with_retry(
        lambda: submit_slurm(script, export_none=cfg.submission.slurm_export_none),
        label="sbatch",
    )


def _dispatch_pbs(script: str, cfg: ClientConfig) -> str:
    return submit_with_retry(lambda: submit_pbs(script), label="qsub")


# Registry of wired scheduler modes. Modes valid in SubmissionConfig.mode but
# absent here (currently just "lsf") raise UnwiredSchedulerError.
SCHEDULER_SUBMITTERS: dict[str, Callable[[str, ClientConfig], str]] = {
    "slurm": _dispatch_slurm,
    "pbs": _dispatch_pbs,
}


def submit_to_scheduler(
    mode: str,
    batch: DispatchBatchResponse,
    cfg: ClientConfig,
    sample_ids: list[str],
) -> str:
    """Render the submission script for *mode* and dispatch it, returning the executor job id.

    Raises TemplatePathMissingError / UnwiredSchedulerError for the two "can't
    even try" cases; a scheduler submission failure after retries propagates
    as whatever submit_with_retry raised (subprocess.SubprocessError/OSError).
    """
    if not cfg.submission.template_path:
        raise TemplatePathMissingError(mode)

    submitter = SCHEDULER_SUBMITTERS.get(mode)
    if submitter is None:
        raise UnwiredSchedulerError(mode)

    context = build_submission_context(batch, cfg, sample_ids)
    script = render_submission_script(cfg.submission.template_path, context)
    return submitter(script, cfg)
