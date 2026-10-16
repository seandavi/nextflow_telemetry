"""Static pages for cmgd-public (plan §4 "Web pages", §5): the store root's
``index.html`` and ``llms.txt``, each release's ``index.html`` and every study's
``studies/<study>/index.html``. Plain HTML from stdlib ``string.Template``: no
build step, no JavaScript, one inline stylesheet with a light and a dark scheme.

Everything is read back from files the release already has (manifest,
``files.json``, ``provenance.json``, ``studies/index.json``, the ``samples`` and
metadata Parquet), so a page can't disagree with the data it describes. Links
between pages are relative (the site works from any host, e.g. a local
``http.server``); URLs inside code snippets are absolute (``base``), because
DuckDB needs them.
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path
from string import Template

import duckdb

SAMPLES = "samples"
HARMONIZED = "sample_metadata_harmonized"
# The fields the harmonized view covers (plan §3 field map), in display order.
FIELDS = ("age_years", "sex", "country", "bmi", "antibiotics_current_use", "health_condition")
NO_METADATA = ("Sample metadata (cMD curation and the Olm Microbiome Repo) is not part of this "
               "release yet; it is planned for the next release.")

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<meta name="description" content="$description">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f7f8fa; --surface: #ffffff; --border: #e3e7ee; --text: #18202e; --muted: #5b6678;
  --accent: oklch(0.52 0.13 160); --accent-bg: oklch(0.52 0.13 160 / 0.08);
  --warn-bg: oklch(0.9 0.08 75 / 0.45); --code-bg: #f0f2f6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #080c14; --surface: #0f1623; --border: rgba(255,255,255,0.09); --text: #e2e8f0;
    --muted: #8a97b0; --accent: oklch(0.72 0.15 160); --accent-bg: oklch(0.65 0.16 160 / 0.14);
    --warn-bg: oklch(0.78 0.14 75 / 0.14); --code-bg: #161f2e;
  }
}
:root[data-theme="dark"] {
  --bg: #080c14; --surface: #0f1623; --border: rgba(255,255,255,0.09); --text: #e2e8f0;
  --muted: #8a97b0; --accent: oklch(0.72 0.15 160); --accent-bg: oklch(0.65 0.16 160 / 0.14);
  --warn-bg: oklch(0.78 0.14 75 / 0.14); --code-bg: #161f2e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.6 'DM Sans', system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
main { max-width: 980px; margin: 0 auto; padding: 40px 16px 80px; }
header.top { border-bottom: 1px solid var(--border); padding-bottom: 20px; margin-bottom: 28px; }
.crumbs { font-size: 14px; color: var(--muted); margin-bottom: 10px; }
h1 { font-size: 30px; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.01em; }
h2 { font-size: 20px; margin: 40px 0 12px; }
h3 { font-size: 16px; margin: 24px 0 8px; }
p { margin: 0 0 12px; }
.lede { color: var(--muted); font-size: 17px; max-width: 720px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre, .mono { font-family: 'DM Mono', ui-monospace, monospace; font-size: 13.5px; }
pre { background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px; overflow-x: auto; line-height: 1.5; margin: 0 0 16px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
  margin: 20px 0 8px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; }
.stat b { display: block; font-size: 24px; line-height: 1.2; }
.stat span { color: var(--muted); font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px; margin: 0 0 16px; }
.note { background: var(--warn-bg); border-radius: 8px; padding: 12px 16px; margin: 0 0 16px; }
.scroll { overflow-x: auto; margin: 0 0 16px; border: 1px solid var(--border);
  border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border);
  vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 12.5px; text-transform: uppercase;
  letter-spacing: 0.03em; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.sha { font-family: 'DM Mono', monospace; font-size: 12px; color: var(--muted);
  word-break: break-all; }
.pill { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 99px;
  background: var(--accent-bg); color: var(--accent); white-space: nowrap; }
dl.kv { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; margin: 0; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; }
footer { margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 14px; }
@media (max-width: 600px) { h1 { font-size: 24px; } dl.kv { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<main>
$body
<footer>curatedMetagenomicData public releases &middot; <a href="$root/llms.txt">llms.txt</a>
&middot; <a href="$root/index.json">index.json</a> &middot;
<a href="https://github.com/seandavi/nextflow_telemetry/blob/main/docs/data-access.md">access guide</a></footer>
</main>
</body>
</html>
""")


