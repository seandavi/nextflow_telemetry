"""Run every code snippet in docs/data-access.md against a cmgd-public site.

  uv run python scripts/smoke_public_data.py                       # the live site
  uv run python scripts/smoke_public_data.py --base http://…/public --no-r

Fenced ``sql`` blocks run in order on one DuckDB connection, ``python`` blocks in
order in one namespace, ``bash`` blocks one by one, and all ``r`` blocks as one
Rscript. Everything runs in a scratch directory. The doc's example release id is
replaced by each dataset's current ``latest.json`` release, and its public base
by ``--base``. tests/test_data_access_docs.py runs the same code against a local
release; this script is for the live site and is not run in CI.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

DOC = Path(__file__).resolve().parents[1] / "docs" / "data-access.md"
PUBLIC_BASE = "https://cmgd-public.cancerdatasci.org"
DOC_RELEASE = "2026-10-08"  # the example release id the doc's snippets use
R_PACKAGES = ("DBI", "duckdb", "Matrix", "TreeSummarizedExperiment", "jsonlite")
_FENCE = re.compile(r"^```(sql|python|bash|r)\n(.*?)^```", re.M | re.S)


def snippets(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _FENCE.finditer(text)]


def localize(text: str, base: str) -> str:
    """Point the doc at ``base`` and at each dataset's current release."""
    return published(text, base)[0]


def published(text: str, base: str) -> tuple[str, set[str]]:
    """``localize`` plus the doc's datasets that have no release yet (latest.json 404s)."""
    text = text.replace(PUBLIC_BASE, base.rstrip("/"))
    missing: set[str] = set()
    for dataset in sorted(set(re.findall(rf"/([\w.]+-[\d.]+)/{re.escape(DOC_RELEASE)}\b", text))):
        # Cloudflare rejects urllib's default User-Agent on cancerdatasci.org.
        req = urllib.request.Request(f"{base.rstrip('/')}/{dataset}/latest.json",
                                     headers={"User-Agent": "cmgd-smoke-public-data"})
        try:
            with urllib.request.urlopen(req) as r:
                release = json.load(r)["release"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            missing.add(dataset)  # documented but not published yet: its snippets are skipped
            continue
        text = text.replace(f"/{dataset}/{DOC_RELEASE}", f"/{dataset}/{release}")
    return text, missing


def r_missing() -> str | None:
    """Why the R snippets can't run here, or None if they can."""
    if not shutil.which("Rscript"):
        return "Rscript not on PATH"
    check = "; ".join(f"library({p})" for p in R_PACKAGES)
    r = subprocess.run(["Rscript", "-e", check], capture_output=True, text=True)
    return None if r.returncode == 0 else f"R packages missing ({', '.join(R_PACKAGES)}): {r.stderr.strip()[-300:]}"


def run(text: str, langs: tuple[str, ...], workdir: Path,
        skip: set[str] = frozenset()) -> list[tuple[str, int, str | None]]:  # type: ignore[assignment]
    """Run the doc's snippets; returns (lang, n, error or None) per snippet/group.

    Snippets naming a dataset in ``skip`` (not published yet) are left out.
    """
    results: list[tuple[str, int, str | None]] = []
    blocks = [(lang, code) for lang, code in snippets(text)
              if not any(d in code for d in skip)]
    with contextlib.chdir(workdir):
        con = duckdb.connect()
        ns: dict = {"__name__": "__doc__"}
        for n, (lang, code) in enumerate(blocks):
            if lang not in langs or lang == "r":
                continue
            try:
                if lang == "sql":
                    con.execute(code).fetchall()
                elif lang == "python":
                    exec(compile(code, f"<data-access.md python #{n}>", "exec"), ns)
                else:
                    subprocess.run(["bash", "-euo", "pipefail", "-c", code], check=True,
                                   capture_output=True, text=True)
                results.append((lang, n, None))
            except subprocess.CalledProcessError as e:
                results.append((lang, n, f"{e}\n{e.stderr}"))
            except Exception:
                results.append((lang, n, traceback.format_exc(limit=3)))
        con.close()
        r_code = "\n".join(code for lang, code in blocks if lang == "r")
        if "r" in langs and r_code:
            Path("data-access.R").write_text(r_code)
            p = subprocess.run(["Rscript", "data-access.R"], capture_output=True, text=True)
            results.append(("r", -1, None if p.returncode == 0 else p.stdout[-2000:] + p.stderr[-3000:]))
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=PUBLIC_BASE)
    p.add_argument("--no-r", action="store_true", help="skip the R snippets")
    a = p.parse_args(argv)
    langs: tuple[str, ...] = ("sql", "python", "bash")
    if not a.no_r:
        why = r_missing()
        if why:
            print(f"skipping R: {why}")
        else:
            langs += ("r",)
    try:
        text, missing = published(DOC.read_text(), a.base)
    except urllib.error.URLError as e:
        print(f"FAIL resolving the current releases (latest.json) under {a.base}: {e}")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        results = run(text, langs, Path(tmp), skip=missing)
    for d in sorted(missing):
        print(f"skip {d}: not published yet (no latest.json)")
    for lang, n, err in results:
        print(f"{'FAIL' if err else 'ok  '} {lang} #{n}" + (f"\n{err}" if err else ""))
    return 1 if any(err for *_, err in results) else 0


if __name__ == "__main__":
    sys.exit(main())
