"""Configuration model for nf_client.

A YAML file is the canonical config source, loaded via ClientConfig.from_yaml().

Workflow details (repository, revision, profile) are no longer specified here —
they come from the server's dispatch response. The config only describes
how this client connects to the server and how it submits jobs locally.

Example config file (client-hpc.yaml):

    server_url: "http://telemetry.example.com"
    weblog_url: "http://telemetry.example.com/telemetry"

    dispatch:
      batch_size: 200
      # Optional: pin this client to a specific workflow/version queue
      workflow_id: "curatedMetagenomics"
      workflow_version: "1.0.0"

    submission:
      mode: "slurm"          # local | slurm | pbs | lsf
      template_path: "templates/slurm.sh.j2"

      # Default template variables — overridden per-job if needed
      defaults:
        walltime: "48:00:00"
        memory: "16G"
        cpus: 4
        log_dir: "logs"
        outdir: "results"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class DispatchConfig(BaseModel):
    batch_size: int = Field(default=50, ge=1, le=500)
    # Optional filters: if set, this client only pulls jobs for this workflow
    workflow_id: str | None = None
    workflow_version: str | None = None


class SubmissionConfig(BaseModel):
    mode: Literal["local", "slurm", "pbs", "lsf"] = "local"
    template_path: Path | None = None
    max_concurrent_runs: int | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class ClientConfig(BaseModel):
    server_url: str
    weblog_url: str
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ClientConfig":
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)