def _size(n: int | None) -> str:
    if n is None:
        return "–"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1000 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1000
    return ""


def _table(head: list[str], rows: list[list[str]], num: set[int] = frozenset()) -> str:  # type: ignore[assignment]
    """Cells are already-escaped HTML; ``num`` are right-aligned column indexes."""
    th = "".join(f'<th{" class=num" if i in num else ""}>{h}</th>' for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f'<td{" class=num" if i in num else ""}>{c}</td>'
                                   for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _pre(code: str) -> str:
    return f"<pre><code>{escape(code.strip())}</code></pre>"


def _sql(path: Path, query: str, params: dict | None = None) -> list[tuple]:
    return duckdb.sql(query.format(p=f"read_parquet('{path}')"), params=params).fetchall()


class Release:
    """One local release directory, read back from its own files."""

    def __init__(self, release_dir: Path, base: str):
        self.dir = release_dir
        self.manifest = json.loads((release_dir / "manifest.json").read_text())
        self.dataset, self.release = self.manifest["dataset"], self.manifest["release"]
        self.url = f"{base}/{self.dataset}/{self.release}"
        prov = release_dir / "provenance.json"
        self.cmgd = json.loads(prov.read_text()).get("cmgd", {}) if prov.exists() else {}
        studies = release_dir / "studies" / "index.json"
        self.studies = json.loads(studies.read_text())["studies"] if studies.exists() else []
        self.tables = {t["name"]: t for t in self.manifest["tables"]}
        qc = self.tables.get("qc_metrics")
        self.n_samples = qc["row_count"] if qc else 0

    def parquet(self, table: str) -> Path:
        return self.dir / "tables" / table / "data" / "part-00000.parquet"

    def has(self, table: str) -> bool:
        return table in self.tables

    def files(self, table: str) -> list[dict]:
        return json.loads((self.dir / "tables" / table / "files.json").read_text())["files"]

    def coverage(self) -> dict[str, dict[str, int]]:
        """study -> field -> samples with a harmonized value."""
        if not self.has(HARMONIZED):
            return {}
        out: dict[str, dict[str, int]] = {}
        for study, field, n in _sql(self.parquet(HARMONIZED),
                                    "SELECT study_name, field, count(DISTINCT sample_key) FROM {p} "
                                    "WHERE value IS NOT NULL GROUP BY ALL"):
            out.setdefault(study, {})[field] = n
        return out


def _facts(r: Release) -> str:
    reg = r.cmgd.get("registration") or {}
    runs = r.cmgd.get("pipeline_runs") or []
    dbs = r.cmgd.get("databases") or {}
    srcs = r.cmgd.get("metadata_sources") or []
    rows = [("Dataset", f"<code>{escape(r.dataset)}</code>"),
            ("Release", f"<code>{escape(r.release)}</code> &middot; published "
                        f"{escape(r.manifest.get('published_at', '')[:10])}")]
    if reg.get("revision"):
        repo = reg.get("repository_url") or ""
        rows.append(("Registration", f"<code>{escape(reg['workflow_id'])}/{escape(reg['version'])}</code>, "
                     f"revision <a href=\"{escape(repo)}/tree/{escape(reg['revision'])}\">"
                     f"{escape(reg['revision'])}</a>"))
    if reg.get("params"):
        rows.append(("Parameters", "<code>" + escape(json.dumps(reg["params"])) + "</code>"))
    for run in runs:
        rows.append(("Pipeline run", f"curatedMetagenomicsNextflow {escape(str(run['pipeline_version']))} "
                     f"at <code>{escape(str(run['git_commit'])[:12])}</code> "
                     f"({run['n_samples']} samples)"))
    labels = {"metaphlan": "MetaPhlAn", "metaphlan_index": "MetaPhlAn index",
              "kraken2_bracken": "Kraken2 / Bracken DB", "card": "CARD"}
    for k, v in dbs.items():
        rows.append((labels.get(k, k), f"<code>{escape(v)}</code>"))
    for s in srcs:
        rows.append((f"Metadata: {escape(str(s.get('source')))}",
                     escape(f"release {s.get('release')}" + (f", doi:{s['doi']}" if s.get("doi") else "")
                            + (f", {s['license']}" if s.get("license") else ""))))
    if not srcs:
        rows.append(("Metadata", escape(NO_METADATA)))
    return '<dl class="kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def _studies_table(r: Release, prefix: str) -> str:
    cov = r.coverage()
    head = ["Study", "Samples"] + (["Metadata coverage"] if cov else []) + ["Files"]
    rows = []
    for s in r.studies:
        name = s["study_name"]
        page = r.dir / "studies" / name / "index.html"
        link = (f'<a href="{prefix}studies/{escape(name)}/index.html">{escape(name)}</a>'
                if page.exists() else escape(name))
        row = [link, f"{s['n_samples']:,}"]
        if cov:
            c = cov.get(name, {})
            row.append(" ".join(f'<span class="pill">{escape(f)} {c[f]}/{s["n_samples"]}</span>'
                                for f in FIELDS if c.get(f)) or "–")
        row.append(" &middot; ".join(f'<a href="{prefix}{escape(f["path"])}">{escape(f["name"])}</a>'
                            for f in s["files"] if not f["name"].endswith(".html")))
        rows.append(row)
    return _table(head, rows, {1})


def _files_table(r: Release, prefix: str) -> str:
    rows = []
    for name, t in sorted(r.tables.items()):
        for f in r.files(name):
            path = f"tables/{name}/{f['uri']}"
            rows.append([f'<a href="{prefix}{escape(path)}"><code>{escape(name)}</code></a>'
                         f'<br><span class="sha">{escape(t["description"])}</span>',
                         f"{t['row_count']:,}", _size(f["size"]),
                         f'<span class="pill">{escape(t["license"])}</span>',
                         f'<span class="sha">{escape(f["sha256"])}</span>'])
    for name in ("manifest.json", "provenance.json", "catalog.ducklake", "README.md",
                 "studies/index.json"):
        p = r.dir / name
        if p.exists():
            art = (r.manifest.get("artifacts", {}).get("ducklake", {})
                   if name == "catalog.ducklake" else {})
            rows.append([f'<a href="{prefix}{name}"><code>{name}</code></a>', "", _size(p.stat().st_size),
                         "", f'<span class="sha">{escape(art.get("sha256", ""))}</span>'])
    return _table(["File", "Rows", "Size", "Licence", "sha256"], rows, {1, 2})


def _snippet_list(r: Release, study: str | None = None) -> list[str]:
    """DuckDB examples against the release's absolute URLs, with a real sample."""
    u = r.url
    # The example sample is the first one with a species-level profile.
    q = ("SELECT sample_key, study_name FROM {p} WHERE rank = 'species' AND data_type = 'full_data' "
         "AND humann_bundle IS NULL "
         + ("AND study_name = $s " if study else "") + "ORDER BY study_name, sample_key LIMIT 1")
    got = _sql(r.parquet("taxonomic_profile_metaphlan"), q, {"s": study} if study else None)
    sample = got[0] if got else None
    rs, st = (sample[0], sample[1]) if sample else ("RS.…", study or "…")
    out = [f"""-- DuckDB 1.5+: attach the whole release (read-only, fetches only what a query needs)
INSTALL ducklake; LOAD ducklake; INSTALL httpfs; LOAD httpfs;
ATTACH '{u}/catalog.ducklake' AS cmgd (
  TYPE DUCKLAKE, DATA_PATH '{u}/', OVERRIDE_DATA_PATH, READ_ONLY);
SELECT study_name, count(*) AS n_samples FROM cmgd.samples GROUP BY 1 ORDER BY 1;""",
           f"""-- Top 5 MetaPhlAn species of one sample, straight from Parquet (no attach needed)
SELECT clade_name, relative_abundance
FROM read_parquet('{u}/tables/taxonomic_profile_metaphlan/data/part-00000.parquet')
WHERE sample_key = '{rs}' AND rank = 'species'
  AND data_type = 'full_data' AND humann_bundle IS NULL
ORDER BY relative_abundance DESC LIMIT 5;""",
           f"""-- A study's samples: readset id, runs, BioSample, BioProject
SELECT sample_key, run_accessions, biosample, bioproject
FROM read_parquet('{u}/tables/samples/data/part-00000.parquet')
WHERE study_name = '{st}';"""]
    if r.has(HARMONIZED):
        out.append(f"""-- A study's harmonized metadata, with the source and source release of each value
SELECT sample_key, field, value, source, release, route, confidence
FROM read_parquet('{u}/tables/{HARMONIZED}/data/part-00000.parquet')
WHERE study_name = '{st}' ORDER BY sample_key, field;""")
    return out


def _snippets(r: Release, study: str | None = None) -> str:
    return "".join(_pre(s) for s in _snippet_list(r, study))


def _cite(r: Release | None) -> str:
    cit = (r.cmgd.get("citation") if r else None) or {}
    olm = any(t["license"] != "CC0-1.0" for t in r.tables.values()) if r else False
    parts = [f"<p>Cite the dataset and release id you used (e.g. <code>{escape(r.dataset)} "
             f"{escape(r.release)}</code>) and curatedMetagenomicData:</p>" if r else "",
             f'<div class="card">{escape(cit.get("cmgd", ""))}</div>' if cit.get("cmgd") else ""]
    if olm and cit.get("olm"):
        parts.append("<p>The tables marked <span class=\"pill\">CC-BY-4.0</span> are derived from the "
                     "Olm lab Microbiome Repo and require attribution:</p>"
                     f'<div class="card">{escape(cit["olm"])}</div>')
    parts.append("<p>All other tables are <span class=\"pill\">CC0-1.0</span>: no rights reserved. "
                 "Each table's licence is in <code>manifest.json</code>.</p>")
    return "".join(parts)


def _stats(r: Release) -> str:
    items = [(f"{r.n_samples:,}", "samples"), (f"{len(r.studies):,}", "studies"),
             (escape(r.release), "release"), (f"{len(r.tables)}", "tables")]
    return '<div class="stats">' + "".join(f'<div class="stat"><b>{v}</b><span>{k}</span></div>'
                                           for v, k in items) + "</div>"


def release_body(r: Release, prefix: str) -> str:
    """The release's details; links are relative to the page (``prefix`` = path
    from the page to the release directory, ending in '/' or empty)."""
    note = "" if r.has(HARMONIZED) else f'<div class="note">{escape(NO_METADATA)}</div>'
    return (f"{_stats(r)}{note}<h2>Provenance</h2><div class=\"card\">{_facts(r)}</div>"
            f"<h2>Studies</h2>{_studies_table(r, prefix)}"
            f"<h2>Query it over HTTPS</h2><p>Every file is public: no account, no credentials. "
            f"Parquet is sorted by study and sample, so a filtered query reads only the row "
            f"groups it needs.</p>{_snippets(r)}"
            f"<h2>Files</h2>{_files_table(r, prefix)}")


def write_release_page(release_dir: Path, base: str) -> None:
    r = Release(release_dir, base)
    body = (f'<header class="top"><div class="crumbs"><a href="../../index.html">cmgd-public</a> / '
            f'{escape(r.dataset)} / {escape(r.release)}</div><h1>{escape(r.dataset)} '
            f'<span class="pill">{escape(r.release)}</span></h1>'
            f'<p class="lede">{escape(r.manifest.get("description") or "")}</p></header>'
            f"{release_body(r, '')}<h2>How to cite</h2>{_cite(r)}")
    (release_dir / "index.html").write_text(PAGE.substitute(
        title=escape(f"{r.dataset} {r.release} · curatedMetagenomicData"),
        description=escape(f"curatedMetagenomicData release {r.dataset} {r.release}"),
        body=body, root="../.."))


def write_study_pages(release_dir: Path, base: str) -> None:
    """``studies/<study>/index.html`` for every study, then the release page."""
    r = Release(release_dir, base)
    cov = r.coverage()
    for s in r.studies:
        name = s["study_name"]
        d = release_dir / "studies" / name
        samples = _sql(r.parquet(SAMPLES),
                       "SELECT sample_key, run_accessions, biosample, bioproject, completed_at "
                       "FROM {p} WHERE study_name = $s ORDER BY sample_key", {"s": name}) \
            if r.has(SAMPLES) else []
        srows = [[f"<code>{escape(k)}</code>", escape((runs or "").replace(";", "; ")),
                  escape(bs or "–"), escape(bp or "–"), escape((done or "")[:10])]
                 for k, runs, bs, bp, done in samples]
        if r.has(HARMONIZED):
            c = cov.get(name, {})
            srcs = _sql(r.parquet(HARMONIZED),
                        "SELECT field, source, release, count(DISTINCT sample_key) FROM {p} "
                        "WHERE study_name = $s AND value IS NOT NULL GROUP BY ALL ORDER BY ALL",
                        {"s": name})
            meta = (_table(["Field", "Samples with a value"],
                           [[f"<code>{f}</code>", f"{c.get(f, 0)} / {s['n_samples']}"] for f in FIELDS], {1})
                    + "<h3>Where the values come from</h3>"
                    + _table(["Field", "Source", "Source release", "Samples"],
                             [[f"<code>{escape(str(f))}</code>", escape(str(src)), escape(str(rel)), str(n)]
                              for f, src, rel, n in srcs], {3}))
        else:
            meta = f'<div class="note">{escape(NO_METADATA)}</div>'
        files = _table(["File", "Size", "sha256"],
                       [[f'<a href="{escape(f["name"])}">{escape(f["name"])}</a>', _size(f["size"]),
                         f'<span class="sha">{escape(f["sha256"])}</span>'] for f in s["files"]], {1})
        body = (f'<header class="top"><div class="crumbs"><a href="../../../../index.html">cmgd-public</a> / '
                f'<a href="../../index.html">{escape(r.dataset)} {escape(r.release)}</a> / {escape(name)}</div>'
                f"<h1>{escape(name)}</h1><p class=\"lede\">{s['n_samples']} samples in "
                f"<code>{escape(r.dataset)}</code> release <code>{escape(r.release)}</code>.</p></header>"
                f"<h2>Metadata coverage</h2>{meta}"
                f"<h2>Downloads</h2>{files}"
                f"<h2>Query this study</h2>{_snippets(r, name)}"
                f"<h2>Samples</h2>{_table(['Readset', 'Runs', 'BioSample', 'BioProject', 'Completed'], srows)}")
        (d / "index.html").write_text(PAGE.substitute(
            title=escape(f"{name} · {r.dataset} {r.release}"),
            description=escape(f"{name}: {s['n_samples']} samples, curatedMetagenomicData {r.dataset}"),
            body=body, root="../../../.."))
    write_release_page(release_dir, base)


def _releases(root: Path, base: str) -> list[tuple[dict, Release | None]]:
    idx = json.loads((root / "index.json").read_text())["datasets"]
    out = []
    for d in idx:
        rd = root / d["id"] / d["latest_release"]
        out.append((d, Release(rd, base) if (rd / "manifest.json").exists() else None))
    return out


def write_site(root: Path, base: str) -> None:
    """The store root's ``index.html`` and ``llms.txt``, from ``index.json`` and
    the local releases. The featured release is the most recently updated dataset
    that has a release page."""
    rels = _releases(root, base)
    built = [(d, r) for d, r in rels if r and (r.dir / "index.html").exists()]
    featured = max(built, key=lambda x: x[0]["updated_at"])[1] if built else None
    rows = []
    for d, r in rels:
        page = f"{d['id']}/{d['latest_release']}/" + ("index.html" if r and (r.dir / "index.html").exists()
                                                      else "manifest.json")
        rows.append([f'<a href="{escape(page)}"><code>{escape(d["id"])}</code></a>',
                     escape(d["latest_release"]), f"{r.n_samples:,}" if r else "–",
                     f"{len(r.studies):,}" if r else "–",
                     f'<a href="{escape(d["latest"])}">latest.json</a>'])
    body = ('<header class="top"><h1>curatedMetagenomicData</h1>'
            '<p class="lede">Uniformly processed taxonomic, functional and resistome profiles of public '
            'human microbiome shotgun metagenomes, produced by curatedMetagenomicsNextflow and published '
            'as open Parquet files you can query in place over HTTPS.</p></header>'
            "<h2>Datasets</h2><p>Each pipeline registration is a dataset; each dataset has immutable, "
            "dated releases. <code>latest.json</code> names a dataset's newest release.</p>"
            + _table(["Dataset", "Latest release", "Samples", "Studies", "Pointer"], rows, {2, 3}))
    if featured:
        body += (f'<h2>Latest release: {escape(featured.dataset)} '
                 f'<span class="pill">{escape(featured.release)}</span></h2>'
                 f'<p><a href="{escape(featured.dataset)}/{escape(featured.release)}/index.html">'
                 f"Release page</a></p>"
                 + release_body(featured, f"{featured.dataset}/{featured.release}/"))
    body += (f"<h2>How to cite</h2>{_cite(featured)}"
             "<h2>For programs and agents</h2><p><a href=\"llms.txt\">llms.txt</a> describes the site, "
             "its URL patterns and join keys in plain text. <a href=\"index.json\">index.json</a> lists "
             "every dataset; nothing on the site can be listed, so every file is reachable from an index.</p>")
    (root / "index.html").write_text(PAGE.substitute(
        title="curatedMetagenomicData · public releases",
        description="Open, uniformly processed human microbiome profiles as Parquet over HTTPS.",
        body=body, root="."))
    (root / "llms.txt").write_text(llms_txt(rels, featured, base))


def llms_txt(rels: list[tuple[dict, Release | None]], featured: Release | None, base: str) -> str:
    lines = ["# curatedMetagenomicData public releases", "",
             "> Uniformly processed human microbiome shotgun metagenome profiles (MetaPhlAn, Kraken2/Bracken, "
             "CARD resistome, QC, MetaPhlAn markers) as Parquet over HTTPS. No accounts, no credentials. "
             "The bucket cannot be listed: every file is reachable from an index below.", "",
             "## Datasets (latest release)", ""]
    for d, r in rels:
        extra = f", {r.n_samples} samples, {len(r.studies)} studies" if r else ""
        lines.append(f"- {d['id']} release {d['latest_release']}{extra}: "
                     f"{base}/{d['id']}/{d['latest_release']}/manifest.json")
    lines += ["", "## URL patterns", "",
              f"- dataset index: {base}/index.json",
              f"- newest release of a dataset: {base}/{{dataset}}/latest.json",
              f"- release manifest (tables, row counts, licence per table): {base}/{{dataset}}/{{release}}/manifest.json",
              f"- table Parquet: {base}/{{dataset}}/{{release}}/tables/{{table}}/data/part-00000.parquet",
              f"- DuckLake catalog: {base}/{{dataset}}/{{release}}/catalog.ducklake",
              f"- per-study files: {base}/{{dataset}}/{{release}}/studies/index.json "
              "and studies/{study}/index.html",
              f"- provenance (pipeline, databases, metadata sources): {base}/{{dataset}}/{{release}}/provenance.json",
              "", "## Join keys", "",
              "- sample_key / readset_id (RS.…): every table; samples has one row per sample",
              "- study_name: every table (curatedMetagenomicData study name)",
              "- run_accessions, biosample, bioproject: samples table",
              "- metadata tables (when present): sample_metadata_cmd, sample_metadata_olm, "
              "sample_metadata_harmonized, keyed by sample_key + field, with source and release per value",
              "", "## Profiles", "",
              "- taxonomic_profile_metaphlan: relative_abundance in percent; main pass = humann_bundle IS NULL; "
              "data_type full_data (all reads) or rarefied_data", "",
              "## Licence and citation", "",
              "- Tables are CC0-1.0 unless manifest.json says otherwise; Olm-derived metadata tables are "
              "CC-BY-4.0 and need attribution (see citation in provenance.json).",
              "- curatedMetagenomicData: Pasolli E et al. Nat Methods 2017;14:1023-1024. doi:10.1038/nmeth.4468"]
    if featured:
        u = featured.url
        lines += ["", f"## Tables in {featured.dataset} {featured.release}", ""]
        for name, t in sorted(featured.tables.items()):
            lines.append(f"- {name} ({t['row_count']} rows, {t['license']}): {u}/tables/{name}/data/part-00000.parquet")
        lines += ["", "## DuckDB examples", "", "```sql"]
        lines += [s + "\n" for s in _snippet_list(featured)]
        lines += ["```", "",
                  f"Studies and per-study pages: {u}/studies/index.json; {u}/index.html"]
    return "\n".join(lines) + "\n"
