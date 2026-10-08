"""Reads the backlog from the v2 control plane (ADR-0006/0009): registrations
and their completed jobs. Read-only, unauthenticated GETs.

A job's ``sample_key`` is the id its registration dispatched and published
under: the md5 ``sample_id`` for 2.2.x, a readset id (``RS.…``, ADR-0007) for
registrations keyed that way.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

API_URL = os.environ.get("NF_TELEMETRY_URL", "https://nf-telemetry.seandavi.workers.dev").rstrip("/")
PAGE = 1000


@dataclass(frozen=True)
class Registration:
    pk: int
    workflow_id: str
    version: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompletedJob:
    sample_key: str
    completed_at: str | None
    sample_id: str | None  # md5 content address; None when the key is a readset id
    readset_id: str | None
    collections: tuple[str, ...] = ()

    @property
    def study_name(self) -> str | None:
        # ponytail: a sample in several collections gets the first by id; a
        # collection type to prefer studies would make this deterministic by meaning.
        return self.collections[0] if self.collections else None


def job_from_item(item: dict) -> CompletedJob:
    key = item["sample_key"]
    rs = key.startswith("RS.")
    return CompletedJob(
        sample_key=key,
        completed_at=item.get("completed_at"),
        sample_id=item.get("sample_id") or (None if rs else key),
        readset_id=item.get("readset_id") or (key if rs else None),
        collections=tuple(item.get("collections") or ()),
    )


def registrations(client: httpx.Client) -> list[Registration]:
    r = client.get(f"{API_URL}/api/workflows")
    r.raise_for_status()
    return [Registration(w["id"], w["workflow_id"], w["version"], w.get("params") or {})
            for w in r.json()]


def completed_jobs(client: httpx.Client, reg: Registration) -> list[CompletedJob]:
    """Every completed job of one registration, oldest job id first."""
    out: list[CompletedJob] = []
    after = 0
    while True:
        r = client.get(f"{API_URL}/api/workflows/{reg.pk}/jobs",
                       params={"status": "completed", "after": after, "limit": PAGE})
        r.raise_for_status()
        items = r.json()["items"]
        out.extend(job_from_item(i) for i in items)
        if len(items) < PAGE:
            return out
        after = items[-1]["job_id"]
