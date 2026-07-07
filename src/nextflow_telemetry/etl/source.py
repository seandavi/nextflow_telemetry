"""Reads published outputs from object storage via rclone.

We never LIST — every path is reconstructed from ``(workflow, version, sample_id)``.
rclone reuses the already-configured ``gs1:`` remote on onclappc02, so there's no
new credential wiring. ``ETL_SOURCE_BASE`` overrides the remote+prefix (e.g. for a
test double); default is the cMDv4 GCS publish base.
"""
from __future__ import annotations

import os
import subprocess

# rclone remote + publish_base_dir. The workflow derives <base>/<name>/<version>/<sample>.
SOURCE_BASE = os.environ.get("ETL_SOURCE_BASE", "gs1:cmgd-data/results/cMDv4")


def sample_prefix(workflow: str, version: str, sample_id: str) -> str:
    return f"{SOURCE_BASE}/{workflow}/{version}/{sample_id}"


def is_published(workflow: str, version: str, sample_id: str) -> bool:
    """MARK_COMPLETE existence gate — the sentinel is the last object written, so
    its presence means the full output set is durably there. Cheap stat, never LIST."""
    path = f"{sample_prefix(workflow, version, sample_id)}/MARK_COMPLETE"
    r = subprocess.run(["rclone", "lsf", path], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def fetch(prefix: str, subpath: str) -> bytes | None:
    """Fetch one object's bytes; None if it's absent (a tolerated skipped branch/step)."""
    r = subprocess.run(["rclone", "cat", f"{prefix}/{subpath}"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout
