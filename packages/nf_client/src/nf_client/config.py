"""Configuration model for nf_client.

A YAML file is the canonical config source, loaded via ClientConfig.from_yaml().

Workflow details (repository, revision) come from the server's dispatch response.
The profile is execution-environment-specific and lives here in the client config
so the same workflow definition can run on different HPC systems (e.g. anvil vs alpine).

String values may reference environment variables as ``${NAME}``; they are
expanded at load time, so one YAML can serve every cluster that exports the
same ``NF_TEL_*`` variables (docs/hpc-layout.md). Bare ``$NAME`` is left alone
because some defaults are shell snippets expanded later inside the job.

See packages/nf_client/client-example.yaml for a fully annotated reference config.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, where: str = "") -> Any:
    """Replace ``${NAME}`` in every string of a loaded YAML tree.

    An unset variable is an error, not an empty string: a silently blank
    account or store path would submit jobs that charge or write the wrong place.
    """
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{where}.{k}" if where else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ValueError(f"config {where}: environment variable {name} is not set")
            return os.environ[name]
        return _ENV_REF.sub(sub, value)
    return value



def _redact_defaults(d: dict) -> dict:
    """Strip secrets from a config dict before it is reported to the server.

    `token` is the bearer token itself; submission.defaults may hold credential paths.
    GET /daemons is readable without auth, so anything left here is public.
    """
    out = {k: v for k, v in d.items() if k != "token"}
    if "submission" in out:
        out["submission"] = {k: v for k, v in out["submission"].items() if k != "defaults"}
    return out


class DispatchConfig(BaseModel):
    batch_size: int = Field(default=50, ge=1, le=500)
    # Optional filters: if set, this client only pulls jobs for these workflows.
    # Accepts a single string or a list. Omit (or set to null) to claim any workflow.
    workflow_id: list[str] | None = None
    workflow_version: str | None = None

    @field_validator("workflow_id", mode="before")
    @classmethod
    def _coerce_workflow_id(cls, v: object) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x) for x in v]
        raise ValueError(f"workflow_id must be a string or list of strings, got {type(v)}")


class SubmissionConfig(BaseModel):
    mode: Literal["local", "slurm", "pbs", "lsf"] = "local"
    template_path: Path | None = None
    max_concurrent_runs: int | None = None
    slurm_export_none: bool = True
    defaults: dict[str, Any] = Field(default_factory=dict)


class ClientConfig(BaseModel):
    server_url: str
    # Only the daemon/run-wrapper needs a weblog sink; operator/CI commands
    # (study submissions, reconcile, introspection) don't, so it's optional.
    weblog_url: str = ""
    # Bearer token for operator/CI mutating endpoints (POST /submissions, …).
    # Usually supplied via the NF_OPERATOR_TOKEN env var rather than YAML;
    # the env var overrides this field. Empty ⇒ no Authorization header sent.
    token: str | None = None
    profile: str = Field(default="standard", description="Nextflow profile passed as -profile to nextflow run. HPC-specific (e.g. 'anvil', 'alpine').")
    continuous: bool = Field(default=False, description="Keep daemon running when queue is empty, polling for new jobs.")
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ClientConfig":
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(_expand_env(raw))

    def sanitized_config_yaml(self) -> str:
        """Return config as YAML with submission.defaults stripped (may contain credential paths)."""
        d = self.model_dump(mode="json")
        return yaml.dump(_redact_defaults(d), default_flow_style=False, sort_keys=False)
