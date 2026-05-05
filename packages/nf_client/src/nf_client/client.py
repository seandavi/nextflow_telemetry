"""JobClient — the protocol library for claiming and reporting dispatch jobs.

This class owns only the HTTP conversation with the telemetry server.
It has no opinion about resource availability, concurrency limits, or
scheduler state — those decisions belong to the caller.
"""
from __future__ import annotations

from pathlib import Path

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
        Workflow details (repository, revision, profile) are in the response.
        """
        batch_size = limit or self._config.dispatch.batch_size
        payload: dict = {"limit": batch_size}
        if self._config.dispatch.workflow_id:
            payload["workflow_id"] = self._config.dispatch.workflow_id
        if self._config.dispatch.workflow_version:
            payload["workflow_version"] = self._config.dispatch.workflow_version

        response = await self._client.post("dispatch/batch", json=payload)
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return DispatchBatchResponse.model_validate(response.json())

    async def upload_task_log(
        self,
        *,
        run_name: str,
        task_hash: str,
        log_type: str,
        content: str,
    ) -> None:
        """Upload a single task log file (.command.sh or .command.err) to the server.

        Idempotent: re-uploading the same (run_name, task_hash, log_type) replaces
        the previous content.
        """
        response = await self._client.post(
            "task-logs",
            data={"run_name": run_name, "task_hash": task_hash, "log_type": log_type},
            files={"content": ("content", content.encode("utf-8", errors="replace"), "text/plain")},
        )
        response.raise_for_status()

    async def post_heartbeat(self, payload: dict) -> None:
        """Send a heartbeat to the server. Never raises — failure is silently ignored."""
        try:
            await self._client.put("daemons/heartbeat", json=payload, timeout=5)
        except Exception:
            pass

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
            "dispatch/submitted", json=payload.model_dump()
        )
        response.raise_for_status()
