"""nf-etl mirror: Zenodo draft deposit against a fake Zenodo, HF layout and card."""
from __future__ import annotations

import hashlib
import json

import httpx
from cmgd_release_fixture import MPA, public_site  # noqa: F401 -- fixture

from nextflow_telemetry.etl import mirror


def _rdir(site):
    m = site.manifests[MPA]
    return site.root / m.dataset / m.release


def test_zenodo_draft_uploads_every_file_and_checks_md5(public_site, monkeypatch):
    rdir = _rdir(public_site)
    stored: dict[str, bytes] = {}
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path == "/api/deposit/depositions":
            return httpx.Response(201, json={"id": 7, "links": {"bucket": "https://z/api/files/b",
                                                                "html": "https://z/deposit/7"}})
        if req.method == "PUT" and req.url.path.startswith("/api/files/b/"):
            stored[req.url.path.removeprefix("/api/files/b/")] = req.read()
            return httpx.Response(201, json={})
        if req.method == "PUT":
            assert json.loads(req.read())["metadata"]["upload_type"] == "dataset"
            return httpx.Response(200, json={})
        if req.url.path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": n, "checksum": hashlib.md5(b).hexdigest()}
                                             for n, b in stored.items()])
        return httpx.Response(404)

    monkeypatch.setattr(mirror, "ZENODO_API", "https://z/api")
    out = mirror.zenodo_deposit(rdir, "https://pub", client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    assert out["checksums_match"] and out["state"].startswith("draft")
    assert "manifest.json" in stored and "tables__samples__data__part-00000.parquet" in stored
    assert not any(n.endswith(".html") for n in stored)
    assert not any("publish" in c for c in calls)  # never mints a DOI


def test_zenodo_metadata_licence_follows_the_tables():
    m = {"dataset": "d", "release": "r", "tables": [{"name": "a", "license": "CC0-1.0"}]}
    assert mirror.zenodo_metadata(m, "https://pub")["metadata"]["license"] == "cc0-1.0"
    m["tables"].append({"name": "b", "license": "CC-BY-4.0"})
    meta = mirror.zenodo_metadata(m, "https://pub")["metadata"]
    assert meta["license"] == "cc-by-4.0"
    assert {r["identifier"] for r in meta["related_identifiers"]} == {"https://pub/d/r/", mirror.OLM_DOI}


def test_hf_layout_has_one_config_per_table(public_site, tmp_path):
    rdir = _rdir(public_site)
    mirror.hf_layout(rdir, tmp_path, "org/repo", "https://pub")
    manifest = json.loads((rdir / "manifest.json").read_text())
    card = (tmp_path / "README.md").read_text()
    for t in manifest["tables"]:
        assert f"- config_name: {t['name']}\n  data_files: data/{t['name']}/*.parquet" in card
        assert list((tmp_path / "data" / t["name"]).glob("*.parquet"))
    assert card.startswith("---\nlicense: cc0-1.0\n")
    dry = mirror.hf_upload(rdir, "org/repo", "https://pub", dry_run=True)
    assert dry["private"] and "README.md" in dry["files"]
