"""Unit tests for the nf_client protocol library."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import respx
import httpx

from nf_client.client import JobClient
from nf_client.config import ClientConfig
from nf_client.models import DispatchBatchResponse, DispatchedJob
from nf_client.submission import generate_run_name, render_submission_script


MINIMAL_CONFIG = {
    "server_url": "http://test.local",
    "weblog_url": "http://test.local/telemetry",
    "dispatch": {
        "batch_size": 50,
        "workflow_id": "curatedMetagenomics",
        "workflow_version": "1.0.0",
    },
}

FULL_BATCH_RESPONSE = {
    "run_name": "test-run-001",
    "workflow_id": "curatedMetagenomics",
    "workflow_version": "1.0.0",
    "workflow_pk": 1,
    "repository_url": "https://github.com/org/pipeline",
    "revision": "main",
    "profile": "test",
    "jobs": [
        {"sample_id": "SRR001", "metadata": {"ncbi_accession": "SRR001"}},
        {"sample_id": "SRR002", "metadata": {"ncbi_accession": "SRR002"}},
    ],
}


@pytest.fixture
def config() -> ClientConfig:
    return ClientConfig.model_validate(MINIMAL_CONFIG)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

def test_config_from_dict(config: ClientConfig):
    assert config.server_url == "http://test.local"
    assert config.dispatch.workflow_id == ["curatedMetagenomics"]  # coerced str → list
    assert config.dispatch.batch_size == 50


def test_config_from_yaml(tmp_path: Path, config: ClientConfig):
    import yaml
    cfg_file = tmp_path / "client.yaml"
    cfg_file.write_text(yaml.safe_dump(MINIMAL_CONFIG))
    loaded = ClientConfig.from_yaml(cfg_file)
    assert loaded.server_url == config.server_url
    assert loaded.dispatch.workflow_version == config.dispatch.workflow_version


# ---------------------------------------------------------------------------
# JobClient protocol tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_next_batch_returns_batch(config: ClientConfig):
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/dispatch/batch").mock(
            return_value=httpx.Response(200, json=FULL_BATCH_RESPONSE)
        )
        async with JobClient(config) as client:
            batch = await client.fetch_next_batch()

    assert batch is not None
    assert batch.run_name == "test-run-001"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].sample_id == "SRR001"
    assert batch.jobs[0].metadata == {"ncbi_accession": "SRR001"}


@pytest.mark.asyncio
async def test_fetch_next_batch_returns_none_on_204(config: ClientConfig):
    with respx.mock(base_url="http://test.local") as mock:
        mock.post("/dispatch/batch").mock(return_value=httpx.Response(204))
        async with JobClient(config) as client:
            batch = await client.fetch_next_batch()

    assert batch is None


@pytest.mark.asyncio
async def test_report_submitted_sends_correct_payload(config: ClientConfig):
    captured = {}

    with respx.mock(base_url="http://test.local") as mock:
        def capture(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_name": "test-run-001", "status": "submitted"})

        mock.post("/dispatch/submitted").mock(side_effect=capture)

        async with JobClient(config) as client:
            await client.report_submitted(
                run_name="test-run-001",
                sample_ids=["SRR001", "SRR002"],
                executor_job_id="12345",
            )

    assert captured["body"]["run_name"] == "test-run-001"
    assert captured["body"]["executor_job_id"] == "12345"
    assert set(captured["body"]["sample_ids"]) == {"SRR001", "SRR002"}


@pytest.mark.asyncio
async def test_client_requires_context_manager(config: ClientConfig):
    client = JobClient(config)
    with pytest.raises(RuntimeError, match="context manager"):
        await client.fetch_next_batch()


# ---------------------------------------------------------------------------
# Submission utils tests
# ---------------------------------------------------------------------------

def test_generate_run_name_is_valid_uuid(config: ClientConfig):
    import uuid
    name = generate_run_name()
    assert name.startswith("r")
    parsed = uuid.UUID(name[1:])  # strip the "r" prefix before parsing
    assert parsed.version == 7


def test_generate_run_name_is_sortable():
    names = [generate_run_name() for _ in range(10)]
    assert names == sorted(names)


def test_render_submission_script(tmp_path: Path):
    template = tmp_path / "submit.sh.j2"
    template.write_text(textwrap.dedent("""\
        #!/bin/bash
        #SBATCH --job-name=nf_{{ run_name[:8] }}
        nextflow run {{ workflow_repository }} -name {{ run_name }}
    """))
    result = render_submission_script(
        template,
        {"run_name": "abcdef1234567890", "workflow_repository": "https://github.com/org/pipeline"},
    )
    assert "#SBATCH --job-name=nf_abcdef12" in result
    assert "nextflow run https://github.com/org/pipeline -name abcdef1234567890" in result


def test_render_submission_script_raises_on_missing_var(tmp_path: Path):
    from jinja2 import UndefinedError
    template = tmp_path / "submit.sh.j2"
    template.write_text("{{ missing_var }}")
    with pytest.raises(UndefinedError):
        render_submission_script(template, {})


# ---------------------------------------------------------------------------
# Daemon resilience: an unreachable API must not crash the loop
# ---------------------------------------------------------------------------

def test_daemon_survives_unreachable_api(tmp_path: Path):
    """A failed batch fetch (server restart / network blip) must be caught and
    retried on the next poll, not propagate and kill the daemon."""
    from nf_client import cli

    cfg_file = tmp_path / "client.yaml"
    cfg_file.write_text(textwrap.dedent("""\
        server_url: http://test.local
        weblog_url: http://test.local/telemetry
        continuous: false
        dispatch:
          batch_size: 2
          workflow_id: wf
        submission:
          mode: local
    """))

    with respx.mock(assert_all_called=False) as mock:
        # Heartbeat unreachable — must be swallowed.
        mock.put("http://test.local/daemons/heartbeat").mock(side_effect=httpx.ConnectError("down"))
        # First fetch errors (API down), second returns 204 (no pending work).
        mock.post("http://test.local/dispatch/batch").mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(204)]
        )
        # Must return cleanly after recovery, not raise.
        cli.daemon(config=cfg_file, batch_size=0, poll_interval=0.0, continuous=False)


def test_run_wrapper_subcommand_propagates_exit_code():
    """`nf-client run-wrapper -- <cmd>` execs the trailing command (single-dash
    flags passed through) and returns its exit code; unreachable telemetry is
    swallowed."""
    from typer.testing import CliRunner
    from nf_client.cli import app

    runner = CliRunner()
    U = "http://127.0.0.1:9/api"   # unreachable → telemetry no-op

    ok = runner.invoke(app, ["run-wrapper", "--run-name", "t", "--telemetry-url", U, "--", "true"])
    assert ok.exit_code == 0

    fail = runner.invoke(app, ["run-wrapper", "--run-name", "t", "--telemetry-url", U, "--", "bash", "-c", "exit 3"])
    assert fail.exit_code == 3
