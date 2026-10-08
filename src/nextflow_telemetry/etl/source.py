"""Reads published outputs from object storage via rclone.

We never LIST — every path is reconstructed from ``(workflow_id, version,
sample_key)``. Outputs publish to ``<base>/<workflow_id>/<version>/<sample_key>/``
(ADR-0008, #220). The default base is R2 ``r2:cmgd-raw`` (``ETL_SOURCE_BASE``
overrides it); ``cmgd_nextflow 2.2.1`` also has a legacy GCS base, tried after
R2. rclone reuses the ``r2:`` and ``gs1:`` remotes already configured on
onclappc02, so there's no new credential wiring.
"""
from __future__ import annotations

import os
import subprocess

SOURCE_BASE = os.environ.get("ETL_SOURCE_BASE", "r2:cmgd-raw")
# Pre-R2 publish bases, tried in order after SOURCE_BASE (ADR-0008: GCS gets no
# new writes and is deleted once the v2 re-run replaces it).
LEGACY_BASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("cmgd_nextflow", "2.2.1"): ("gs1:cmgd-data/results/cMDv4",),
}


def _exists(path: str) -> bool:
    r = subprocess.run(["rclone", "lsf", path], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _cat(path: str) -> bytes | None:
    r = subprocess.run(["rclone", "cat", path], capture_output=True)
    return r.stdout if r.returncode == 0 and r.stdout else None


def locate(workflow_id: str, version: str, sample_key: str) -> str | None:
    """The sample's publish prefix, or None if it isn't published yet.

    MARK_COMPLETE is the last object the pipeline writes, so its presence means
    the full output set is durably there. One stat per base, never a LIST."""
    for base in (SOURCE_BASE, *LEGACY_BASES.get((workflow_id, version), ())):
        prefix = f"{base}/{workflow_id}/{version}/{sample_key}"
        if _exists(f"{prefix}/MARK_COMPLETE"):
            return prefix
    return None


def fetch(prefix: str, subpath: str) -> bytes | None:
    """Fetch one object's bytes; None if it's absent (a tolerated skipped branch/step)."""
    return _cat(f"{prefix}/{subpath}")
