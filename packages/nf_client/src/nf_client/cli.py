"""nf-client CLI — thin wrapper around the protocol library.

This CLI is intentionally minimal: it handles one claim+submit cycle.
Looping, resource-awareness, and scheduling are left to the operator
(cron, Airflow, site-specific daemon).

Usage:
    nf-client submit --config client-hpc.yaml [--dry-run]
    nf-client fetch  --config client-hpc.yaml
    nf-client daemon --config client-local.yaml [--batch-size 10]
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import typer

from .client import JobClient
from .config import ClientConfig
from .submission import (
    build_nextflow_command,
    generate_metadata_tsv,
    render_submission_script,
    submit_local,
    submit_slurm,
    submit_pbs,
)

app = typer.Typer(help="nf-client: claim and submit Nextflow telemetry jobs")


def _count_active_slurm_jobs() -> int:
    """Count running/pending SLURM jobs for the current user."""
    result = subprocess.run(
        ["squeue", "--me", "--noheader", "--format=%j"],
        capture_output=True,
        text=True,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])

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
        typer.echo(f"profile:    {batch.profile}")
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
                "profile": batch.profile,
                "weblog_url": cfg.weblog_url,
                "workflow_id": batch.workflow_id,
                "workflow_version": batch.workflow_version,
                "metadata_tsv_content": generate_metadata_tsv(batch.jobs),
            }
            script = render_submission_script(cfg.submission.template_path, context)

            if mode == "slurm":
                executor_job_id = submit_slurm(script, export_none=cfg.submission.slurm_export_none)
            elif mode == "pbs":
                executor_job_id = submit_pbs(script)

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
) -> None:
    """Claim and submit batches continuously until no pending jobs remain.

    In local mode: runs nextflow synchronously before claiming the next batch.
    In slurm mode: submits via sbatch (non-blocking) and immediately claims the
    next batch, subject to max_concurrent_runs from the config.
    """

    async def _fetch(cfg: ClientConfig, limit: int) -> object:
        async with JobClient(cfg) as client:
            return await client.fetch_next_batch(limit=limit)

    async def _report(cfg: ClientConfig, run_name: str, sample_ids: list[str], job_id: str | None) -> None:
        async with JobClient(cfg) as client:
            await client.report_submitted(run_name=run_name, sample_ids=sample_ids, executor_job_id=job_id)

    cfg = ClientConfig.from_yaml(config)
    effective_batch_size = batch_size if batch_size > 0 else cfg.dispatch.batch_size
    mode = cfg.submission.mode
    max_concurrent = cfg.submission.max_concurrent_runs
    run_number = 0

    typer.echo(
        f"Daemon started — mode={mode} batch_size={effective_batch_size}"
        + (f" max_concurrent_runs={max_concurrent}" if max_concurrent else "")
    )

    while True:
        # For SLURM mode, check concurrency before fetching so we don't grab
        # jobs and then stall while holding a claim.
        if mode == "slurm" and max_concurrent is not None:
            active = _count_active_slurm_jobs()
            if active >= max_concurrent:
                typer.echo(f"  {active} SLURM jobs active (limit {max_concurrent}) — waiting {poll_interval}s")
                time.sleep(poll_interval)
                continue

        batch = asyncio.run(_fetch(cfg, effective_batch_size))

        if batch is None:
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
            cmd = build_nextflow_command(batch=batch, weblog_url=cfg.weblog_url)
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
                "profile": batch.profile,
                "weblog_url": cfg.weblog_url,
                "workflow_id": batch.workflow_id,
                "workflow_version": batch.workflow_version,
                "metadata_tsv_content": generate_metadata_tsv(batch.jobs),
            }
            script = render_submission_script(cfg.submission.template_path, context)

            if mode == "slurm":
                executor_job_id = submit_slurm(script, export_none=cfg.submission.slurm_export_none)
            elif mode == "pbs":
                executor_job_id = submit_pbs(script)

            typer.echo(f"  Submitted {mode.upper()} job {executor_job_id}")

            try:
                asyncio.run(_report(cfg, batch.run_name, sample_ids, executor_job_id))
            except Exception as e:
                typer.echo(f"  WARN: failed to report submitted: {e}", err=True)

    typer.echo(f"\nDaemon finished after {run_number} run(s).")
