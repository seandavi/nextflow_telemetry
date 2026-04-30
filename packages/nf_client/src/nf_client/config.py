"""Configuration model for nf_client.

A YAML file is the canonical config source, loaded via ClientConfig.from_yaml().
Environment variables can override individual fields via pydantic-settings.

Example config file (client-hpc.yaml):

    server_url: "http://telemetry.example.com"
    weblog_url: "http://telemetry.example.com/telemetry"

    workflow:
      id: "curatedMetagenomics"
      version: "1.0.0"
      repository: "https://github.com/org/curatedMetagenomicsNextflow"
      revision: "main"
      profile: "slurm"

    dispatch:
      batch_size: 200
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


class WorkflowConfig(BaseModel):
    id: str
    version: str
    repository: str
    revision: str = "main"
    profile: str = "local"


class DispatchConfig(BaseModel):
    batch_size: int = Field(default=50, ge=1, le=500)


class SubmissionConfig(BaseModel):
    mode: Literal["local", "slurm", "pbs", "lsf"] = "local"
    template_path: Path | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class ClientConfig(BaseModel):
    server_url: str
    weblog_url: str
    workflow: WorkflowConfig
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ClientConfig":
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)
