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
    render_submission_script,
    submit_local,
    submit_slurm,
    submit_pbs,
)

app = typer.Typer(help="nf-client: claim and submit Nextflow telemetry jobs")

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
            }
            script = render_submission_script(cfg.submission.template_path, context)

            if mode == "slurm":
                executor_job_id = submit_slurm(script)
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
    batch_size: int = typer.Option(10, "--batch-size", "-n", help="Samples per batch", min=1, max=500),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds to wait between polls when queue is empty"),
) -> None:
    """Claim and run batches continuously until no pending jobs remain.

    Runs nextflow synchronously (blocks until complete) before claiming the
    next batch. With --batch-size 10 and 69 samples this yields 7 runs.
    """

    async def _fetch(cfg: ClientConfig, limit: int) -> object:
        async with JobClient(cfg) as client:
            return await client.fetch_next_batch(limit=limit)

    async def _report(cfg: ClientConfig, run_name: str, sample_ids: list[str], pid: str | None) -> None:
        async with JobClient(cfg) as client:
            await client.report_submitted(run_name=run_name, sample_ids=sample_ids, executor_job_id=pid)

    cfg = ClientConfig.from_yaml(config)
    run_number = 0

    typer.echo(f"Daemon started — batch_size={batch_size}")

    while True:
        batch = asyncio.run(_fetch(cfg, batch_size))

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

        cmd = build_nextflow_command(batch=batch, weblog_url=cfg.weblog_url)

        # Report submitted before blocking on nextflow — transitions claimed→submitted
        # while the run is still in claimed state. If we called this after subprocess.run,
        # the synchronous nextflow execution would have already sent the weblog completed
        # event, moving the run to completed before we report submitted.
        try:
            asyncio.run(_report(cfg, batch.run_name, sample_ids, None))
        except Exception as e:
            typer.echo(f"  WARN: failed to report submitted: {e}", err=True)

        # Run synchronously so we know when nextflow finishes before claiming more.
        result = subprocess.run(cmd, capture_output=False)
        exit_code = result.returncode
        typer.echo(f"[run {run_number}] nextflow exited with code {exit_code}")

    typer.echo(f"\nDaemon finished after {run_number} run(s).")
