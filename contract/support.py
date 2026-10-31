import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, TypeAdapter

from contract import schemas

T = TypeVar("T")

PRODUCTION_HOSTS: frozenset[str] = frozenset(
    {"nf-telemetry.seandavi.workers.dev", "nf-telemetry.cancerdatasci.org"}
)


@dataclass(frozen=True)
class Target:
    base_url: str
    event_url: str
    token: str | None
    allow_writes: bool
    fast_clock_seconds: float | None
    event_lag_seconds: float


def target_from_env(environ: Mapping[str, str]) -> Target:
    base_url = environ.get("CONTRACT_BASE_URL", "").rstrip("/")
    if not base_url:
        raise ValueError("CONTRACT_BASE_URL is required, e.g. http://127.0.0.1:8787")
    event_url = environ.get("CONTRACT_EVENT_URL", "").rstrip("/") or base_url
    allow_writes = environ.get("CONTRACT_ALLOW_WRITES") == "1"
    if allow_writes:
        for url in (base_url, event_url):
            host = urlsplit(url).hostname
            if host in PRODUCTION_HOSTS:
                raise ValueError(f"refusing writes against production host {host}: unset CONTRACT_ALLOW_WRITES")
    fast_clock = environ.get("CONTRACT_FAST_CLOCK_SECONDS")
    return Target(
        base_url=base_url,
        event_url=event_url,
        token=environ.get("CONTRACT_TOKEN") or None,
        allow_writes=allow_writes,
        fast_clock_seconds=float(fast_clock) if fast_clock else None,
        event_lag_seconds=float(environ.get("CONTRACT_EVENT_LAG_SECONDS") or 5),
    )


def eventually(check: Callable[[], T], timeout: float, interval: float = 0.25) -> T:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)


def validate(model: type[BaseModel] | TypeAdapter[Any], resp: httpx.Response, status: int = 200) -> Any:
    assert resp.status_code == status, (
        f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text[:500]}"
    )
    if isinstance(model, TypeAdapter):
        return model.validate_python(resp.json())
    return model.model_validate(resp.json())


def new_srr() -> str:
    return f"SRR{secrets.randbelow(9 * 10**8) + 10**8}"


def md5_sample_id(runs: str) -> str:
    return hashlib.md5(";".join(sorted(runs.split(";"))).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def register_workflow(api: httpx.Client, workflow_id: str, **fields: Any) -> dict[str, Any]:
    body = {
        "workflow_id": workflow_id,
        "version": "1.0.0",
        "repository_url": "https://example.invalid/contract",
        "revision": "main",
        "max_retries": 1,
    } | fields
    return validate(schemas.Workflow, api.post("workflows", json=body), 201).model_dump()


def register_sample(api: httpx.Client, collection: str | None, runs: str | None = None, **fields: Any) -> dict[str, Any]:
    runs = runs or new_srr()
    body: dict[str, Any] = {"sample_id": md5_sample_id(runs), "ncbi_accession": runs} | fields
    if collection is not None:
        body["collection"] = collection
    return validate(schemas.Sample, api.post("samples", json=body), 201).model_dump()


def registration(api: httpx.Client, name: str, samples: int = 1, **fields: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A workflow scoped to its own collection `name`, with `samples` fresh samples and jobs reconciled."""
    workflow = register_workflow(api, name, collections=[name], **fields)
    registered = [register_sample(api, name) for _ in range(samples)]
    reconcile(api)
    return workflow, registered


def reconcile(api: httpx.Client) -> int:
    return validate(schemas.Reconciled, api.post("admin/reconcile-jobs", json={})).jobs_created


def claim(api: httpx.Client, workflow_id: str | list[str], limit: int = 10) -> dict[str, Any] | None:
    resp = api.post("dispatch/batch", json={"limit": limit, "workflow_id": workflow_id})
    if resp.status_code == 204:
        return None
    return validate(schemas.ClaimedBatch, resp).model_dump()


def submitted(api: httpx.Client, run_name: str, sample_ids: list[str]) -> None:
    body = {"run_name": run_name, "executor_job_id": "contract-1", "sample_ids": sample_ids}
    validate(schemas.Submitted, api.post("dispatch/submitted", json=body))


def weblog(edge: httpx.Client, run_name: str, event: str, **extra: Any) -> None:
    body = {"runId": "contract-nf-session", "runName": run_name, "event": event, "utcTime": _now()} | extra
    resp = edge.post("telemetry", json=body)
    assert resp.status_code == 200, f"POST telemetry {event}: {resp.status_code} {resp.text[:500]}"


def start_run(api: httpx.Client, edge: httpx.Client, workflow_id: str, limit: int = 10) -> dict[str, Any]:
    """Claim, confirm submitted and send the weblog `started` event; returns the claimed batch."""
    batch = claim(api, workflow_id, limit)
    assert batch is not None, f"nothing to claim for {workflow_id}"
    submitted(api, batch["run_name"], [j["sample_id"] for j in batch["jobs"]])
    weblog(edge, batch["run_name"], "started")
    return batch


def mark_complete(edge: httpx.Client, run_name: str, tag: str) -> None:
    trace = {"tag": tag, "process": "cmgd:MARK_COMPLETE", "status": "COMPLETED", "hash": "aa/bbbbbb"}
    weblog(edge, run_name, "process_completed", trace=trace)


def run_event(
    edge: httpx.Client,
    run_name: str,
    event: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> httpx.Response:
    data = {"event": json.dumps({"utc_time": _now()} | event)}
    return edge.post(f"api/runs/{run_name}/event", data=data, files=files)


def job_summary(api: httpx.Client, pk: int) -> dict[str, Any]:
    return validate(schemas.JobSummary, api.get(f"workflows/{pk}/job-summary")).model_dump()


def run_detail(api: httpx.Client, run_name: str) -> dict[str, Any]:
    return validate(schemas.RunDetail, api.get(f"runs/{run_name}")).model_dump()
