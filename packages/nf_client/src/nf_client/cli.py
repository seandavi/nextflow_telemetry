"""nf-client CLI — thin wrapper around the protocol library.

This CLI is intentionally minimal: it handles one claim+submit cycle.
Looping, resource-awareness, and scheduling are left to the operator
(cron, Airflow, site-specific daemon).

Usage:
    nf-client submit --config client-hpc.yaml [--dry-run]
    nf-client fetch  --config client-hpc.yaml
"""
from __future__ import annotations

import asyncio
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
