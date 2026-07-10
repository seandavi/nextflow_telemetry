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
import json as _json
import os
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
from .srr import derive_sample_id
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

# Operator/CI commands don't need the full daemon YAML — server URL and token can
# come from flags/env. Config is optional here (contrast with config_option above).
opt_config = typer.Option(None, "--config", "-c", help="Path to client YAML config (optional for operator commands).")
opt_server = typer.Option(None, "--server", help="Telemetry API base URL. Overrides config; falls back to $NF_TELEMETRY_URL.")
json_option = typer.Option(False, "--json", help="Emit raw JSON (for scripts/CI) instead of human text.")

ENV_SERVER = "NF_TELEMETRY_URL"
ENV_TOKEN = "NF_OPERATOR_TOKEN"


def _operator_config(config: Path | None, server: str | None) -> ClientConfig:
    """Resolve a ClientConfig for an operator/CI command.

    Server URL: --server > $NF_TELEMETRY_URL > config.server_url.
    Token:      $NF_OPERATOR_TOKEN > config.token  (env overrides YAML, so most
    uses need no config file at all — just the two env vars).
    """
    if config is not None:
        cfg = ClientConfig.from_yaml(config)
    else:
        url = server or os.environ.get(ENV_SERVER)
        if not url:
            raise typer.BadParameter(
                f"Provide --server, set ${ENV_SERVER}, or pass --config with a server_url."
            )
        cfg = ClientConfig(server_url=url)
    if server:
        cfg = cfg.model_copy(update={"server_url": server})
    token = os.environ.get(ENV_TOKEN) or cfg.token
    return cfg.model_copy(update={"token": token})


def _run_operator(coro):
    """Run an operator coroutine, mapping HTTP errors to clean stderr + exit 1 (CI-friendly)."""
    import httpx

    try:
        return asyncio.run(coro)
    except httpx.HTTPStatusError as e:
        detail = e.response.text
        try:
            detail = e.response.json().get("detail", detail)
        except Exception:
            pass
        typer.echo(f"error: {e.response.status_code} {detail}", err=True)
        raise typer.Exit(1)
    except httpx.HTTPError as e:
        typer.echo(f"error: request failed: {e}", err=True)
        raise typer.Exit(1)


def _emit(payload: dict, as_json: bool, human) -> None:
    """Print payload as JSON or via the human(payload) formatter."""
    if as_json:
        typer.echo(_json.dumps(payload, default=str))
    else:
        human(payload)


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
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Print a summary of system state: samples, workflows, jobs/runs by status, DLQ."""

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            return await client.get_stats()

    payload = _run_operator(_run())

    def human(p: dict) -> None:
        typer.echo(f"samples:   {p['samples']}")
        typer.echo(f"workflows: {p['workflows']}")
        typer.echo(f"dead-letter (unresolved): {p['dead_letter_unresolved']}")
        typer.echo("\njobs by status:")
        for status, count in sorted(p["jobs_by_status"].items()) or []:
            typer.echo(f"  {status:<10} {count}")
        if not p["jobs_by_status"]:
            typer.echo("  (none)")
        typer.echo("\nruns by status:")
        for status, count in sorted(p["runs_by_status"].items()) or []:
            typer.echo(f"  {status:<10} {count}")
        if not p["runs_by_status"]:
            typer.echo("  (none)")

    _emit(payload, as_json, human)


@app.command(name="submit-study")
def submit_study(
    accession: str = typer.Argument(..., help="Study/BioProject accession (PRJNA…, SRP…, ERP…, DRP…)."),
    config: Path = opt_config,
    server: str = opt_server,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview counts without registering anything."),
    reconcile: bool = typer.Option(False, "--reconcile", help="After a real submit, create pending jobs (POST /admin/reconcile-jobs)."),
    as_json: bool = json_option,
) -> None:
    """Register a study/BioProject's samples by accession (mints a submission_id).

    Requires an operator token: set $NF_OPERATOR_TOKEN (or token: in --config).
    Registration only unless --reconcile is passed.
    """
    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            receipt = await client.create_submission(accession, dry_run=dry_run)
            if reconcile and not dry_run:
                receipt["reconcile"] = await client.reconcile()
            return receipt

    payload = _run_operator(_run())

    def human(p: dict) -> None:
        typer.echo(
            f"{p['status']}: {p['collection_id']} "
            f"({p.get('source')}/{p.get('type')}) — "
            f"found {p['samples_found']}, added {p['samples_added']}, existing {p['samples_existing']}"
        )
        if p.get("submission_id"):
            typer.echo(f"submission_id: {p['submission_id']}")
        if "reconcile" in p:
            typer.echo(f"reconcile: {p['reconcile']}")

    _emit(payload, as_json, human)


@app.command()
def submission(
    submission_id: str = typer.Argument(..., help="Submission id returned by submit-study."),
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Look up a submission record by id (provenance receipt)."""

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            return await client.get_submission(submission_id)

    payload = _run_operator(_run())

    def human(p: dict) -> None:
        for k in ("submission_id", "method", "accession", "collection_id", "submitted_by",
                  "status", "samples_found", "samples_added", "samples_existing", "created_at", "error"):
            if p.get(k) is not None:
                typer.echo(f"{k}: {p[k]}")

    _emit(payload, as_json, human)


