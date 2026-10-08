"""Reads from the v2 control plane (the Cloudflare Worker) that the historical
tier needs: run → workflow attribution, collection membership, and which
workflow versions are active. All GETs; the catalog never writes to v2 here.
"""
from __future__ import annotations

import httpx

_TIMEOUT = 30.0
_PAGE = 500


def run_workflows(base_url: str, run_names: set[str]) -> dict[str, tuple[str, str]]:
    """``run_name → (workflow_id, workflow_version)`` for the runs v2 knows.

    Pages newest-first through ``GET /runs`` until every requested run is found
    or the list ends.
    """
    found: dict[str, tuple[str, str]] = {}
    offset = 0
    with httpx.Client(base_url=base_url, timeout=_TIMEOUT) as client:
        while len(found) < len(run_names):
            resp = client.get("/runs", params={"limit": _PAGE, "offset": offset})
            resp.raise_for_status()
            runs = resp.json()["runs"]
            for r in runs:
                if r["run_name"] in run_names:
                    found[r["run_name"]] = (r["workflow_id"], r["workflow_version"])
            if len(runs) < _PAGE:
                break
            offset += _PAGE
    return found


def collection_samples(base_url: str, collection_id: str) -> list[str] | None:
    """Sample ids in a collection, or None when v2 has no such collection."""
    with httpx.Client(base_url=base_url, timeout=_TIMEOUT) as client:
        resp = client.get("/cohorts")
        resp.raise_for_status()
        if not any(c["collection_id"] == collection_id for c in resp.json()):
            return None
        samples: list[str] = []
        while True:
            resp = client.get(
                "/samples",
                params={"collection": collection_id, "limit": 1000, "offset": len(samples)},
            )
            resp.raise_for_status()
            items = resp.json()["items"]
            samples.extend(s["sample_id"] for s in items)
            if len(items) < 1000:
                return samples


def active_workflows(base_url: str) -> list[tuple[str, str]]:
    """``(workflow_id, version)`` of every active workflow version."""
    resp = httpx.get(f"{base_url}/workflows", params={"status": "active"}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return [(w["workflow_id"], w["version"]) for w in resp.json()]
