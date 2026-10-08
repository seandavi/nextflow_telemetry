"""Per-study downloads (#229) and the gene-family download index, written into a
release directory next to the tables (``publish.py``).

Public R2 can't LIST, so each artifact set has its own ``index.json``; paths in
it are relative to the release directory
(``https://cmgd-public.cancerdatasci.org/<dataset>/<release>/<path>``). Every
index carries ``spec_version`` (the cmgd index spec, ``INDEX_SPEC_VERSION``,
documented in docs/data-access.md; separate from cdsci-lake's manifest spec)
and names file sizes and checksums ``size``/``sha256`` like cdsci-lake's
``files.json``.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import duckdb

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# cmgd index spec: root index.json, studies/index.json, genefamilies/*.json.
# Bump the major on a breaking change (clients check it).
INDEX_SPEC_VERSION = "1.0"

SPECIES_TSV = "metaphlan_species.tsv.gz"
QC_TSV = "qc.tsv"
# Long per-study Parquet: file name -> source table. Written when the
# registration publishes the table.
STUDY_PARQUET = {
    "metaphlan.parquet": "taxonomic_profile_metaphlan",
    "bracken.parquet": "taxonomic_profile_bracken",
    "resistome.parquet": "resistome",
    "pathways.parquet": "humann_pathabundance",
}
STUDY_FILE_DOCS = {
    SPECIES_TSV: ("Species x samples matrix: one row per MetaPhlAn species clade (clade_name), "
                  "one column per sample_key; relative abundance in percent from the main "
                  "MetaPhlAn pass on full_data; 0 = not detected. Every sample in qc.tsv "
                  "has a column: a sample with no species-level rows is an all-zero column."),
    "metaphlan.parquet": "taxonomic_profile_metaphlan rows for the study (long; all passes and branches).",
    "bracken.parquet": "taxonomic_profile_bracken rows for the study (long).",
    "resistome.parquet": "resistome rows for the study (long).",
    "pathways.parquet": "humann_pathabundance rows for the study (long; HUMAnN registrations only).",
    QC_TSV: "qc_metrics rows for the study: one row per sample, with run_ids.",
}


def entry(release_dir: Path, path: Path) -> dict:
    with path.open("rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    return {"name": path.name, "path": path.relative_to(release_dir).as_posix(),
            "size": path.stat().st_size, "sha256": sha256}


def _study_files(snap: Path, d: Path, study: str) -> None:
    con = duckdb.connect()  # one in-memory DuckDB per study
    try:
        for name, table in STUDY_PARQUET.items():
            src = snap / f"{table}.parquet"
            if src.exists():
                con.execute(f"COPY (SELECT * FROM read_parquet('{src}') WHERE study_name = $s) "
                            f"TO '{d / name}' (FORMAT parquet, COMPRESSION zstd)", {"s": study})
        con.execute(
            "CREATE TABLE species AS SELECT sample_key, clade_name, relative_abundance "
            f"FROM read_parquet('{snap / 'taxonomic_profile_metaphlan.parquet'}') "
            "WHERE study_name = $s AND rank = 'species' AND data_type = 'full_data' "
            "AND humann_bundle IS NULL", {"s": study})
        qc = con.sql(f"SELECT * FROM read_parquet('{snap / 'qc_metrics.parquet'}') "
                     "WHERE study_name = $s ORDER BY sample_key", params={"s": study})
        qc.write_csv(str(d / QC_TSV), sep="\t", na_rep="", header=True)
        # One column per qc sample, so samples without species rows are all-zero columns.
        keys = ", ".join("'" + k.replace("'", "''") + "'"
                         for (k,) in qc.select("sample_key").fetchall())
        con.sql(f"PIVOT species ON sample_key IN ({keys}) USING first(relative_abundance) "
                "GROUP BY clade_name ORDER BY clade_name").write_csv(
            str(d / SPECIES_TSV), sep="\t", na_rep="0", header=True, compression="gzip")
    finally:
        con.close()


def write_studies(snap: Path, out: Path, dataset: str, artifacts: list[dict]) -> None:
    """``<out>/<study>/…`` for every study in the snapshot, plus ``<out>/index.json``.
    ``snap`` holds one ``<table>.parquet`` per published table. ``out`` becomes the
    release's ``studies/`` directory. ``artifacts`` (``entry`` dicts) are the
    release's other index files, listed with size and sha256 because
    ``publish_release`` can't put extra files in the manifest (cdsci-lake#134).

    One file for all studies: ~1 kB per study."""
    studies = duckdb.sql(
        f"SELECT study_name, count(DISTINCT sample_key) FROM read_parquet('{snap / 'qc_metrics.parquet'}') "
        "WHERE study_name IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
    release_dir = out.parent
    index = []
    for study, n_samples in studies:
        if not SAFE_NAME.match(study):
            raise ValueError(f"study name not safe as a path segment: {study!r}")
        d = out / study
        d.mkdir(parents=True)
        _study_files(snap, d, study)
        index.append({"study_name": study, "n_samples": n_samples,
                      "files": [entry(release_dir, p) for p in sorted(d.iterdir())]})
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(
        {"spec_version": INDEX_SPEC_VERSION, "dataset": dataset,
         "file_descriptions": STUDY_FILE_DOCS, "studies": index, "artifacts": artifacts},
        indent=2))


GENEFAMILY_COLUMNS = ("study_name", "sample_key", "readset_id", "humann_bundle", "branch",
                      "key", "url", "size", "sha256", "rows")


def write_genefamilies_index(files: duckdb.DuckDBPyRelation, out: Path, dataset: str,
                             raw_base_url: str | None) -> None:
    """Gene-family files in cmgd-raw, one per sample, indexed per study:
    ``<out>/<study>.json`` lists the study's files (``key`` is the object key in
    cmgd-raw; ``url`` is ``<raw_base_url>/<key>``, or null without a base;
    ``size``/``sha256``/``rows`` come from ingest). ``<out>/index.json`` lists
    the studies with their file's size and sha256, and ``index.tsv`` every file
    in one table, in ``files`` order (study, then sample)."""
    base = raw_base_url.rstrip("/") if raw_base_url else None
    cols = [c for c in GENEFAMILY_COLUMNS if c != "url"]
    by_study: dict[str, list[dict]] = {}
    for values in files.select(", ".join(cols)).fetchall():
        row = dict(zip(cols, values))
        row["url"] = f"{base}/{row['key']}" if base else None
        by_study.setdefault(row["study_name"], []).append({c: row[c] for c in GENEFAMILY_COLUMNS})
    out.mkdir(parents=True)
    release_dir = out.parent
    studies = []
    for study, rows in by_study.items():
        if not study or not SAFE_NAME.match(study):
            raise ValueError(f"gene-family file without a path-safe study: {study!r}")
        path = out / f"{study}.json"
        path.write_text(json.dumps(
            {"spec_version": INDEX_SPEC_VERSION, "dataset": dataset, "study_name": study,
             "raw_base_url": base, "files": rows}, indent=2))
        e = entry(release_dir, path)
        studies.append({"study_name": study, "n_samples": len({r["sample_key"] for r in rows}),
                        "n_files": len(rows), "path": e["path"], "size": e["size"],
                        "sha256": e["sha256"]})
    with (out / "index.tsv").open("w") as f:
        f.write("\t".join(GENEFAMILY_COLUMNS) + "\n")
        for rows in by_study.values():
            for r in rows:
                f.write("\t".join("" if r[c] is None else str(r[c]) for c in GENEFAMILY_COLUMNS) + "\n")
    (out / "index.json").write_text(json.dumps(
        {"spec_version": INDEX_SPEC_VERSION, "dataset": dataset, "raw_base_url": base,
         "description": "HUMAnN gene families: one unnormalized table per sample, downloaded "
                        "from cmgd-raw (not in the lake or the release tables). Integrity: "
                        "size and sha256 of each file, computed at ingest.",
         "studies": studies, "artifacts": [entry(release_dir, out / "index.tsv")]}, indent=2))
