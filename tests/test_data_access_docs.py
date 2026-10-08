"""Every code block in docs/data-access.md runs against a local release served
over HTTP (cmgd_release_fixture), via the same runner as
scripts/smoke_public_data.py uses for the live site."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cmgd_release_fixture import public_site  # noqa: F401 -- fixture

_path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_public_data.py"
_spec = importlib.util.spec_from_file_location("smoke_public_data", _path)
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def _doc(site) -> str:
    return smoke.localize(smoke.DOC.read_text(), site.base)


def test_doc_has_snippets_for_every_access_path():
    langs = [lang for lang, _ in smoke.snippets(smoke.DOC.read_text())]
    assert {"sql", "python", "bash", "r"} <= set(langs)
    assert smoke.PUBLIC_BASE in smoke.DOC.read_text()


def test_doc_sql_python_bash_snippets_run(public_site, tmp_path):
    results = smoke.run(_doc(public_site), ("sql", "python", "bash"), tmp_path)
    failures = [f"{lang} #{n}:\n{err}" for lang, n, err in results if err]
    assert not failures, "\n\n".join(failures)
    assert len(results) == sum(1 for lang, _ in smoke.snippets(smoke.DOC.read_text()) if lang != "r")
    # the gene-family snippet really downloaded files from the stand-in cmgd-raw
    assert sorted(p.name for p in tmp_path.glob("*_genefamilies.tsv.gz"))
    assert (tmp_path / "metaphlan_species.tsv.gz").exists()


def test_doc_r_recipe_builds_a_tree_summarized_experiment(public_site, tmp_path):
    why = smoke.r_missing()
    if why:
        pytest.skip(why)
    [(lang, _, err)] = smoke.run(_doc(public_site), ("r",), tmp_path)
    assert err is None, err
