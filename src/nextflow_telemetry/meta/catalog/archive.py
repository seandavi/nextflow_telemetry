"""Whole-file snapshots in a private, content-addressed local archive.

No downloader or file-type interpretation. A caller supplies bytes it is entitled
 to retain. Placement is atomic and never overwrites an existing blob.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class Artifact:
    sha256: str
    size_bytes: int
    uri: str


def _copy_hash(src: BinaryIO, dest: BinaryIO | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: src.read(1 << 20), b""):
        digest.update(block)
        size += len(block)
        if dest is not None:
            dest.write(block)
    return digest.hexdigest(), size


def inspect_file(path: Path, root: Path) -> Artifact:
    with path.open("rb") as f:
        sha, size = _copy_hash(f)
    return Artifact(sha, size, (root.resolve() / "sha256" / sha).as_uri())


def retain(path: Path, root: Path, expected: Artifact) -> Artifact:
    """Retain exactly the inspected bytes. Existing archives must be private.

    Changing input between preview/hash and copy is an error. Reusing a blob
    verifies its bytes, not just its file name. No garbage collection here: an
    orphan after a DB failure is safer than a receipt pointing at missing data.
    """
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("archive directory must be private (chmod 700)")
    blobs = root / "sha256"
    if blobs.is_symlink():
        raise ValueError("archive blob directory must not be a symlink")
    blobs.mkdir(mode=0o700, exist_ok=True)
    if blobs.stat().st_mode & 0o077:
        raise ValueError("archive blob directory must be private (chmod 700)")
    target = blobs / expected.sha256
    fd, name = tempfile.mkstemp(prefix=".capture-", dir=blobs)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as inp:
            sha, size = _copy_hash(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if (sha, size) != (expected.sha256, expected.size_bytes):
            raise ValueError("input changed during capture; retry with a stable file")
        try:
            os.link(tmp, target)  # atomic create-if-absent; mkstemp gives mode 600
        except FileExistsError:
            pass
        if target.is_symlink() or not target.is_file():
            raise ValueError("archive blob must be a regular file, not a symlink")
        if target.stat().st_mode & 0o077:
            raise ValueError("archive blob must be private (chmod 600)")
        with target.open("rb") as inp:
            actual = _copy_hash(inp)
        if actual != (sha, size):
            raise ValueError(f"archive blob checksum mismatch: {target}")
        return Artifact(sha, size, target.as_uri())
    finally:
        tmp.unlink(missing_ok=True)