# Default raw base for curatedMetagenomicData curation TSVs (waldronlab).
_CMD_CURATION_BASE = (
    "https://raw.githubusercontent.com/waldronlab/"
    "curatedMetagenomicDataCuration/master/inst/curated"
)


async def _ingest_rows(
    client: JobClient,
    rows: list[dict],
    *,
    collection: str | None,
    limit: int | None,
) -> dict:
    """Register curation-TSV rows as samples, attaching each to a collection.

    Each row needs an ``ncbi_accession`` column. ``collection`` (if given) is
    used for every row; otherwise the row's ``study_name`` column names the
    collection. Rows without a real run accession are skipped (curation TSVs
    carry placeholders like "Not applicable"). Membership goes through the
    server's ``collection`` field — never ``metadata.cohort`` (retired).
    """
    registered = skipped = 0
    for row in rows:
        if limit is not None and registered >= limit:
            break
        acc = (row.get("ncbi_accession") or "").strip()
        sample_id = derive_sample_id(acc)
        if sample_id is None:
            skipped += 1
            continue
        coll = collection or (row.get("study_name") or "").strip() or None
        await client.register_sample(sample_id, ncbi_accession=acc, collection=coll)
        registered += 1
    return {"registered": registered, "skipped": skipped}


@app.command(name="add-samples")
def add_samples(
    tsv: Path = typer.Option(..., "--tsv", help="Path to a sample TSV (columns: ncbi_accession, study_name)."),
    collection: str = typer.Option(None, "--collection", help="Collection to attach every sample to. If omitted, each row's study_name column is used."),
    limit: int = typer.Option(None, "--limit", help="Register at most this many samples."),
    reconcile_after: bool = typer.Option(False, "--reconcile", help="After registering, create pending jobs (POST /admin/reconcile-jobs)."),
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Register samples from a curation TSV into a collection (membership, not metadata.cohort).

    Requires an operator token for the mutating calls: set $NF_OPERATOR_TOKEN
    (or token: in --config).
    """
    import csv

    with tsv.open(newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            result = await _ingest_rows(client, rows, collection=collection, limit=limit)
            if reconcile_after:
                result["reconcile"] = await client.reconcile()
            return result

    payload = _run_operator(_run())
    payload["source"] = tsv.name

    def human(p: dict) -> None:
        typer.echo(f"{p['source']}: registered {p['registered']}, skipped {p['skipped']} (no run accession)")
        if "reconcile" in p:
            typer.echo(f"reconcile: {p['reconcile'].get('jobs_created')} pending jobs created")

    _emit(payload, as_json, human)


@app.command(name="add-cmd")
def add_cmd(
    study: list[str] = typer.Option(..., "--study", help="curatedMetagenomicData study name (repeatable), e.g. WirbelJ_2018."),
    limit: int = typer.Option(25, "--limit", help="Max samples per study."),
    base: str = typer.Option(_CMD_CURATION_BASE, "--base", help="Raw base URL for curated <study>_sample.tsv files."),
    reconcile_after: bool = typer.Option(False, "--reconcile", help="After registering, create pending jobs."),
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Register samples from curatedMetagenomicData studies (collection = study name).

    Fetches each study's `<study>_sample.tsv` from the waldronlab curation repo.
    Requires an operator token (see add-samples).
    """
    import csv
    import io
    import urllib.request

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        per_study: dict = {}
        total = 0
        async with JobClient(cfg) as client:
            for s in study:
                try:
                    raw = urllib.request.urlopen(f"{base}/{s}/{s}_sample.tsv", timeout=30).read().decode()
                except Exception as e:  # network / 404 — record and continue to next study
                    per_study[s] = {"error": str(e)}
                    continue
                rows = list(csv.DictReader(io.StringIO(raw), delimiter="\t"))
                r = await _ingest_rows(client, rows, collection=s, limit=limit)
                per_study[s] = r
                total += r["registered"]
            out: dict = {"studies": per_study, "total_registered": total}
            if reconcile_after:
                out["reconcile"] = await client.reconcile()
            return out

    payload = _run_operator(_run())

    def human(p: dict) -> None:
        for s, r in p["studies"].items():
            if "error" in r:
                typer.echo(f"  {s:24s} FETCH FAILED: {r['error']}")
            else:
                typer.echo(f"  {s:24s} registered {r['registered']}, skipped {r['skipped']}")
        typer.echo(f"TOTAL registered {p['total_registered']}")
        if "reconcile" in p:
            typer.echo(f"reconcile: {p['reconcile'].get('jobs_created')} pending jobs created")

    _emit(payload, as_json, human)


@app.command(name="register-workflow")
def register_workflow_cmd(
    workflow_id: str = typer.Option(..., "--id", help="Workflow id, e.g. nf_testing."),
    version: str = typer.Option(..., "--version", help="Workflow version, e.g. 0.1.0."),
    repository_url: str = typer.Option(..., "--repo", help="Repository URL or absolute path to main.nf."),
    revision: str = typer.Option(..., "--revision", help="Git branch/tag/commit (mutable — no rerun on change)."),
    max_retries: int = typer.Option(3, "--max-retries", help="Retries before dead-lettering (0–10)."),
    description: str = typer.Option(None, "--description", help="Free-text description."),
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Register (upsert) a workflow version. Requires an operator token."""
    body: dict = {
        "workflow_id": workflow_id,
        "version": version,
        "repository_url": repository_url,
        "revision": revision,
        "max_retries": max_retries,
    }
    if description:
        body["description"] = description

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            return await client.register_workflow(body)

    payload = _run_operator(_run())

    def human(p: dict) -> None:
        typer.echo(f"workflow: {p.get('workflow_id')} v{p.get('version')} registered (id={p.get('id')})")

    _emit(payload, as_json, human)


@app.command()
def reconcile(
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Create pending jobs for the samples × active-workflows cross-product."""

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            return await client.reconcile()

    payload = _run_operator(_run())
    _emit(payload, as_json, lambda p: typer.echo(_json.dumps(p, default=str)))


@app.command(name="requeue-dlq")
def requeue_dlq(
    config: Path = opt_config,
    server: str = opt_server,
    as_json: bool = json_option,
) -> None:
    """Requeue dead-letter jobs back to pending."""

    async def _run() -> dict:
        cfg = _operator_config(config, server)
        async with JobClient(cfg) as client:
            return await client.requeue_dead_letter()

    payload = _run_operator(_run())
    _emit(payload, as_json, lambda p: typer.echo(_json.dumps(p, default=str)))


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


@app.command(
    name="run-wrapper",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    help=(
        "Wrap a `nextflow run` command with run-lifecycle telemetry "
        "(wrapper_started / heartbeat / wrapper_exited, and .nextflow.log upload). "
        "Put the nextflow command after `--`, e.g.:\n\n"
        "  nf-client run-wrapper --run-name R --telemetry-url URL -- nextflow run repo ..."
    ),
)
def run_wrapper_cmd(
    ctx: typer.Context,
    run_name: str = typer.Option(..., "--run-name", help="Nextflow run name (-name)."),
    telemetry_url: str = typer.Option(..., "--telemetry-url", help="Telemetry API base URL (same as ClientConfig.server_url)."),
    heartbeat_seconds: float | None = typer.Option(None, "--heartbeat-seconds", help="Heartbeat interval in seconds; 0 disables. Omit for the default."),
    nextflow_log: Path | None = typer.Option(None, "--nextflow-log", help="Path to .nextflow.log to upload on exit (default: cwd/.nextflow.log)."),
) -> None:
    """Thin front-end that delegates to run_wrapper.main, so the exec / signal-
    forwarding / stream-capture machinery lives in exactly one place."""
    from . import run_wrapper as _rw

    argv: list[str] = ["--run-name", run_name, "--telemetry-url", telemetry_url]
    if heartbeat_seconds is not None:
        argv += ["--heartbeat-seconds", str(heartbeat_seconds)]
    if nextflow_log is not None:
        argv += ["--nextflow-log", str(nextflow_log)]
    # Everything after the typer options (the nextflow command) lands in
    # ctx.args. Re-insert `--` so argparse treats single-dash nextflow flags
    # (-revision, -name, -profile, ...) as the positional command, not options.
    argv += ["--", *ctx.args]
    raise typer.Exit(_rw.main(argv))
