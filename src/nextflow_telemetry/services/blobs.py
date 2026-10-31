"""The object store for task logs and run logs, under the Worker's keys (ADR-0013 #373).

The edge Worker writes these keys to R2; the origin reads them, and writes them too for
events posted straight to it.
"""
from __future__ import annotations

from datetime import datetime

import obstore
from obstore.store import LocalStore, ObjectStore, S3Store

NEXTFLOW_LOG_KEY = "nextflow-logs/{run_name}/nextflow.log"
WRAPPER_LOG_KEY = "nextflow-logs/{run_name}/wrapper_output.log"
TASK_LOG_KEY = "task-logs/{run_name}/{task_hash}/{log_type}"


def blob_store(url: str, *, r2_account_id: str, r2_access_key_id: str, r2_secret_access_key: str) -> ObjectStore:
    """``r2://<bucket>`` is Cloudflare R2; anything else is a local directory (tests, contract target).

    An empty ``url`` is an error: there is no default bucket, so nothing writes to production by accident.
    """
    if not url:
        raise RuntimeError("BLOB_STORE_URL is not set: use r2://<bucket> or a local directory")
    if url.startswith("r2://"):
        return S3Store(
            url.removeprefix("r2://"),
            endpoint=f"https://{r2_account_id}.r2.cloudflarestorage.com",
            access_key_id=r2_access_key_id,
            secret_access_key=r2_secret_access_key,
            region="auto",
        )
    return LocalStore(url.removeprefix("file://"), mkdir=True)


async def put_text(store: ObjectStore, key: str, data: bytes) -> None:
    await obstore.put_async(store, key, data)


async def list_keys(store: ObjectStore, prefix: str) -> list[tuple[str, datetime]]:
    """``(path, last_modified)`` under ``prefix``, sorted by path."""
    found: list[tuple[str, datetime]] = []
    async for batch in obstore.list(store, prefix=prefix):
        found.extend((obj["path"], obj["last_modified"]) for obj in batch)
    return sorted(found)


async def get_text(store: ObjectStore, key: str) -> str | None:
    try:
        result = await obstore.get_async(store, key)
    except FileNotFoundError:
        return None
    return bytes(await result.bytes_async()).decode("utf-8", "replace")
