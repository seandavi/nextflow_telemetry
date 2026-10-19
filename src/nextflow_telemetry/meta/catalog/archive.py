"""Whole-file snapshots in a private, content-addressed archive.

The root is a local directory or an rclone path (``r2:cmgd-private/metadata-sources``,
via the repo's existing rclone remotes). Blobs are keyed ``sha256/<digest>`` either way.

No downloader or file-type interpretation. A caller supplies bytes it is entitled
 to retain. Placement is atomic and never overwrites an existing blob.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class Artifact:
    sha256: str
    size_bytes: int
    uri: str


def _copy_hash(src: IO[bytes], dest: IO[bytes] | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: src.read(1 << 20), b""):
        digest.update(block)
        size += len(block)
        if dest is not None:
            dest.write(block)
    return digest.hexdigest(), size


def is_remote(root: str | Path) -> bool:
    """``remote:path`` (rclone) vs a filesystem path. Absolute/relative paths never match."""
    r = str(root)
    return ":" in r.split("/", 1)[0] and not r.startswith(("file:", "/", "."))


def _rclone(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *args], capture_output=True)


def _remote_hash(uri: str) -> tuple[str, int]:
    """Stream the stored object and hash it: R2's own checksums are not sha256."""
    with subprocess.Popen(["rclone", "cat", uri], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as p:
        assert p.stdout is not None
        got = _copy_hash(p.stdout)
        if p.wait() != 0:
            raise ValueError(f"cannot read retained snapshot: {uri}")
    return got


def inspect_file(path: Path, root: str | Path) -> Artifact:
    with path.open("rb") as f:
        sha, size = _copy_hash(f)
    if is_remote(root):
        return Artifact(sha, size, f"{str(root).rstrip('/')}/sha256/{sha}")
    return Artifact(sha, size, (Path(root).resolve() / "sha256" / sha).as_uri())


def verify(uri: str, expected: Artifact) -> None:
    """Re-read a retained blob and require the receipt's bytes (local or remote)."""
    if uri.startswith("file:"):
        got = _local_hash(Path(unquote(urlsplit(uri).path)))
    else:
        got = _remote_hash(uri)
    if got != (expected.sha256, expected.size_bytes):
        raise ValueError(f"retained snapshot checksum mismatch: {uri}")


def fetch(uri: str, dest: Path, expected: Artifact) -> Path:
    """Copy a retained blob to ``dest`` (a new file) and require the receipt's bytes.

    The only way loaders read source bytes: a load can't silently use anything but
    what the catalog says was captured."""
    if uri.startswith("file:"):
        src = Path(unquote(urlsplit(uri).path))
        dest.write_bytes(src.read_bytes())
    elif (r := _rclone("copyto", uri, str(dest))).returncode:
        raise ValueError(f"cannot fetch {uri}: {r.stderr.decode(errors='replace').strip()[-300:]}")
    got = _local_hash(dest)
    if got != (expected.sha256, expected.size_bytes):
        dest.unlink(missing_ok=True)
        raise ValueError(f"retained snapshot checksum mismatch: {uri}")
    return dest


def _local_hash(path: Path) -> tuple[str, int]:
    with path.open("rb") as f:
        return _copy_hash(f)


def _retain_remote(path: Path, root: str, expected: Artifact) -> Artifact:
    target = f"{root.rstrip('/')}/sha256/{expected.sha256}"
    if _rclone("lsf", target).stdout.strip():  # exists: never overwrite, just verify below
        pass
    elif (r := _rclone("copyto", "--immutable", str(path), target)).returncode:
        raise ValueError(f"upload failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
    # Hash what R2 now holds, not what we sent: catches a changed input and bad uploads.
    verify(target, expected)
    return Artifact(expected.sha256, expected.size_bytes, target)


def retain(path: Path, root: str | Path, expected: Artifact) -> Artifact:
    """Retain exactly the inspected bytes. Existing archives must be private.

    Changing input between preview/hash and copy is an error. Reusing a blob
    verifies its bytes, not just its file name. No garbage collection here: an
    orphan after a DB failure is safer than a receipt pointing at missing data.
    """
    if is_remote(root):
        return _retain_remote(path, str(root), expected)
    root = Path(root).resolve()
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
