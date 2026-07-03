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
        # httpx resolves relative paths against base_url per RFC 3986: without a
        # trailing slash, the last segment of base_url is replaced. Force a trailing
        # slash so callers can pass `.../api` or `.../api/` interchangeably.
        base_url = self._config.server_url.rstrip("/") + "/"
        headers = {}
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30, headers=headers)
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
            payload["workflow_id"] = self._config.dispatch.workflow_id  # list[str]
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

    async def get_stats(self) -> dict:
        """Return the server's summary stats payload (samples, workflows, jobs/runs by status, DLQ)."""
        response = await self._client.get("admin/stats")
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Operator / CI methods (require a bearer token for the mutating ones)
    # ------------------------------------------------------------------

    async def create_submission(self, accession: str, *, dry_run: bool = False) -> dict:
        """Register a study/BioProject by accession (or preview it with dry_run)."""
        response = await self._client.post(
            "submissions", json={"accession": accession, "dry_run": dry_run}
        )
        response.raise_for_status()
        return response.json()

    async def get_submission(self, submission_id: str) -> dict:
        """Fetch a submission record by id (provenance receipt)."""
        response = await self._client.get(f"submissions/{submission_id}")
        response.raise_for_status()
        return response.json()

    async def reconcile(self) -> dict:
        """Create pending jobs for the samples × active workflows cross-product."""
        response = await self._client.post("admin/reconcile-jobs")
        response.raise_for_status()
        return response.json()

    async def requeue_dead_letter(self) -> dict:
        """Requeue dead-letter jobs back to pending."""
        response = await self._client.post("admin/requeue-dead-letter")
        response.raise_for_status()
        return response.json()

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
