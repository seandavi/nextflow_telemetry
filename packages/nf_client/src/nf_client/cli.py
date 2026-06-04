"""nf-client CLI — thin wrapper around the protocol library.

This CLI is intentionally minimal: it handles one claim+submit cycle.
Looping, resource-awareness, and scheduling are left to the operator
(cron, Airflow, site-specific daemon).

Usage:
    nf-client submit      --config client-hpc.yaml [--dry-run]
    nf-client fetch       --config client-hpc.yaml
    nf-client daemon      --config client-local.yaml [--batch-size 10]
    nf-client upload-logs --config client-hpc.yaml --run-name <name> --work-dir /path/to/work
"""
from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import time
from pathlib import Path

import typer
try:
    from importlib.metadata import version as _pkg_version
    _NF_CLIENT_VERSION: str | None = _pkg_version("nf-client")
except Exception:
    _NF_CLIENT_VERSION = None

from .client import JobClient
from .config import ClientConfig
from .submission import (
    build_nextflow_command,
    generate_metadata_tsv,
    render_submission_script,
    submit_local,
    submit_slurm,
    submit_pbs,
    submit_with_retry,
)

app = typer.Typer(help="nf-client: claim and submit Nextflow telemetry jobs")


# run_name format: "r" + uuid7, e.g. r069fa00c-f146-74a9-8000-99ccb39f2d7e
_RUN_NAME_RE = re.compile(r'^r[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _count_active_slurm_jobs() -> int:
    """Count running/pending nf-client wrapper jobs.

    Wrapper jobs are named with the UUID run_name assigned by the server.
    Child task jobs spawned by Nextflow use a different format (e.g. 'nf-<process>'),
    so UUID matching reliably isolates only the master wrapper jobs.
    """
    result = subprocess.run(
        ["squeue", "--me", "--noheader", "--format=%j"],
        capture_output=True,
        text=True,
    )
    return len([l for l in result.stdout.splitlines() if _RUN_NAME_RE.match(l.strip())])

config_option = typer.Option(..., "--config", "-c", help="Path to client YAML config")
dry_run_option = typer.Option(False, "--dry-run", help="Print what would be submitted without executing")


@app.command()
def fetch(
    config: Path = config_option,
) -> None:
    """Fetch the next batch of pending jobs and print them (no submission)."""

    async def _run():
        cfg = ClientConfig.from_yaml(config)
        async with JobClient(cfg) as client:
            batch = await client.fetch_next_batch()
        if batch is None:
            typer.echo("No pending jobs available.")
            return
        typer.echo(f"run_name:   {batch.run_name}")
        typer.echo(f"workflow:   {batch.workflow_id} v{batch.workflow_version}")
        typer.echo(f"repository: {batch.repository_url} @ {batch.revision}")
        typer.echo(f"profile:    {cfg.profile}")
        for job in batch.jobs:
            typer.echo(f"  {job.sample_id}")

    asyncio.run(_run())


@app.command()
def submit(
    config: Path = config_option,
    dry_run: bool = dry_run_option,
) -> None:
    """Claim a batch of jobs, submit to the executor, and report back to server."""

    async def _run():
        cfg = ClientConfig.from_yaml(config)
        async with JobClient(cfg) as client:
            batch = await client.fetch_next_batch()

        if batch is None:
            typer.echo("No pending jobs available.")
            raise typer.Exit(0)

        run_name = batch.run_name
        sample_ids = [j.sample_id for j in batch.jobs]

        typer.echo(f"Claimed {len(batch.jobs)} jobs — run_name: {run_name}")

        cmd = build_nextflow_command(
            batch=batch,
            profile=cfg.profile,
            weblog_url=cfg.weblog_url,
        )

        if dry_run:
            typer.echo("Dry run — command that would be submitted:")
            typer.echo("  " + " ".join(cmd))
            return

        executor_job_id: str | None = None
        mode = cfg.submission.mode

        if mode == "local":
            executor_job_id = submit_local(cmd)
            typer.echo(f"Launched local process PID {executor_job_id}")

        elif mode in ("slurm", "pbs", "lsf"):
            if not cfg.submission.template_path:
                typer.echo(f"ERROR: submission.template_path required for mode={mode}", err=True)
                raise typer.Exit(1)

            context = {
                **cfg.submission.defaults,
                "run_name": run_name,
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
            script = render_submission_script(cfg.submission.template_path, context)

            if mode == "slurm":
                executor_job_id = submit_with_retry(
                    lambda: submit_slurm(script, export_none=cfg.submission.slurm_export_none),
                    label="sbatch",
                )
            elif mode == "pbs":
                executor_job_id = submit_with_retry(
                    lambda: submit_pbs(script),
                    label="qsub",
                )
            else:
                # mode passed the guard above (slurm|pbs|lsf) but no
                # submit_lsf exists yet. Fail explicitly rather than
                # silently reporting a null executor_job_id to the server.
                typer.echo(f"ERROR: mode={mode!r} is in the supported set but no submit path is wired yet", err=True)
                raise typer.Exit(1)

            typer.echo(f"Submitted {mode.upper()} job {executor_job_id}")

        # Report back to server
        async with JobClient(cfg) as client:
            await client.report_submitted(
                run_name=run_name,
                sample_ids=sample_ids,
                executor_job_id=executor_job_id,
            )
        typer.echo(f"Reported submitted to server: {run_name}")

    asyncio.run(_run())


@app.command()
def daemon(
    config: Path = config_option,
    batch_size: int = typer.Option(0, "--batch-size", "-n", help="Samples per batch (0 = use config value)", min=0, max=500),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds to wait between polls when queue is empty or at concurrency limit"),
    continuous: bool = typer.Option(False, "--continuous", help="Keep running when queue is empty, polling for new jobs (overrides config)"),
) -> None:
    """Claim and submit batches continuously until no pending jobs remain.

    In local mode: runs nextflow synchronously before claiming the next batch.
    In slurm mode: submits via sbatch (non-blocking) and immediately claims the
    next batch, subject to max_concurrent_runs from the config.

    Pass --continuous (or set continuous: true in config) to keep running when
    the queue is empty so new samples can be added without restarting the daemon.
    """

    async def _fetch(cfg: ClientConfig, limit: int) -> object:
        async with JobClient(cfg) as client:
            return await client.fetch_next_batch(limit=limit)

    async def _report(cfg: ClientConfig, run_name: str, sample_ids: list[str], job_id: str | None) -> None:
        async with JobClient(cfg) as client:
            await client.report_submitted(run_name=run_name, sample_ids=sample_ids, executor_job_id=job_id)

    async def _heartbeat(cfg: ClientConfig, agent_id: str, active_runs: int, status: str) -> None:
        payload = {
            "agent_id": agent_id,
            "hostname": socket.gethostname(),
            "workflow_id": ",".join(cfg.dispatch.workflow_id) if cfg.dispatch.workflow_id else None,
            "profile": cfg.profile,
            "nf_client_version": _NF_CLIENT_VERSION,
            "config_yaml": cfg.sanitized_config_yaml(),
            "mode": cfg.submission.mode,
            "batch_size": cfg.dispatch.batch_size,
            "max_concurrent_runs": cfg.submission.max_concurrent_runs,
            "active_runs": active_runs,
            "status": status,
        }
        async with JobClient(cfg) as client:
            await client.post_heartbeat(payload)

    cfg = ClientConfig.from_yaml(config)
    run_number = 0

    hostname = socket.gethostname()
    agent_id = hostname

    def _safe_heartbeat(cfg: ClientConfig, active: int, status: str) -> None:
        """Heartbeat is observability only — never let it crash or stall the loop."""
        try:
            asyncio.run(_heartbeat(cfg, agent_id, active, status))
        except Exception as e:
            typer.echo(f"  WARN: heartbeat failed (API unreachable?): {e}", err=True)

    typer.echo(
        f"Daemon started — mode={cfg.submission.mode} batch_size={batch_size if batch_size > 0 else cfg.dispatch.batch_size}"
        + (f" max_concurrent_runs={cfg.submission.max_concurrent_runs}" if cfg.submission.max_concurrent_runs else "")
        + (" continuous=true" if (continuous or cfg.continuous) else "")
    )

    while True:
        # Reload config each iteration so edits take effect without restart.
        try:
            cfg = ClientConfig.from_yaml(config)
        except Exception as e:
            typer.echo(f"WARN: failed to reload config, using previous: {e}", err=True)

        effective_batch_size = batch_size if batch_size > 0 else cfg.dispatch.batch_size
        run_continuous = continuous or cfg.continuous
        mode = cfg.submission.mode
        max_concurrent = cfg.submission.max_concurrent_runs

        # For SLURM mode, check concurrency before fetching so we don't grab
        # jobs and then stall while holding a claim.
        if mode == "slurm" and max_concurrent is not None:
            active = _count_active_slurm_jobs()
            if active >= max_concurrent:
                typer.echo(f"  {active} SLURM jobs active (limit {max_concurrent}) — waiting {poll_interval}s")
                _safe_heartbeat(cfg, active, "running")
                time.sleep(poll_interval)
                continue
        else:
            active = 0

        _safe_heartbeat(cfg, active, "running" if active > 0 else "idle")

        # The API being unreachable (server restart, network blip) must not kill
        # the daemon — warn, back off, and retry on the next poll.
        try:
            batch = asyncio.run(_fetch(cfg, effective_batch_size))
        except Exception as e:
            typer.echo(f"  WARN: failed to reach API for next batch, retrying in {poll_interval}s: {e}", err=True)
            time.sleep(poll_interval)
            continue

        if batch is None:
            if run_continuous:
                typer.echo(f"No pending jobs — waiting {poll_interval}s")
                time.sleep(poll_interval)
                continue
            typer.echo("No pending jobs — daemon complete.")
            break

        run_number += 1
        sample_ids = [j.sample_id for j in batch.jobs]
        typer.echo(
            f"\n[run {run_number}] {batch.run_name}  "
            f"workflow={batch.workflow_id} v{batch.workflow_version}  "
            f"samples={len(sample_ids)}"
        )

        executor_job_id: str | None = None

        if mode == "local":
            cmd = build_nextflow_command(batch=batch, profile=cfg.profile, weblog_url=cfg.weblog_url)
            # Report submitted before blocking on nextflow — transitions claimed→submitted
            # before the weblog completed event arrives.
            try:
                asyncio.run(_report(cfg, batch.run_name, sample_ids, None))
            except Exception as e:
                typer.echo(f"  WARN: failed to report submitted: {e}", err=True)
            result = subprocess.run(cmd, capture_output=False)
            typer.echo(f"[run {run_number}] nextflow exited with code {result.returncode}")

        elif mode in ("slurm", "pbs", "lsf"):
            if not cfg.submission.template_path:
                typer.echo(f"ERROR: submission.template_path required for mode={mode}", err=True)
                raise typer.Exit(1)

            context = {
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
            script = render_submission_script(cfg.submission.template_path, context)

            try:
                if mode == "slurm":
                    executor_job_id = submit_with_retry(
                        lambda: submit_slurm(script, export_none=cfg.submission.slurm_export_none),
                        label="sbatch",
                    )
                elif mode == "pbs":
                    executor_job_id = submit_with_retry(
                        lambda: submit_pbs(script),
                        label="qsub",
                    )
                else:
                    # mode passed the outer guard but no submit path is
                    # wired (e.g. lsf). Skip the batch loudly so the server
                    # can sweep it via TTL rather than silently recording
                    # executor_job_id=None.
                    typer.echo(f"  ERROR: mode={mode!r} has no submit path wired; skipping batch (will requeue via TTL)", err=True)
                    continue
            except Exception as e:
                typer.echo(f"  ERROR: scheduler submission failed after retries, skipping batch (will requeue via TTL): {e}", err=True)
                continue

            typer.echo(f"  Submitted {mode.upper()} job {executor_job_id}")

            try:
                asyncio.run(_report(cfg, batch.run_name, sample_ids, executor_job_id))
            except Exception as e:
                typer.echo(f"  WARN: failed to report submitted: {e}", err=True)

            # Give the scheduler time to register the job before the next
            # concurrency check — squeue can lag a few seconds after sbatch.
            if max_concurrent is not None:
                time.sleep(5)

    typer.echo(f"\nDaemon finished after {run_number} run(s).")


@app.command()
def stats(
    config: Path = config_option,
) -> None:
    """Print a summary of system state: samples, workflows, jobs/runs by status, DLQ."""

    async def _run() -> dict:
        cfg = ClientConfig.from_yaml(config)
        async with JobClient(cfg) as client:
            return await client.get_stats()

    payload = asyncio.run(_run())

    typer.echo(f"samples:   {payload['samples']}")
    typer.echo(f"workflows: {payload['workflows']}")
    typer.echo(f"dead-letter (unresolved): {payload['dead_letter_unresolved']}")

    typer.echo("\njobs by status:")
    if payload["jobs_by_status"]:
        for status, count in sorted(payload["jobs_by_status"].items()):
            typer.echo(f"  {status:<10} {count}")
    else:
        typer.echo("  (none)")

    typer.echo("\nruns by status:")
    if payload["runs_by_status"]:
        for status, count in sorted(payload["runs_by_status"].items()):
            typer.echo(f"  {status:<10} {count}")
    else:
        typer.echo("  (none)")


@app.command(name="upload-logs")
def upload_logs(
    config: Path = config_option,
    run_name: str = typer.Option(..., "--run-name", "-r", help="Nextflow run name (value passed to -name)."),
    work_dir: Path = typer.Option(..., "--work-dir", "-w", help="Nextflow work directory (contains task subdirs)."),
    max_size_kb: int = typer.Option(5120, "--max-size-kb", help="Skip files larger than this many KB (default 5120 = 5 MB, matching the server cap). Lower it to trim noisy kraken2 stdout."),
    dry_run: bool = dry_run_option,
) -> None:
    """Walk a Nextflow work directory and upload .command.sh, .command.out and .command.err for each task.

    The Nextflow work directory structure is:

        work/<2-char-prefix>/<rest-of-hash>/
            .command.sh
            .command.out
            .command.err

    This command reconstructs the task_hash as "<prefix>/<rest>" and uploads each
    file to the server as log_type "command_sh", "command_out" and "command_err"
    respectively. Processes that log to stdout (e.g. kraken2's report) land in
    .command.out — without it those tasks appear to have no logs at all.

    Intended to be called after a Nextflow run completes, typically from the SLURM
    job script or a Nextflow afterScript hook. Idempotent — safe to re-run.
    """
    _LOG_FILES = {
        ".command.sh":  "command_sh",
        ".command.out": "command_out",
        ".command.err": "command_err",
    }
    max_bytes = max_size_kb * 1024

    if not work_dir.is_dir():
        typer.echo(f"ERROR: work-dir does not exist: {work_dir}", err=True)
        raise typer.Exit(1)

    # Discover all task directories: work/<2chars>/<rest>/
    tasks: list[tuple[str, Path]] = []
    for prefix_dir in sorted(work_dir.iterdir()):
        if not prefix_dir.is_dir() or len(prefix_dir.name) != 2:
            continue
        for task_dir in sorted(prefix_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            task_hash = f"{prefix_dir.name}/{task_dir.name}"
            tasks.append((task_hash, task_dir))

    if not tasks:
        typer.echo(f"No task directories found in {work_dir}")
        return

    typer.echo(f"Found {len(tasks)} task directories in {work_dir}")

    if dry_run:
        for task_hash, task_dir in tasks:
            for fname in _LOG_FILES:
                fpath = task_dir / fname
                if fpath.exists():
                    size_kb = fpath.stat().st_size // 1024
                    typer.echo(f"  would upload {task_hash}/{fname} ({size_kb} KB)")
        return

    cfg = ClientConfig.from_yaml(config)
    uploaded = skipped = errors = 0

    async def _upload_all() -> None:
        nonlocal uploaded, skipped, errors
        async with JobClient(cfg) as client:
            for task_hash, task_dir in tasks:
                for fname, log_type in _LOG_FILES.items():
                    fpath = task_dir / fname
                    if not fpath.exists():
                        continue
                    size = fpath.stat().st_size
                    if size > max_bytes:
                        typer.echo(f"  SKIP {task_hash}/{fname} ({size // 1024} KB > {max_size_kb} KB limit)")
                        skipped += 1
                        continue
                    content = fpath.read_text(errors="replace")
                    try:
                        await client.upload_task_log(
                            run_name=run_name,
                            task_hash=task_hash,
                            log_type=log_type,
                            content=content,
                        )
                        uploaded += 1
                    except Exception as exc:
                        typer.echo(f"  ERROR {task_hash}/{fname}: {exc}", err=True)
                        errors += 1

    asyncio.run(_upload_all())
    typer.echo(f"Done — uploaded: {uploaded}, skipped (too large): {skipped}, errors: {errors}")
