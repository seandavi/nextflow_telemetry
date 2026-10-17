"""Scoped, time-limited deliverables (#302): one registration x some collections,
as wide TSVs a requester can open without the lake.

Not a release (ADR-0011): no DOI, not immutable, and it may be deleted (an R2
lifecycle rule on ``deliverables/`` expires it; the README states the date).
The build reads the v2 jobs API and ``cmgd-raw`` (``source``), plus cMD's curated
sample metadata from ``lake.cmgd_meta`` when the lake is reachable. It never
touches the lake's result tables, ``releases/`` or ``index.json``.

  <out>/<name>/<YYYY-MM-DD>/  metaphlan_<profile>.tsv, [humann4_pathabundance.tsv,
                              humann4_pathcoverage.tsv, baqlava_vgb.tsv], samples.tsv,
                              coverage.tsv, README.md, MANIFEST.sha256, <name>-<date>.tar.gz

``--sync`` copies that folder to ``<remote>/deliverables/<name>/<date>/``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import engine, export, parsers, publish, source
from .specs import DATABASES, DEFAULT_METAPHLAN_PROFILE, SPECS
from .v2 import Registration

PREFIX = "deliverables"
EXPIRY_DAYS = 90  # matches the proposed R2 lifecycle rule (monode terraform/apps/cmgd)
RUNNING = {"claimed", "submitted", "running"}
# ponytail: guessed path; no pipeline version runs BAQLaVa yet. Fix it (or move it
# into specs.py) when the 2.4.0 registration publishes it.
BAQLAVA_SUBPATH = "baqlava/out_BAQLaVa_profile.txt"
# Host-dependent version strings that say nothing about comparability.
IGNORED_TOOLS = {"awscli"}
# cMD curation fields put first in samples.tsv when present; the rest follow sorted.
FIRST_FIELDS = ("study_condition", "disease", "age", "age_years", "age_category", "sex",
                "country", "subject_id", "body_site")
SAMPLE_COLUMNS = ("readset_id", "run_accessions", "collection", "reads_raw",
                  "reads_decontaminated", "reads_surviving_fraction", "pipeline_version",
                  "git_commit")


@dataclass(frozen=True)
class Output:
    file: str
    subpath: str  # relative to the sample's publish prefix
    parser: Callable[[bytes], Iterator[dict]]
    feature: str
    value: str
    doc: str


def outputs(reg: Registration) -> list[Output]:
    """The wide tables a registration can produce: the main MetaPhlAn pass (full
    reads) and the HUMAnN pathway tables from its specs, plus BAQLaVa. A table is
    only written when some sample has the file."""
    profile = reg.params.get("metaphlan_profile", DEFAULT_METAPHLAN_PROFILE)
    out = []
    for s in SPECS[(reg.workflow_id, reg.version)]:
        if s.table == "taxonomic_profile_metaphlan" and s.branched:
            out.append(Output(
                f"metaphlan_{profile}.tsv", f"full_data/{s.subpath}", s.parser, "clade_name",
                "relative_abundance",
                f"MetaPhlAn relative abundance (percent; {profile}, all reads, main pass). "
                "Columns are clade names exactly as MetaPhlAn reports them (full lineage, "
                "every rank, plus UNCLASSIFIED); 0 = not detected."))
        elif s.table in ("humann_pathabundance", "humann_pathcoverage"):
            # ponytail: HUMAnN 4 file names come from specs.py; untested on 2.4.0
            # output until that registration has a spec and completed samples.
            kind = s.table.removeprefix("humann_")
            out.append(Output(
                f"{s.tags['humann_bundle'].split('.')[0]}_{kind}.tsv", s.subpath, s.parser,
                "pathway", "abundance" if kind == "pathabundance" else "coverage",
                f"HUMAnN {kind} ({s.tags['humann_bundle']}), community totals only (no "
                "per-taxon strata), unnormalized in the bundle's units; columns are pathways "
                "plus UNMAPPED/UNINTEGRATED; 0 = not reported."))
    out.append(Output(
        "baqlava_vgb.tsv", BAQLAVA_SUBPATH, parsers.parse_baqlava_profile, "vgb", "abundance",
        "BAQLaVa viral genome bin (VGB) total abundance (RPKs; the max of the nucleotide and "
        "translated searches); 0 = not detected."))
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
    with path.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        w.writerows(["" if v is None else v for v in r] for r in rows)


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


def _md_table(header: Sequence[str], rows: Iterable[Sequence]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def readme(name: str, today: date, reg: Registration, cov: list[dict],
           vers: dict[str, Counter], files: list[tuple[str, str]],
           meta_releases: Sequence[str]) -> str:
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

## Scope

- Registration: `{reg.workflow_id}/{reg.version}` (registration id {reg.pk})
- Pipeline: {reg.repository_url or 'curatedMetagenomicsNextflow'} at revision `{reg.revision or 'unknown'}`
- Collections: {', '.join(c['collection'] for c in cov)}
- Samples: completed jobs only; `coverage.tsv` says how many of each collection that is.

{_md_table(list(cov[0]), [list(c.values()) for c in cov]) if cov else ''}

## Versions

Read from each delivered sample's `manifest.json` (number of samples per version):

{_md_table(["tool", "version (samples)"],
           [[t, "; ".join(f"{v} ({n})" for v, n in sorted(c.items()))] for t, c in sorted(vers.items())])}

Reference databases pinned for the registration: {', '.join(f'{k} `{v}`' for k, v in dbs.items()) if dbs else 'not recorded'}.

## Comparability

{comparability} Results from other registrations (other MetaPhlAn databases, HUMAnN
bundles or pipeline versions) are not directly comparable with these.

## Files

{_md_table(["file", "contents"], files)}

All TSVs are tab-separated with a header row. The profile tables are samples x
features: one row per sample (`readset_id`), one column per feature.

## Columns of samples.tsv

- `readset_id`: the sample's readset id (RS.…), the row key of every table here.
- `run_accessions`: semicolon-separated SRA/ENA runs the sample was built from.
- `collection`: the requested collection(s) (study) the sample belongs to.
- `reads_raw`, `reads_decontaminated`, `reads_surviving_fraction`: reads before and
  after host decontamination, and their ratio.
- `pipeline_version`, `git_commit`: the pipeline that produced the sample.
- Every further column is a curatedMetagenomicData curation field, named and valued
  exactly as the curation sheet gives it (several values are `;`-joined). Source:
  {meta}.

## coverage.tsv

`registered`: jobs of this registration in the collection; `completed`: finished;
`delivered`: completed and found in the output store (only these are in the tables);
`dead_lettered`: failed past their retry budget; `pending`: not started; `running`:
claimed, submitted or running.
"""


