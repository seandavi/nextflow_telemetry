"""Scoped, time-limited deliverables (#302): one registration x some collections,
as wide TSVs a requester can open without the lake.

Not a release (ADR-0011): no DOI, not immutable, and it may be deleted (an R2
lifecycle rule on ``deliverables/`` expires it; the README states the date).
The build reads the v2 jobs API and ``cmgd-raw`` (``source``), plus cMD's curated
sample metadata from ``lake.cmgd_meta`` when the lake is reachable. It never
touches the lake's result tables, ``releases/`` or ``index.json``.

  <out>/<name>/<YYYY-MM-DD>/  index.html, README.md, METHODS.md, samples.tsv.gz,
                              coverage.tsv.gz, metaphlan_<profile>.tsv.gz,
                              [humann4_*.tsv.gz, baqlava_vgb.tsv.gz], MANIFEST.tsv,
                              MANIFEST.sha256, <name>-<date>.tar.gz

Every table is gzipped with mtime 0, so the same data rebuilds to the same sha256.

Gene families stay per-sample files on cmgd-raw, linked from samples.tsv and
index.html. ``--sync`` copies that folder to ``<remote>/deliverables/<name>/<date>/``.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from pathlib import Path
from statistics import median
from typing import TextIO

import httpx

from . import engine, export, methods, parsers, publish, site, source
from .methods import md_table
from .specs import CAVEATS, CONTAINERS, DATABASES, DEFAULT_METAPHLAN_PROFILE, SPECS
from .v2 import Registration

PREFIX = "deliverables"
EXPIRY_DAYS = 90  # matches the proposed R2 lifecycle rule (monode terraform/apps/cmgd)
RUNNING = {"claimed", "submitted", "running"}
BAQLAVA_SUBPATH = "baqlava/{key}_BAQLaVa_profile.txt"
# cmgd-raw is public over HTTPS (monode#53); publish leaves its env var unset by default.
RAW_BASE = (publish.RAW_PUBLIC_BASE_URL or "https://cmgd-raw.cancerdatasci.org").rstrip("/")
RUN_URL = "https://cmgd.cancerdatasci.org/runs/{}"
# Host-dependent version strings that say nothing about comparability.
IGNORED_TOOLS = {"awscli"}
# Versions that name a pre-release (humann v4.0.0.alpha.2, baqlava v1.2.0-devel).
PRERELEASE = re.compile(r"alpha|beta|devel|rc\d|\da\d+$")
NOT_TOOLS = {"pipeline", "git_commit", "metaphlan_profile", "humann_bundle"}
# Columns that hold unassigned abundance, not a feature (HUMAnN).
UNASSIGNED = ("UNMAPPED", "UNINTEGRATED", "UNGROUPED")
# cMD curation fields put first in samples.tsv when present; the rest follow sorted.
FIRST_FIELDS = ("study_condition", "disease", "age", "age_years", "age_category", "sex",
                "country", "subject_id", "body_site")
QC_COLUMNS = ("reads_raw", "reads_decontaminated", "reads_surviving_fraction",
              "pipeline_version", "git_commit")
SAMPLE_COLUMNS = ("readset_id", "run_accessions", "collection", *QC_COLUMNS,
                  "qc_low_feature_tables", "git_revision", "run_name", "run_started",
                  "completed_at", "run_url", "raw_url", "genefamilies_url")
LOW_FEATURE_LIST = 20  # samples the low-feature caveat names; all are in samples.tsv.gz
REGISTRY_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"))


@dataclass(frozen=True)
class Output:
    file: str
    subpath: str  # relative to the sample's publish prefix; {key} = the sample key
    parser: Callable[[bytes], Iterator[dict]]
    feature: str
    value: str
    doc: str


def _relab(parser: Callable[[bytes], Iterator[dict]]) -> Callable[[bytes], Iterator[dict]]:
    """``parser``'s community totals rescaled to sum to 1 per sample, as
    ``humann_renorm_table --units relab`` does (UNMAPPED etc. in the denominator)."""
    def parse(raw: bytes) -> Iterator[dict]:
        rows = [r for r in parser(raw) if r.get("stratum") is None]
        total = sum(r["abundance"] for r in rows) or 1.0
        return iter([{**r, "abundance": r["abundance"] / total} for r in rows])
    return parse


def outputs(reg: Registration) -> list[Output]:
    """The wide tables a registration can produce: the main MetaPhlAn pass (full
    reads), the HUMAnN pathway tables from its specs (raw and relative), HUMAnN 4
    reactions, and BAQLaVa. A table is only written when some sample has the file."""
    profile = reg.params.get("metaphlan_profile", DEFAULT_METAPHLAN_PROFILE)
    out = []
    for s in SPECS[(reg.workflow_id, reg.version)]:
        if s.table == "taxonomic_profile_metaphlan" and s.branched:
            out.append(Output(
                f"metaphlan_{profile}.tsv.gz", f"full_data/{s.subpath}", s.parser, "clade_name",
                "relative_abundance",
                f"MetaPhlAn relative abundance in percent ({profile}, all quality-controlled "
                "reads, main pass), as reported (`rel_ab_w_read_stats`, not renormalized). "
                "Columns are clade names exactly as MetaPhlAn reports them: full lineages at "
                "every rank, so don't sum across ranks; pick one (e.g. species: `s__` without "
                "`t__`) and treat it as compositional."))
        elif s.table in ("humann_pathabundance", "humann_pathcoverage"):
            bundle = s.tags["humann_bundle"]
            kind = s.table.removeprefix("humann_")
            stem = f"{bundle.split('.')[0]}_{kind}"
            totals = ("Community totals only: the per-taxon strata (`FEATURE|s__…`) are "
                      "dropped; each sample's stratified tables are under its `raw_url`.")
            out.append(Output(
                f"{stem}.tsv.gz", s.subpath, s.parser, "pathway",
                "abundance" if kind == "pathabundance" else "coverage",
                f"HUMAnN {kind} ({bundle}), MetaCyc pathways in HUMAnN's native units, "
                "unnormalized: values scale with sequencing depth, so normalize before "
                f"comparing samples. {totals} UNMAPPED (reads matching no gene family) and "
                "UNINTEGRATED (gene families in no pathway) are kept as columns."))
            if kind == "pathabundance":
                out.append(Output(
                    f"{stem}_relab.tsv.gz", s.subpath.replace(".tsv.gz", "_relab_unstratified.tsv.gz"),
                    s.parser, "pathway", "abundance",
                    f"The pipeline's relative-abundance version of `{stem}.tsv.gz` "
                    "(`humann_renorm_table --units relab`): each row sums to 1 including "
                    "UNMAPPED and UNINTEGRATED; drop those and rescale for pathway-only "
                    f"proportions. {totals}"))
            if kind == "pathabundance" and bundle.startswith("humann4"):
                sub = f"humann/{bundle}/out_3_reactions_unstratified.tsv.gz"
                doc = (f"HUMAnN reactions ({bundle}): gene families grouped into reactions "
                       f"with the utility mapping. {totals}")
                out.append(Output(
                    f"{stem.removesuffix('_pathabundance')}_reactions.tsv.gz", sub,
                    parsers.parse_humann_reactions, "reaction", "abundance",
                    f"{doc} Native units, unnormalized. UNMAPPED (reads matching no gene "
                    "family) and UNGROUPED (gene families in no reaction) are kept as columns."))
                out.append(Output(
                    f"{stem.removesuffix('_pathabundance')}_reactions_relab.tsv.gz", sub,
                    _relab(parsers.parse_humann_reactions), "reaction", "abundance",
                    f"{doc} Rescaled here to sum to 1 per sample including UNMAPPED and "
                    "UNGROUPED (the pipeline writes no relab reactions table), as "
                    "`humann_renorm_table --units relab` does."))
    out.append(Output(
        "baqlava_vgb.tsv.gz", BAQLAVA_SUBPATH, parsers.parse_baqlava_profile, "vgb", "abundance",
        "BAQLaVa abundance in RPK (reads per kilobase) per viral genome bin (VGB): the "
        "total line, which is the larger of the nucleotide and translated estimates, not "
        "their sum; unnormalized. Input is only the reads HUMAnN's nucleotide search left "
        "unaligned, so reads in bacterial pangenomes (prophages included) aren't counted. "
        "Each VGB's species and taxonomy are in the per-sample profile under `raw_url`."))
    return out


def coverage(items: Iterable[dict], collections: Sequence[str],
             delivered: set[str]) -> list[dict]:
    """Per collection: registered (all jobs of the registration in it), completed,
    delivered (completed with outputs found in cmgd-raw), dead_lettered (status
    failed), pending and running (claimed/submitted/running)."""
    rows: dict[str, Counter] = {c: Counter() for c in collections}
    for i in items:
        for c in set(i.get("collections") or ()) & rows.keys():
            n, st = rows[c], i["status"]
            n["registered"] += 1
            n["dead_lettered" if st == "failed" else "running" if st in RUNNING else st] += 1
            n["delivered"] += _key(i) in delivered
    cols = ("registered", "completed", "delivered", "dead_lettered", "pending", "running")
    return [{"collection": c, **{k: rows[c][k] for k in cols}} for c in collections]


def wide(profiles: dict[str, dict[str, float]]) -> tuple[list[str], list[list]]:
    """samples x features: one row per sample (sorted), one column per feature
    seen in any sample (sorted), 0 where a sample doesn't report it."""
    features = sorted({f for p in profiles.values() for f in p})
    return (["readset_id", *features],
            [[k, *(profiles[k].get(f, 0) for f in features)] for k in sorted(profiles)])


