"""The heartbeat's nf_client_version carries the installed commit (#254)."""

import importlib.metadata

from nf_client import cli


class _Dist:
    version = "1.0.0"

    def __init__(self, direct_url):
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None


def test_git_install_reports_sha(monkeypatch):
    url = '{"url": "file:///x", "vcs_info": {"vcs": "git", "commit_id": "b781aa78a26b26a5"}}'
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: _Dist(url))
    assert cli._client_version() == "1.0.0+b781aa7"


def test_path_install_reports_bare_version(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: _Dist('{"url": "file:///x"}'))
    assert cli._client_version() == "1.0.0"
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: _Dist(None))
    assert cli._client_version() == "1.0.0"
