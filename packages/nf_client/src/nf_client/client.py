"""JobClient — the protocol library for claiming and reporting dispatch jobs.

This class owns only the HTTP conversation with the telemetry server.
It has no opinion about resource availability, concurrency limits, or
scheduler state — those decisions belong to the caller.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .config import ClientConfig
from .models import DispatchBatchResponse, SubmittedRequest


class JobClient:
    """Async-capable HTTP client for the dispatch protocol.

    Can be used as an async context manager or called with explicit open/close.
    """

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._http: httpx.AsyncClient | None = None

    @classmethod
    def from_yaml(cls, path: Path | str) -> "JobClient":
        return cls(ClientConfig.from_yaml(path))

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "JobClient":
        self._http = httpx.AsyncClient(base_url=self._config.server_url, timeout=30)
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("JobClient must be used as an async context manager")
        return self._http

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def fetch_next_batch(self, limit: int | None = None) -> DispatchBatchResponse | None:
        """Claim a batch of pending jobs from the server.

        Returns None if no jobs are available (server returns 204).
        The caller decides whether conditions are right before calling this.
        """
        batch_size = limit or self._config.dispatch.batch_size
        payload = {
            "workflow_id": self._config.workflow.id,
            "workflow_version": self._config.workflow.version,
            "limit": batch_size,
        }
        response = await self._client.post("/dispatch/batch", json=payload)
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return DispatchBatchResponse.model_validate(response.json())

    async def report_submitted(
        self,
        run_name: str,
        sample_ids: list[str],
        executor_job_id: str | None = None,
    ) -> None:
        """Report to the server that the run has been submitted to the executor.

        Call this immediately after a successful sbatch / nextflow run invocation.
        The run_name MUST match the value passed as -name to nextflow run.
        """
        payload = SubmittedRequest(
            run_name=run_name,
            executor_job_id=executor_job_id,
            sample_ids=sample_ids,
        )
        response = await self._client.post(
            "/dispatch/submitted", json=payload.model_dump()
        )
        response.raise_for_status()