def write_tsv(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> None:
    """A TSV, gzipped with mtime 0 (reproducible bytes) when ``path`` ends in ``.gz``."""
    with path.open("wb") as raw:
        gz = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) if path.suffix == ".gz" else None
        with io.TextIOWrapper(gz or raw, encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t", lineterminator="\n")
            w.writerow(header)
            w.writerows(["" if v is None else v for v in r] for r in rows)


def read_text(p: Path) -> TextIO:
    return gzip.open(p, "rt") if p.suffix == ".gz" else p.open()


def versions(manifests: Iterable[dict]) -> dict[str, Counter]:
    """tool -> Counter(version -> samples), from each sample's manifest.json:
    the pipeline version and commit, the MetaPhlAn profile / HUMAnN bundle, and
    every ``software_versions`` entry."""
    out: dict[str, Counter] = defaultdict(Counter)
    for m in manifests:
        prov, params = m.get("provenance") or {}, m.get("parameters") or {}
        for tool, v in {"pipeline": prov.get("pipeline_version"),
                        "git_commit": prov.get("git_commit"),
                        "metaphlan_profile": params.get("metaphlan_profile"),
                        "humann_bundle": params.get("humann_bundle"),
                        **(m.get("software_versions") or {})}.items():
            if v and tool not in IGNORED_TOOLS:
                out[tool][str(v)] += 1
    return dict(out)


def table_notes(header: Sequence[str], rows: Sequence[Sequence]) -> str:
    """What a built table says about itself: whether MetaPhlAn wrote an
    UNCLASSIFIED row, and the share of each HUMAnN unassigned column."""
    notes = []
    if any(h.startswith("k__") for h in header):
        notes.append(
            "Includes UNCLASSIFIED (the unknown fraction), so each rank sums to about 100 with "
            "it." if "UNCLASSIFIED" in header else
            "MetaPhlAn wrote no UNCLASSIFIED row for these samples, so each rank sums to about "
            "100 over the clades it could place; the unknown fraction is not represented.")
    for col in UNASSIGNED:
        if col in header:
            i = header.index(col)
            share = [r[i] / s * 100 for r in rows if (s := sum(r[1:]))]
            if share:
                notes.append(f"{col} is {min(share):.1f}–{max(share):.1f}% of the row total here.")
    return " ".join(notes)


def low_features(tables: dict[str, tuple[list[str], list[list]]]
                 ) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Per table (relab tables skipped: same non-zero features as the raw one) the
    median sample's non-zero column count; per sample the tables where it reports
    fewer than half that (samples.tsv's ``qc_low_feature_tables``)."""
    mids: dict[str, float] = {}
    low: dict[str, list[str]] = defaultdict(list)
    for f, (_, rows) in tables.items():
        if "_relab" in f:
            continue
        n = {r[0]: sum(1 for v in r[1:] if v) for r in rows}
        mids[f] = mid = median(n.values()) if n else 0
        for k, v in n.items():
            if v < 0.5 * mid:
                low[k].append(f)
    return mids, dict(low)


def caveats(reg: Registration, vers: dict[str, Counter], expires: date,
            mids: dict[str, float], low: dict[str, list[str]]) -> list[str]:
    """Generated caveats (pre-release tools, mixed versions, samples with few
    features: ``low_features``), then the registration's hand-written CAVEATS and
    the expiry."""
    out = []
    pre = sorted(f"{t} {v}" for t, c in vers.items() if t not in NOT_TOOLS
                 and not t.endswith("_image") for v in c if PRERELEASE.search(v))
    if pre:
        out.append(f"**Pre-release tools:** {', '.join(pre)}. Their outputs may change in final "
                   "releases; don't compare them directly with other versions.")
    mixed = sorted(t for t, c in vers.items() if len(c) > 1)
    if mixed:
        out.append(f"**Mixed versions:** {', '.join(f'{t} {methods.joined(vers[t])}' for t in mixed)}.")
    if low:
        per = Counter(f for fs in low.values() for f in fs)
        multi = sorted(k for k, fs in low.items() if len(fs) > 1)
        named = ", ".join(f"{k} ({', '.join(f'`{f}`' for f in low[k])})"
                          for k in multi[:LOW_FEATURE_LIST])
        more = f" and {len(multi) - LOW_FEATURE_LIST} more" if len(multi) > LOW_FEATURE_LIST else ""
        out.append(
            "**Few features:** samples reporting fewer than half the median sample's non-zero "
            "columns: " + "; ".join(f"{per[f]} in `{f}` (median {mids[f]:g})" for f in mids
                                    if per[f]) +
            ". Usually shallow or low-diversity samples rather than failures; each sample's "
            "flagged tables are in `samples.tsv.gz` (`qc_low_feature_tables`). "
            + (f"Flagged in two or more tables: {named}{more}; check these before analysis."
               if multi else "No sample is flagged in more than one table."))
    out += CAVEATS.get((reg.workflow_id, reg.version), [])
    out.append(f"**Not a release.** No DOI; expires on {expires}. For a permanent record cite a "
               "curatedMetagenomicData release once one covers these samples.")
    return out


def digest(image: str) -> str | None:
    """The digest a registry serves for an image tag today (anonymous pull token
    when it asks for one), or None on any failure. ponytail: a tag can move after
    the run; cMN#129 has the pipeline record the digest that ran."""
    try:
        host, _, rest = image.removeprefix("docker://").partition("/")
        repo, _, tag = rest.rpartition(":")
        url = f"https://{host}/v2/{repo}/manifests/{tag}"
        with httpx.Client(timeout=20) as c:
            r = c.head(url, headers={"Accept": REGISTRY_ACCEPT})
            if r.status_code == 401:
                auth = dict(re.findall(r'(\w+)="([^"]*)"', r.headers["www-authenticate"]))
                token = c.get(auth.pop("realm"), params=auth).json()["token"]
                r = c.head(url, headers={"Accept": REGISTRY_ACCEPT,
                                         "Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return r.headers["docker-content-digest"]
    except Exception:  # noqa: BLE001 -- "not recorded" beats failing the build
        return None


def containers(reg: Registration, manifests: Sequence[dict], today: date) -> list[list[str]]:
    """[steps, image, digest] per image: the manifest's base image and
    ``software_versions.*_image``, plus the registration's CONTAINERS."""
    steps: dict[str, list[str]] = defaultdict(list)
    for m in manifests:
        base = (m.get("provenance") or {}).get("container")
        if base:
            steps[base.removeprefix("docker://")].append("base image (read download, QC, KneadData)")
        for k, v in (m.get("software_versions") or {}).items():
            if k.endswith("_image") and v:
                steps[v.removeprefix("docker://")].append(k.removesuffix("_image"))
    for step, image in CONTAINERS.get((reg.workflow_id, reg.version), {}).items():
        steps[image].append(step)
    out = []
    for image, s in sorted(steps.items()):
        d = digest(image)
        out.append([", ".join(dict.fromkeys(s)), image,
                    f"{d} (tag resolved on {today})" if d else "not recorded"])
    return out


def sample_summary(samples: Sequence[dict], cov: list[dict]) -> list[list[str]]:
    """README's and index.html's stand-in for a per-sample table, so their size
    doesn't grow with the sample count (Markdown cells)."""
    reads = [float(s["reads_decontaminated"]) for s in samples if s.get("reads_decontaminated")]
    revs = Counter(str(s.get("pipeline_version") or "unknown") for s in samples)
    flagged = sum(1 for s in samples if s.get("qc_low_feature_tables"))
    return [
        ["samples", f"{len(samples)} delivered, one row each in `samples.tsv.gz`"],
        ["per collection (delivered / completed / registered)",
         "; ".join(f"{c['collection']} {c['delivered']} / {c['completed']} / {c['registered']}"
                   for c in cov)],
        ["reads after QC and host removal",
         f"median {median(reads) / 1e6:.1f} million (range {min(reads) / 1e6:.1f}–"
         f"{max(reads) / 1e6:.1f} million)" if reads else "not recorded"],
        ["pipeline version", methods.joined(revs) if revs else "–"],
        ["few-features flag", f"{flagged} samples (`qc_low_feature_tables`; see Caveats)"],
    ]


def readme(name: str, today: date, reg: Registration, cov: list[dict],
           vers: dict[str, Counter], files: list[tuple[str, str]],
           meta_releases: Sequence[str], notes: Sequence[str] = (),
           samples: Sequence[dict] = ()) -> str:
    expires = today + timedelta(days=EXPIRY_DAYS)
    mixed = sorted(t for t, c in vers.items() if len(c) > 1)
    comparability = (
        f"**Mixed versions:** {', '.join(mixed)} differ between samples (see the table). "
        "Check that a difference matters for your analysis before pooling those samples."
        if mixed else
        "Every delivered sample ran the same pipeline commit, tools and reference databases, "
        "so the samples are directly comparable with each other.")
    dbs = DATABASES.get((reg.workflow_id, reg.version))
    meta = (f"cMD curation commit {', '.join(meta_releases)} (the pinned release in "
            "`lake.cmgd_meta`)" if meta_releases else
            "**not included**: the metadata store was unreachable when this was built")
    return f"""# curatedMetagenomicData deliverable: {name} ({today})

**This is not a release.** It has no DOI, it is not immutable and it **may be
deleted**: it expires on **{expires}** ({EXPIRY_DAYS} days after it was built). For
citable, durable data use the releases at {publish.PUBLIC_BASE_URL}/.

`METHODS.md` has paste-ready Methods text, how to read each table and what to cite.

## Scope

- Registration: `{reg.workflow_id}/{reg.version}` (registration id {reg.pk})
- Pipeline: {reg.repository_url or 'curatedMetagenomicsNextflow'}; the registration's current
  revision is `{reg.revision or 'unknown'}`, but each sample's own revision is in `samples.tsv.gz`
  (`pipeline_version`, `git_commit`) and the Versions table below.
- Collections: {', '.join(c['collection'] for c in cov)}
- Samples: completed jobs only; `coverage.tsv.gz` says how many of each collection that is.

{md_table(list(cov[0]), [list(c.values()) for c in cov]) if cov else ''}

## Samples

{md_table(["", ""], sample_summary(samples, cov))}

Per-sample runs, read counts, links and metadata are in `samples.tsv.gz`; file
checksums and dimensions in `MANIFEST.tsv`.

## Versions

Read from each delivered sample's `manifest.json` (number of samples per version):

{md_table(["tool", "version (samples)"],
          [[t, "; ".join(f"{v} ({n})" for v, n in sorted(c.items()))] for t, c in sorted(vers.items())])}

Reference databases pinned for the registration: {', '.join(f'{k} `{v}`' for k, v in dbs.items()) if dbs else 'not recorded'}.

## Comparability

{comparability} Results from other registrations (other MetaPhlAn databases, HUMAnN
bundles or pipeline versions) are not directly comparable with these.

## Caveats

{chr(10).join(f"{i}. {c}" for i, c in enumerate(notes, 1)) or 'None recorded.'}

## Files

{md_table(["file", "contents"], files)}

Every table is a gzipped TSV (`.tsv.gz`, tab-separated with a header row; `zcat` it, or
read it directly with R or pandas). The profile tables are samples x features: one row
per sample (`readset_id`), one column per feature.

## Columns of samples.tsv.gz

- `readset_id`: the sample's readset id (RS.…), the row key of every table here.
- `run_accessions`: semicolon-separated SRA/ENA runs the sample was built from.
- `collection`: the requested collection(s) (study) the sample belongs to.
- `reads_raw`, `reads_decontaminated`, `reads_surviving_fraction`: reads before and
  after host decontamination, and their ratio.
- `qc_low_feature_tables`: comma-separated tables in which the sample reports fewer than
  half the median sample's non-zero columns; empty when none.
- `pipeline_version`, `git_commit`, `git_revision`: the pipeline that produced the sample.
- `run_name`, `run_started`, `completed_at`, `run_url`: the Nextflow run, when it started,
  when the job completed, and its page on the dashboard.
- `raw_url`: the sample's folder on cmgd-raw (every per-sample output; not listable, so
  append a path such as `manifest.json`). `genefamilies_url`: its HUMAnN gene families,
  unstratified (`_relab_unstratified`, `_stratified` and `_cpm` siblings sit next to it).
- Every further column is a curatedMetagenomicData curation field, named and valued
  exactly as the curation sheet gives it (several values are `;`-joined). Source:
  {meta}.

## coverage.tsv.gz

`registered`: jobs of this registration in the collection; `completed`: finished;
`delivered`: completed and found in the output store (only these are in the tables);
`dead_lettered`: failed past their retry budget; `pending`: not started; `running`:
claimed, submitted or running.

## How to cite

{publish.CITATION["cmgd"]}

Cite the tools too: `METHODS.md` section 5.
"""


def _inline(md: str) -> str:
    """The Markdown this module writes (**bold**, `code`) as HTML."""
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(md, quote=False))
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)


def index_html(name: str, today: date, reg: Registration, cov: list[dict], d: Path,
               files: list[tuple[str, str]], paragraphs: Sequence[str],
               vers: dict[str, Counter], conts: list[list[str]], notes: Sequence[str],
               samples: list[dict]) -> str:
    """The landing page: the README's and METHODS' content, every file linked
    explicitly (cmgd-public can't be listed) and a summary of the samples; the
    per-sample run/raw links are in samples.tsv.gz, so the page stays small."""
    expires = today + timedelta(days=EXPIRY_DAYS)
    dbs = DATABASES.get((reg.workflow_id, reg.version)) or {}

    def link(url: str | None, text: str) -> str:
        return f'<a href="{escape(url)}">{escape(text)}</a>' if url else "–"

    body = [
        f'<header class="top"><div class="crumbs">curatedMetagenomicData deliverable</div>'
        f'<h1>{escape(name)} ({today})</h1>'
        f'<p class="lede">Registration <code>{escape(reg.workflow_id)}/{escape(reg.version)}'
        f'</code>, {len(samples)} samples from {escape(", ".join(c["collection"] for c in cov))}.'
        "</p></header>",
        f'<p class="note"><b>Not a release.</b> No DOI, not immutable, and it expires on '
        f'<b>{expires}</b>. For citable data use the <a href="{publish.PUBLIC_BASE_URL}/">'
        "releases</a>.</p>",
        "<h2>Files</h2>",
        site._table(["file", "size", "contents"],
                    [[link(f, f), site._size((d / f).stat().st_size if (d / f).exists() else None),
                      _inline(doc)] for f, doc in files], num={1}),
        "<h2>Coverage</h2>",
        site._table(list(cov[0]), [[escape(str(v)) for v in c.values()] for c in cov],
                    num=set(range(1, len(cov[0])))) if cov else "",
        "<h2>Methods text</h2>",
        *(f"<blockquote><p>{_inline(p)}</p></blockquote>" for p in paragraphs),
        "<h2>Software</h2>",
        site._table(["tool", "version (samples)"],
                    [[escape(t), escape(methods.joined(c))] for t, c in sorted(vers.items())]),
        "<h3>Containers</h3>",
        site._table(["step", "image", "digest"], [[escape(v) for v in r] for r in conts]),
        "<h3>Reference databases</h3>",
        site._table(["database", "version"], [[escape(k), escape(v)] for k, v in dbs.items()])
        if dbs else "<p>Not recorded for this registration.</p>",
        "<h2>Caveats</h2>",
        "<ol>" + "".join(f"<li>{_inline(c)}</li>" for c in notes) + "</ol>",
        "<h2>Samples</h2>",
        site._table(["", ""], [[_inline(k), _inline(v)] for k, v in sample_summary(samples, cov)]),
        f"<p>Per-sample runs, read counts, run and cmgd-raw links and metadata: "
        f"{link('samples.tsv.gz', 'samples.tsv.gz')}. Checksums and dimensions: "
        f"{link('MANIFEST.tsv', 'MANIFEST.tsv')}.</p>",
        "<h2>How to cite</h2>",
        site._table(["tool / database", "citation"],
                    [[escape(n), escape(c)] for n, c in methods.citations(vers)]),
    ]
    return site.PAGE.substitute(title=escape(f"{name} deliverable"),
                                description=escape(f"cMD deliverable {name}, {today}"),
                                body="\n".join(body), root="../../..")


def write_manifest(d: Path, name: str, today: date) -> None:
    """``MANIFEST.tsv`` (file, bytes, sha256, rows, cols; rows exclude the header,
    cols include ``readset_id``, counted inside a ``.tsv.gz``; blank for non-TSVs;
    bytes and sha256 are of the file as shipped) and ``MANIFEST.sha256``
    (``sha256sum -c`` format) over every file, then ``<name>-<date>.tar.gz`` of
    them all, manifests included."""
    skip = {"MANIFEST.sha256", "MANIFEST.tsv"}
    files = sorted(p for p in d.iterdir() if p.is_file() and p.name not in skip
                   and not p.name.endswith(".tar.gz"))

    def sha(p: Path) -> str:
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    rows = []
    for p in files:
        n = c = None
        if p.name.endswith((".tsv", ".tsv.gz")):
            with read_text(p) as f:
                c = f.readline().count("\t") + 1
                n = sum(1 for _ in f)
        rows.append([p.name, p.stat().st_size, sha(p), n, c])
    write_tsv(d / "MANIFEST.tsv", ["file", "bytes", "sha256", "rows", "cols"], rows)
    files = sorted([*files, d / "MANIFEST.tsv"])
    (d / "MANIFEST.sha256").write_text("".join(f"{sha(p)}  {p.name}\n" for p in files))
    with tarfile.open(d / f"{name}-{today}.tar.gz", "w:gz") as t:
        for p in [*files, d / "MANIFEST.sha256"]:
            t.add(p, arcname=f"{name}-{today}/{p.name}")


def _key(item: dict) -> str:
    # ponytail: deliverables key on the readset id (2.3.0+ registrations); a 2.2.1
    # job without one falls back to its md5 sample_key.
    return item.get("readset_id") or item["sample_key"]


def _collect(reg: Registration, item: dict, outs: list[Output]):
    """(manifest bytes, {file: {feature: value}}, prefix) for one sample; None if
    unpublished. Each subpath is fetched once (a relab table reuses its source)."""
    prefix = source.locate(reg.workflow_id, reg.version, item["sample_key"])
    manifest = source.fetch(prefix, "manifest.json") if prefix else None
    if prefix is None or manifest is None:
        return None
    fetched: dict[str, bytes | None] = {}
    profiles = {}
    for o in outs:
        sub = o.subpath.format(key=item["sample_key"])
        if sub not in fetched:
            fetched[sub] = source.fetch(prefix, sub)
        if data := fetched[sub]:  # HUMAnN strata are dropped: community totals only
            profiles[o.file] = {r[o.feature]: r[o.value] for r in o.parser(data)
                                if r.get("stratum") is None}
    return manifest, profiles, prefix


def metadata(samples: list[tuple[str, str, str]]) -> tuple[dict[str, dict[str, set]], list[str]]:
    """cMD curated metadata per readset (``samples``: (study_name, key, readset_id)),
    with publish's ``sample_metadata_cmd`` query; and the cMD releases read. Empty,
    with a warning, when the lake isn't reachable: metadata is optional here."""
    try:
        from cdsci.lake import lake_connect  # type: ignore[import-untyped]

        con = lake_connect(read_only=True)
        try:
            con.execute("CREATE TEMP TABLE deliver_samples "
                        "(study_name VARCHAR, sample_key VARCHAR, readset_id VARCHAR)")
            con.executemany("INSERT INTO deliver_samples VALUES (?, ?, ?)", samples)
            sql = publish.META_TABLES["sample_metadata_cmd"][1].format(
                meta=f"lake.{publish.META_SCHEMA}", samples="deliver_samples")
            rows = con.sql(f"SELECT sample_key, source_field, value_raw, release FROM ({sql})"
                           ).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 -- any lake failure means "no metadata"
        print(f"warning: samples.tsv has no curated metadata (lake.cmgd_meta: {e})",
              file=sys.stderr)
        return {}, []
    out: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for key, field, value, _ in rows:
        if value not in (None, ""):
            out[key][field].add(value)
    return out, sorted({r[3] for r in rows})


def _links(reg: Registration, item: dict, prefix: str, manifest: dict) -> dict:
    """samples.tsv's run and cmgd-raw columns for one sample."""
    prov = manifest.get("provenance") or {}
    raw = (f"{RAW_BASE}{prefix.removeprefix('r2:cmgd-raw')}/"
           if prefix.startswith("r2:cmgd-raw/") else None)
    gf = next((s.subpath for s in SPECS[(reg.workflow_id, reg.version)]
               if s.table == publish.GENEFAMILY_FILES_TABLE), None)
    return {"git_revision": prov.get("git_revision"), "run_name": prov.get("run_name"),
            "run_started": prov.get("start_time"), "completed_at": item.get("completed_at"),
            "run_url": RUN_URL.format(prov["run_name"]) if prov.get("run_name") else None,
            "raw_url": raw,
            "genefamilies_url": (f"{raw}{gf.replace('.tsv.gz', '_unstratified.tsv.gz')}"
                                 if raw and gf else None)}


def build(reg: Registration, items: list[dict], collections: Sequence[str], name: str,
          out: Path | str, today: date | None = None) -> Path:
    """Write the deliverable into ``<out>/<name>/<date>/`` (replacing a same-day
    build) and return that directory."""
    for n in (name, *collections):
        if not export.SAFE_NAME.match(n):
            raise ValueError(f"bad name {n!r}")
    today = today or date.today()
    expires = today + timedelta(days=EXPIRY_DAYS)
    d = Path(out) / name / today.isoformat()
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    wanted = set(collections)
    picked = [i for i in items
              if i["status"] == "completed" and wanted & set(i.get("collections") or ())]
    outs = outputs(reg)
    with ThreadPoolExecutor(engine.FETCH_WORKERS) as pool:
        got = {_key(i): (i, r) for i, r in zip(picked, pool.map(lambda i: _collect(reg, i, outs),
                                                               picked)) if r}

    files, tables = [], {}
    for o in outs:
        profiles = {k: r[1][o.file] for k, (_, r) in got.items() if o.file in r[1]}
        if not profiles:
            if got and (o.file != "baqlava_vgb.tsv.gz" or reg.params.get("skip_baqlava") is False):
                print(f"warning: no sample has {o.subpath}; {o.file} not written",
                      file=sys.stderr)
            continue
        header, rows = tables[o.file] = wide(profiles)
        write_tsv(d / o.file, header, rows)
        files.append((o.file, f"{o.doc} {table_notes(header, rows)} {len(profiles)} samples."
                      .replace("  ", " ")))

    in_scope = {k: sorted(wanted & set(i["collections"])) for k, (i, _) in got.items()}
    meta, meta_releases = metadata([(cs[0], k, k) for k, cs in in_scope.items()])
    fields = sorted({f for m in meta.values() for f in m},
                    key=lambda f: (FIRST_FIELDS.index(f) if f in FIRST_FIELDS else len(FIRST_FIELDS), f))
    mids, low = low_features(tables)
    samples, qc_rows, manifests = [], [], []
    for k, (i, (raw, _, prefix)) in sorted(got.items(), key=lambda kv: (in_scope[kv[0]], kv[0])):
        manifest = json.loads(raw)
        qc = next(parsers.parse_qc(raw), {})
        manifests.append(manifest)
        qc_rows.append(qc)
        samples.append({"readset_id": k, "run_accessions": i.get("ncbi_accession"),
                        "collection": ";".join(in_scope[k]),
                        **{c: qc.get(c) for c in QC_COLUMNS},
                        "qc_low_feature_tables": ",".join(low.get(k, ())),
                        **_links(reg, i, prefix, manifest),
                        **{f: ";".join(sorted(meta.get(k, {}).get(f, ()))) for f in fields}})
    write_tsv(d / "samples.tsv.gz", [*SAMPLE_COLUMNS, *fields],
              [list(s.values()) for s in samples])
    cov = coverage(items, collections, set(got))
    write_tsv(d / "coverage.tsv.gz", list(cov[0]), [list(c.values()) for c in cov])
    profile_files = list(files)
    files += [("samples.tsv.gz", "One row per delivered sample: runs, collection, read counts, "
                                 "few-features QC flag, pipeline revision, run and cmgd-raw "
                                 "links, curated sample metadata."),
              ("coverage.tsv.gz", "Per collection: how many samples are registered, completed, "
                                  "delivered, dead-lettered, pending and running."),
              ("index.html", "This deliverable as a web page, every file linked."),
              ("README.md", "Scope, versions, caveats and column definitions."),
              ("METHODS.md", "Methods text, software/containers/databases, how to read each "
                             "table, caveats and citations."),
              ("MANIFEST.tsv", "Per file: bytes, sha256, rows (excluding the header) and "
                               "columns (TSVs only)."),
              ("MANIFEST.sha256", "sha256 of every file (`sha256sum -c MANIFEST.sha256`)."),
              (f"{name}-{today}.tar.gz", "Everything above in one archive.")]
    vers = versions(manifests)
    dbs = DATABASES.get((reg.workflow_id, reg.version)) or {}
    notes = caveats(reg, vers, expires, mids, low)
    paragraphs = methods.prose(manifests, vers, dbs, qc_rows)
    conts = containers(reg, manifests, today)
    (d / "README.md").write_text(readme(name, today, reg, cov, vers, files, meta_releases, notes,
                                              samples))
    (d / "METHODS.md").write_text(methods.render(name, today, expires, reg, cov, len(samples),
                                                 paragraphs, vers, conts, dbs, profile_files,
                                                 notes))
    (d / "index.html").write_text(index_html(name, today, reg, cov, d, files, paragraphs, vers,
                                             conts, notes, samples))
    write_manifest(d, name, today)
    return d


def sync_commands(d: Path, name: str, remote: str = publish.SYNC_REMOTE) -> list[list[str]]:
    """Copy one built deliverable to ``<remote>/deliverables/<name>/<date>/``. Not
    ``--immutable``: a same-day rebuild replaces its files."""
    if not export.SAFE_NAME.match(name):
        raise ValueError(f"bad name {name!r}")
    return [["rclone", "copy", str(d), f"{remote}/{PREFIX}/{name}/{d.name}"]]


def sync(d: Path, name: str, remote: str = publish.SYNC_REMOTE, dry_run: bool = False) -> None:
    for cmd in sync_commands(d, name, remote):
        print(" ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True)