def write_manifest(d: Path, name: str, today: date) -> None:
    """``MANIFEST.sha256`` (``sha256sum -c`` format) over every file, then
    ``<name>-<date>.tar.gz`` of them all, manifest included."""
    files = sorted(p for p in d.iterdir() if p.is_file() and p.name != "MANIFEST.sha256"
                   and not p.name.endswith(".tar.gz"))
    lines = []
    for p in files:
        with p.open("rb") as f:
            lines.append(f"{hashlib.file_digest(f, 'sha256').hexdigest()}  {p.name}\n")
    (d / "MANIFEST.sha256").write_text("".join(lines))
    with tarfile.open(d / f"{name}-{today}.tar.gz", "w:gz") as t:
        for p in [*files, d / "MANIFEST.sha256"]:
            t.add(p, arcname=f"{name}-{today}/{p.name}")


def _key(item: dict) -> str:
    # ponytail: deliverables key on the readset id (2.3.0+ registrations); a 2.2.1
    # job without one falls back to its md5 sample_key.
    return item.get("readset_id") or item["sample_key"]


def _collect(reg: Registration, item: dict, outs: list[Output]):
    """(manifest bytes, {file: {feature: value}}) for one sample; None if unpublished."""
    prefix = source.locate(reg.workflow_id, reg.version, item["sample_key"])
    manifest = source.fetch(prefix, "manifest.json") if prefix else None
    if prefix is None or manifest is None:
        return None
    profiles = {}
    for o in outs:
        data = source.fetch(prefix, o.subpath)
        if data:  # HUMAnN strata are dropped: community totals only
            profiles[o.file] = {r[o.feature]: r[o.value] for r in o.parser(data)
                                if r.get("stratum") is None}
    return manifest, profiles


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


def build(reg: Registration, items: list[dict], collections: Sequence[str], name: str,
          out: Path | str, today: date | None = None) -> Path:
    """Write the deliverable into ``<out>/<name>/<date>/`` (replacing a same-day
    build) and return that directory."""
    for n in (name, *collections):
        if not export.SAFE_NAME.match(n):
            raise ValueError(f"bad name {n!r}")
    today = today or date.today()
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

    files = []
    for o in outs:
        profiles = {k: r[1][o.file] for k, (_, r) in got.items() if o.file in r[1]}
        if profiles:
            write_tsv(d / o.file, *wide(profiles))
            files.append((o.file, f"{o.doc} {len(profiles)} samples."))

    in_scope = {k: sorted(wanted & set(i["collections"])) for k, (i, _) in got.items()}
    meta, meta_releases = metadata([(cs[0], k, k) for k, cs in in_scope.items()])
    fields = sorted({f for m in meta.values() for f in m},
                    key=lambda f: (FIRST_FIELDS.index(f) if f in FIRST_FIELDS else len(FIRST_FIELDS), f))
    rows = []
    for k, (i, (manifest, _)) in sorted(got.items(), key=lambda kv: (in_scope[kv[0]], kv[0])):
        qc = next(parsers.parse_qc(manifest), {})
        rows.append([k, i.get("ncbi_accession"), ";".join(in_scope[k]),
                     *(qc.get(c) for c in SAMPLE_COLUMNS[3:]),
                     *(";".join(sorted(meta.get(k, {}).get(f, ()))) for f in fields)])
    write_tsv(d / "samples.tsv", [*SAMPLE_COLUMNS, *fields], rows)
    cov = coverage(items, collections, set(got))
    write_tsv(d / "coverage.tsv", list(cov[0]), [list(c.values()) for c in cov])
    files += [("samples.tsv", "One row per delivered sample: runs, collection, read counts, "
                              "pipeline version and curated sample metadata."),
              ("coverage.tsv", "Per collection: how many samples are registered, completed, "
                               "delivered, dead-lettered, pending and running."),
              ("MANIFEST.sha256", "sha256 of every file (`sha256sum -c MANIFEST.sha256`)."),
              (f"{name}-{today}.tar.gz", "Everything above in one archive.")]
    vers = versions(json.loads(m) for _, (m, _) in got.values())
    (d / "README.md").write_text(readme(name, today, reg, cov, vers, files, meta_releases))
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
