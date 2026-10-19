"""Catalog seam: actual PostgreSQL, private local archive, no external sources."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.db import metadata_snapshots_tbl, samples_tbl, submissions_tbl
from nextflow_telemetry.meta.catalog import CatalogError, SourceCatalog, SourceManifest
from nextflow_telemetry.meta.catalog import archive
from nextflow_telemetry.meta.catalog.models import Access, AssetRole


def manifest(**source_changes) -> SourceManifest:
    return SourceManifest.model_validate({
        "source": {"source_id": "tracker", "title": "Synthetic private tracker",
                   "kind": "google_sheet", "url": "https://example.org/sheet",
                   "access": "restricted", **source_changes},
        "assets": [{"asset_id": "tab-0", "locator": "gid=0", "role": "inventory"},
                   {"asset_id": "tab-1", "locator": "gid=1", "role": "sample_metadata"}],
    })


def run(db_url, scenario):
    async def go():
        e = create_async_engine(db_url)
        try:
            return await scenario(SourceCatalog(e))
        finally:
            await e.dispose()
    return asyncio.run(go())


def test_registration_preview_idempotence_and_filters(db_url):
    async def scenario(c):
        m = manifest()
        preview = await c.apply(m, actor="tester")
        assert not preview["committed"] and len(preview["changes"]) == 3
        assert await c.list_sources() == []
        applied = await c.apply(m, actor="tester", commit=True)
        assert applied["changes"] == preview["changes"]
        assert (await c.apply(m, actor="someone-else", commit=True))["changes"] == []
        assert len(await c.history("tracker")) == 3
        assert len(await c.list_sources(access=Access.restricted, role=AssetRole.inventory)) == 1
        assert await c.list_sources(access=Access.public) == []
        assert await c.list_sources(role=AssetRole.profiles) == []
        doc = await c.show("tracker")
        assert len(doc["assets"]) == 2 and doc["snapshots"] == []
        # No inference of code/data license or of an asset's access from its parent.
        assert doc["assets"][0]["descriptor"]["access"] == "unknown"
        assert doc["descriptor"]["data_license"] is None
        async with c.engine.connect() as con:
            for table in (samples_tbl, submissions_tbl):
                assert (await con.execute(select(func.count()).select_from(table))).scalar_one() == 0
    run(db_url, scenario)


def test_edits_are_audited_and_omitted_assets_are_preserved(db_url):
    async def scenario(c):
        m = manifest()
        await c.apply(m, actor="original", commit=True)
        old = await c.show("tracker")
        revised = manifest(title="New title", review_status="reviewed")
        revised.assets = revised.assets[:1]
        revised.assets[0].description = "curation work list, not clinical data"
        assert len((await c.apply(revised, actor="reviewer"))["changes"]) == 2
        assert await c.show("tracker") == old
        await c.apply(revised, actor="reviewer", commit=True)
        doc = await c.show("tracker")
        assert len(doc["assets"]) == 2  # no implicit deletion
        hist = await c.history("tracker")
        assert len(hist) == 5
        assert hist[-2]["before"]["title"] == "Synthetic private tracker"
        assert hist[-2]["after"]["title"] == "New title"
        assert hist[-1]["actor"] == "reviewer"
    run(db_url, scenario)


def test_capture_retains_whole_file_freezes_context_and_is_immutable(db_url, tmp_path):
    raw, root = tmp_path / "raw.tsv", tmp_path / "persistent-archive"
    data = b"study\tstatus\nknown\tpublic\nfuture\tprivate\n\x00\xff"
    raw.write_bytes(data)

    async def scenario(c):
        await c.apply(manifest(), actor="curator", commit=True)
        kwargs = dict(version="export-2026-10-18", file=raw, archive_dir=root, actor="curator")
        preview = await c.capture("tracker", "tab-0", **kwargs)
        assert not preview["committed"] and not root.exists()
        assert (await c.show("tracker"))["snapshots"] == []
        cap = await c.capture("tracker", "tab-0", commit=True, **kwargs)
        blob = root / "sha256" / hashlib.sha256(data).hexdigest()
        assert blob.read_bytes() == data  # no parser, filtering or text conversion
        assert blob.stat().st_mode & 0o077 == 0
        assert cap["snapshot"]["size_bytes"] == len(data)
        assert cap["snapshot"]["artifact_uri"] == blob.resolve().as_uri()
        repeat = await c.capture("tracker", "tab-0", commit=True, **kwargs)
        assert repeat["action"] == "unchanged" and repeat["snapshot"] == cap["snapshot"]
        # Freeze the source context; later curation doesn't change the old snapshot.
        await c.apply(manifest(title="Retitled", data_license="CC-BY-4.0"), actor="editor", commit=True)
        snap = (await c.show("tracker"))["snapshots"][0]
        assert snap["source_descriptor"]["title"] == "Synthetic private tracker"
        assert snap["source_descriptor"]["data_license"] is None
        raw.write_bytes(b"different bytes")
        with pytest.raises(CatalogError, match="different bytes"):
            await c.capture("tracker", "tab-0", commit=True, **kwargs)
        async with c.engine.connect() as con:
            assert (await con.execute(select(func.count()).select_from(metadata_snapshots_tbl))).scalar_one() == 1
        # A new explicit version, even with identical bytes, is a separate observation.
        raw.write_bytes(data)
        kwargs["version"] = "export-2026-10-19"
        await c.capture("tracker", "tab-0", commit=True, **kwargs)
        assert len((await c.show("tracker"))["snapshots"]) == 2
        assert len(list((root / "sha256").iterdir())) == 1
    run(db_url, scenario)


def test_unknown_source_asset_and_actor_are_errors(db_url, tmp_path):
    raw = tmp_path / "input"
    raw.write_bytes(b"raw")

    async def scenario(c):
        with pytest.raises(CatalogError, match="unknown source"):
            await c.show("missing")
        with pytest.raises(CatalogError, match="unknown source"):
            await c.history("missing")
        with pytest.raises(CatalogError, match="actor"):
            await c.apply(manifest(), actor=" ", commit=True)
        kwargs = dict(version="v1", file=raw, archive_dir=tmp_path / "archive", actor="test", commit=True)
        with pytest.raises(CatalogError, match="unknown source"):
            await c.capture("tracker", "tab-0", **kwargs)
        await c.apply(manifest(), actor="test", commit=True)
        with pytest.raises(CatalogError, match="unknown asset"):
            await c.capture("tracker", "missing", **kwargs)
        assert not (tmp_path / "archive").exists()
    run(db_url, scenario)


def test_archive_failure_cannot_commit_a_receipt(db_url, tmp_path):
    raw, root = tmp_path / "input", tmp_path / "archive"
    raw.write_bytes(b"all bytes")
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    async def scenario(c):
        await c.apply(manifest(), actor="test", commit=True)
        kwargs = dict(version="v1", file=raw, archive_dir=root, actor="test", commit=True)
        with pytest.raises(ValueError, match="private"):
            await c.capture("tracker", "tab-0", **kwargs)
        assert (await c.show("tracker"))["snapshots"] == []
        root.chmod(0o700)
        blobdir = root / "sha256"
        blobdir.mkdir(mode=0o700)
        (blobdir / hashlib.sha256(raw.read_bytes()).hexdigest()).write_bytes(b"corrupt")
        (blobdir / hashlib.sha256(raw.read_bytes()).hexdigest()).chmod(0o600)
        with pytest.raises(ValueError, match="checksum mismatch"):
            await c.capture("tracker", "tab-0", **kwargs)
        assert (await c.show("tracker"))["snapshots"] == []
    run(db_url, scenario)


def test_archive_rejects_changed_input_and_symlinks(tmp_path):
    raw, root = tmp_path / "input", tmp_path / "archive"
    raw.write_bytes(b"first")
    inspected = archive.inspect_file(raw, root)
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        archive.retain(raw, root, inspected)
    assert list((root / "sha256").iterdir()) == []
    raw.write_bytes(b"first")
    (root / "sha256" / inspected.sha256).symlink_to(raw)
    with pytest.raises(ValueError, match="symlink"):
        archive.retain(raw, root, inspected)


def test_manifest_validation():
    base = manifest().model_dump(mode="json")
    for edit in ({"schema_version": 2}, {"schema_version": True}, {"unexpected": "ignored?"},
                 {"assets": []}, {"assets": [base["assets"][0], base["assets"][0]]}):
        with pytest.raises(ValidationError):
            SourceManifest.model_validate({**base, **edit})
    for field, value in (("source_id", "../bad"), ("url", "https://user:password@example.org"),
                         ("url", "file:///etc/passwd"), ("kind", "made-up"), ("title", " ")):
        with pytest.raises(ValidationError):
            SourceManifest.model_validate({**base, "source": {**base["source"], field: value}})


def test_cli_validation_has_no_database_or_lake(monkeypatch, tmp_path, capsys):
    from nextflow_telemetry.meta.cli import main
    from nextflow_telemetry.meta.catalog import cli

    def forbidden(*a, **kw):
        raise AssertionError("validation opened a database")
    monkeypatch.setattr(cli, "create_async_engine", forbidden)
    monkeypatch.delenv("SQLALCHEMY_URI", raising=False)
    path = tmp_path / "source.json"
    path.write_text(manifest().model_dump_json())
    main(["sources", "validate", str(path)])
    assert json.loads(capsys.readouterr().out)["valid"]
    with pytest.raises(SystemExit, match="set SQLALCHEMY_URI"):
        main(["sources", "list"])
    path.write_text('{"unexpected": true}')
    with pytest.raises(SystemExit):
        main(["sources", "validate", str(path)])
    invalid = manifest().model_dump(mode="json")
    invalid["source"]["url"] = "https://user:must-not-echo@example.org"
    path.write_text(json.dumps(invalid))
    with pytest.raises(SystemExit) as error:
        main(["sources", "validate", str(path)])
    assert "must-not-echo" not in str(error.value)


def test_cli_apply_and_reads(db_url, monkeypatch, tmp_path, capsys):
    from nextflow_telemetry.meta.cli import main

    monkeypatch.setenv("SQLALCHEMY_URI", db_url)
    path = tmp_path / "source.json"
    path.write_text(manifest().model_dump_json())
    main(["sources", "apply", str(path), "--actor", "cli"])
    assert not json.loads(capsys.readouterr().out)["committed"]
    main(["sources", "apply", str(path), "--actor", "cli", "--commit"])
    assert json.loads(capsys.readouterr().out)["committed"]
    main(["sources", "list", "--role", "inventory"])
    assert len(json.loads(capsys.readouterr().out)) == 1
    main(["sources", "show", "tracker"])
    assert json.loads(capsys.readouterr().out)["descriptor"]["source_id"] == "tracker"
    main(["sources", "history", "tracker"])
    assert len(json.loads(capsys.readouterr().out)) == 3
    raw = tmp_path / "source.tsv"
    raw.write_bytes(b"original,whole,source\n")
    args = ["sources", "capture", "tracker", "tab-0", "--version", "v1", "--file", str(raw),
            "--archive-dir", str(tmp_path / "archive"), "--actor", "cli"]
    main(args)
    assert not json.loads(capsys.readouterr().out)["committed"]
    main([*args, "--commit"])
    assert json.loads(capsys.readouterr().out)["snapshot"]["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()


def test_concurrent_registration_and_capture_are_idempotent(db_url, tmp_path):
    raw, root = tmp_path / "raw", tmp_path / "archive"
    raw.write_bytes(b"same bytes")

    async def scenario(c):
        replies = await asyncio.gather(*(c.apply(manifest(), actor="test", commit=True) for _ in range(3)))
        assert sorted(len(r["changes"]) for r in replies) == [0, 0, 3]
        replies = await asyncio.gather(*(c.capture("tracker", "tab-0", version="v1", file=raw,
            archive_dir=root, actor="test", commit=True) for _ in range(3)))
        assert sorted(r["action"] for r in replies) == ["added", "unchanged", "unchanged"]
    run(db_url, scenario)


# --- rclone (object-storage) archive, exercised with rclone's local backend ---

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")


@pytest.fixture()
def fake_remote(monkeypatch, tmp_path):
    """Remote `fakeremote:` is a plain directory: real rclone, no network."""
    monkeypatch.setenv("RCLONE_CONFIG_FAKEREMOTE_TYPE", "local")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "empty.conf"))
    store = tmp_path / "bucket"
    store.mkdir()
    return f"fakeremote:{store}/metadata-sources", store


@needs_rclone
def test_remote_root_detection():
    assert archive.is_remote("r2:cmgd-private/metadata-sources")
    assert not any(archive.is_remote(p) for p in ("/abs/path", "relative/dir", "./x:y", "file:///a"))


@needs_rclone
def test_remote_capture_retains_verifies_and_never_overwrites(db_url, tmp_path, fake_remote):
    root, bucket = fake_remote
    raw = tmp_path / "raw.bz2"
    data = b"BZh9\x00\xffwhole compressed file"
    raw.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    async def scenario(c):
        await c.apply(manifest(), actor="t", commit=True)
        kw = dict(version="v1", file=raw, archive_dir=root, actor="t")
        assert not (bucket / "metadata-sources").exists()  # preview uploads nothing
        await c.capture("tracker", "tab-0", **kw)
        assert not (bucket / "metadata-sources").exists()
        cap = await c.capture("tracker", "tab-0", commit=True, **kw)
        assert cap["snapshot"]["artifact_uri"] == f"{root}/sha256/{sha}"
        assert (bucket / "metadata-sources" / "sha256" / sha).read_bytes() == data
        assert (await c.capture("tracker", "tab-0", commit=True, **kw))["action"] == "unchanged"
        # A second version with the same bytes reuses the object.
        await c.capture("tracker", "tab-1", commit=True, **{**kw, "version": "v2"})
        assert len(list((bucket / "metadata-sources" / "sha256").iterdir())) == 1
        # Corrupt the stored object: a repeat capture must notice, not trust the key.
        (bucket / "metadata-sources" / "sha256" / sha).write_bytes(b"corrupt")
        with pytest.raises(ValueError, match="checksum mismatch"):
            await c.capture("tracker", "tab-0", commit=True, **kw)
    run(db_url, scenario)


@needs_rclone
def test_remote_retain_rejects_changed_input_and_corrupt_existing(tmp_path, fake_remote):
    root, bucket = fake_remote
    raw = tmp_path / "in"
    raw.write_bytes(b"first")
    inspected = archive.inspect_file(raw, root)
    raw.write_bytes(b"changed")  # changed between hashing and upload
    with pytest.raises(ValueError, match="checksum mismatch"):
        archive.retain(raw, root, inspected)
    raw.write_bytes(b"first")
    blob = bucket / "metadata-sources" / "sha256" / inspected.sha256
    blob.write_bytes(b"pre-existing corrupt object")  # same key, wrong bytes: not overwritten
    with pytest.raises(ValueError, match="checksum mismatch"):
        archive.retain(raw, root, inspected)
    assert blob.read_bytes() == b"pre-existing corrupt object"
