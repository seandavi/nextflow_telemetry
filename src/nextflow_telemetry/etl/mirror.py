"""Mirrors of a finished release (plan §4 "Mirrors", Phase 5): Zenodo and a
Hugging Face Dataset repo. A mirror copies a built, verified release directory
from the local store; it never rebuilds anything, and cmgd-public stays the
canonical copy.

Nothing here makes a release public on its own. ``zenodo`` leaves the deposit as
an unpublished draft (publishing mints a DOI: a human does it in the Zenodo UI
after review). ``hf`` creates the repo private unless ``public=True``.

Tokens come from the environment (``ZENODO_TOKEN``, ``HF_TOKEN``) or GCP Secret
Manager (project ``cdsci-infra``: ``cdsci-zenodo-api-token``,
``cdsci-huggingface-token``) and are never written anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import Template

import httpx

ZENODO_API = os.environ.get("ZENODO_API", "https://zenodo.org/api")  # sandbox: https://sandbox.zenodo.org/api
ZENODO_MAX_FILES = 100  # Zenodo's default per-record file limit
OLM_DOI = "10.5281/zenodo.23087712"


def token(env: str, secret: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    return subprocess.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret}",
                           "--project=cdsci-infra"], check=True, capture_output=True,
                          text=True).stdout.strip()


def release_files(release_dir: Path) -> list[Path]:
    """The upload set: every file of the release (tables, cmgd.duckdb, manifest,
    provenance, README, studies/), without the HTML pages (they link back to
    cmgd-public relative paths that don't exist on a mirror)."""
    return sorted(p for p in release_dir.rglob("*") if p.is_file() and p.suffix != ".html")


def _license(manifest: dict) -> str:
    """One licence for a whole record: CC-BY-4.0 if any table needs attribution."""
    return "cc-by-4.0" if any(t["license"] == "CC-BY-4.0" for t in manifest["tables"]) else "cc0-1.0"


def zenodo_metadata(manifest: dict, base_url: str) -> dict:
    url = f"{base_url}/{manifest['dataset']}/{manifest['release']}/"
    licences = "".join(f"<li>{t['name']}: {t['license']}</li>" for t in manifest["tables"])
    related = [{"identifier": url, "relation": "isIdenticalTo", "resource_type": "dataset"}]
    if _license(manifest) == "cc-by-4.0":
        related.append({"identifier": OLM_DOI, "relation": "isDerivedFrom", "scheme": "doi",
                        "resource_type": "dataset"})
    return {"metadata": {
        "upload_type": "dataset",
        "title": f"curatedMetagenomicData {manifest['dataset']} release {manifest['release']}",
        "version": manifest["release"],
        "creators": [{"name": "curatedMetagenomicData"}],
        "description": (f"<p>Mirror of {url}, the canonical copy. Parquet tables, "
                        "cmgd.duckdb (DuckDB views over the canonical copy's Parquet URLs), "
                        "manifest with per-file sha256, and per-study downloads. "
                        "File names flatten the release paths ('/' → '__').</p>"
                        f"<p>Per-table licences:</p><ul>{licences}</ul>"),
        "license": _license(manifest),
        "related_identifiers": related,
        "keywords": ["microbiome", "metagenomics", "MetaPhlAn", "curatedMetagenomicData"],
    }}


def zenodo_deposit(release_dir: Path, base_url: str, *, previous: int | None = None,
                   client: httpx.Client | None = None, dry_run: bool = False) -> dict:
    """Upload the release as a Zenodo **draft**: a new version of deposition
    ``previous`` (keeps one concept DOI) or a new deposition. Returns the draft
    plus ``checksums_match`` (Zenodo's md5 per file vs the local bytes)."""
    manifest = json.loads((release_dir / "manifest.json").read_text())
    files = release_files(release_dir)
    if len(files) > ZENODO_MAX_FILES:
        raise ValueError(f"{len(files)} files > Zenodo's {ZENODO_MAX_FILES}; zip studies/ first")
    meta = zenodo_metadata(manifest, base_url)
    names = {p.relative_to(release_dir).as_posix().replace("/", "__"): p for p in files}
    if dry_run:
        return {"dry_run": True, "files": sorted(names), "metadata": meta["metadata"]}
    c = client or httpx.Client(timeout=600, headers={
        "Authorization": f"Bearer {token('ZENODO_TOKEN', 'cdsci-zenodo-api-token')}"})
    if previous:
        r = c.post(f"{ZENODO_API}/deposit/depositions/{previous}/actions/newversion")
        r.raise_for_status()
        draft = c.get(r.json()["links"]["latest_draft"]).raise_for_status().json()
        for f in draft.get("files", []):  # a new version starts with the old files
            c.delete(f"{ZENODO_API}/deposit/depositions/{draft['id']}/files/{f['id']}").raise_for_status()
    else:
        draft = c.post(f"{ZENODO_API}/deposit/depositions", json={}).raise_for_status().json()
    for name, p in names.items():
        with p.open("rb") as fh:
            c.put(f"{draft['links']['bucket']}/{name}", content=fh).raise_for_status()
    c.put(f"{ZENODO_API}/deposit/depositions/{draft['id']}", json=meta).raise_for_status()
    remote = {f["filename"]: f["checksum"].removeprefix("md5:") for f in
              c.get(f"{ZENODO_API}/deposit/depositions/{draft['id']}/files").raise_for_status().json()}
    local = {n: hashlib.md5(p.read_bytes()).hexdigest() for n, p in names.items()}
    return {"id": draft["id"], "html": draft["links"].get("html"), "state": "draft (unpublished)",
            "checksums_match": remote == local}


CARD = Template("""---
license: $license
pretty_name: curatedMetagenomicData $dataset
tags: [microbiome, metagenomics, metaphlan]
configs:
$configs
---

# curatedMetagenomicData `$dataset`, release `$release`

Mirror of <$url>, the canonical copy (manifest with sha256 per file, cmgd.duckdb views,
per-study downloads, provenance). One config per table:

```python
from datasets import load_dataset
samples = load_dataset("$repo", "samples", split="train")
```

| table | rows | licence | description |
|---|---|---|---|
$tables

## Licence and citation

Tables are licensed individually (above). Cite curatedMetagenomicData:
$cmgd

$olm
""")


def dataset_card(manifest: dict, provenance: dict, repo: str, base_url: str) -> str:
    cit = provenance.get("cmgd", {}).get("citation", {})
    cc_by = _license(manifest) == "cc-by-4.0"
    return CARD.substitute(
        license=_license(manifest), dataset=manifest["dataset"], release=manifest["release"],
        url=f"{base_url}/{manifest['dataset']}/{manifest['release']}/", repo=repo,
        configs="\n".join(f"- config_name: {t['name']}\n  data_files: data/{t['name']}/*.parquet"
                          for t in manifest["tables"]),
        tables="\n".join(f"| {t['name']} | {t['row_count']} | {t['license']} | {t['description']} |"
                         for t in manifest["tables"]),
        cmgd=cit.get("cmgd", ""),
        olm=f"CC-BY-4.0 tables require this attribution: {cit.get('olm', '')}" if cc_by else "")


def hf_layout(release_dir: Path, out: Path, repo: str, base_url: str) -> None:
    """``data/<table>/*.parquet`` + ``README.md`` card + manifest/provenance in ``out``."""
    manifest = json.loads((release_dir / "manifest.json").read_text())
    provenance = json.loads((release_dir / "provenance.json").read_text())
    for t in manifest["tables"]:
        d = out / "data" / t["name"]
        d.mkdir(parents=True)
        for p in (release_dir / "tables" / t["name"] / "data").glob("*.parquet"):
            shutil.copy2(p, d / p.name)
    for name in ("manifest.json", "provenance.json"):
        shutil.copy2(release_dir / name, out / name)
    (out / "README.md").write_text(dataset_card(manifest, provenance, repo, base_url))


def hf_upload(release_dir: Path, repo: str, base_url: str, *, public: bool = False,
              dry_run: bool = False) -> dict:
    """Upload the release to Dataset repo ``repo`` (created private unless
    ``public``), as one commit tagged with the release id; ``main`` = latest."""
    release = json.loads((release_dir / "manifest.json").read_text())["release"]
    with tempfile.TemporaryDirectory() as tmp:
        hf_layout(release_dir, Path(tmp), repo, base_url)
        files = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file())
        if dry_run:
            return {"dry_run": True, "repo": repo, "private": not public, "tag": release, "files": files}
        from huggingface_hub import HfApi  # type: ignore[import-not-found]

        api = HfApi(token=token("HF_TOKEN", "cdsci-huggingface-token"))
        api.create_repo(repo, repo_type="dataset", private=not public, exist_ok=True)
        commit = api.upload_folder(repo_id=repo, repo_type="dataset", folder_path=tmp,
                                   commit_message=f"release {release}")
        api.create_tag(repo, tag=release, repo_type="dataset", exist_ok=True)
    return {"repo": repo, "private": not public, "tag": release, "commit": commit.oid,
            "files": len(files)}
