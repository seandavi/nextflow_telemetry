"""Per-study downloads (#229) and the gene-family download index, written into a
release directory next to the tables (``publish.py``).

Public R2 can't LIST, so each artifact set has its own ``index.json``; paths in
it are relative to the release directory
(``https://cmgd-public.cancerdatasci.org/<dataset>/<release>/<path>``).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import duckdb

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

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
                  "MetaPhlAn pass on full_data; 0 = not detected. Samples with no species "
                  "rows are absent (see qc.tsv for every sample)."),
    "metaphlan.parquet": "taxonomic_profile_metaphlan rows for the study (long; all passes and branches).",
    "bracken.parquet": "taxonomic_profile_bracken rows for the study (long).",
    "resistome.parquet": "resistome rows for the study (long).",
    "pathways.parquet": "humann_pathabundance rows for the study (long; HUMAnN registrations only).",
    QC_TSV: "qc_metrics rows for the study: one row per sample, with run_ids.",
}


def _entry(release_dir: Path, path: Path) -> dict:
    with path.open("rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    return {"name": path.name, "path": path.relative_to(release_dir).as_posix(),
            "bytes": path.stat().st_size, "sha256": sha256}


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
        if con.sql("SELECT count(*) FROM species").fetchone()[0]:  # type: ignore[index]
            con.sql("PIVOT species ON sample_key USING first(relative_abundance) "
                    "GROUP BY clade_name ORDER BY clade_name").write_csv(
                str(d / SPECIES_TSV), sep="\t", na_rep="0", header=True, compression="gzip")
        con.sql(f"SELECT * FROM read_parquet('{snap / 'qc_metrics.parquet'}') "
                "WHERE study_name = $s ORDER BY sample_key", params={"s": study}).write_csv(
            str(d / QC_TSV), sep="\t", na_rep="", header=True)
    finally:
        con.close()


def write_studies(snap: Path, out: Path, dataset: str) -> None:
    """``<out>/<study>/…`` for every study in the snapshot, plus ``<out>/index.json``.
    ``snap`` holds one ``<table>.parquet`` per published table. ``out`` becomes the
    release's ``studies/`` directory."""
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
                      "files": [_entry(release_dir, p) for p in sorted(d.iterdir())]})
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(
        {"dataset": dataset, "file_descriptions": STUDY_FILE_DOCS, "studies": index}, indent=2))


GENEFAMILY_COLUMNS = ("study_name", "sample_key", "readset_id", "humann_bundle", "branch",
                      "key", "url", "bytes", "rows")


def write_genefamilies_index(files: duckdb.DuckDBPyRelation, out: Path, dataset: str,
                             raw_base_url: str | None) -> None:
    """``<out>/index.json`` and ``index.tsv``: one entry per sample gene-family file
    in cmgd-raw. ``key`` is the object key in cmgd-raw; ``url`` is
    ``<raw_base_url>/<key>``, or null while cmgd-raw has no public base URL.

    # ponytail: whole index in memory and in one JSON file (~250 B/sample, ~100 MB
    # at 400k samples); split per study if that gets in the way.
    """
    base = raw_base_url.rstrip("/") if raw_base_url else None
    cols = [c for c in GENEFAMILY_COLUMNS if c != "url"]
    rows = [dict(zip(cols, r)) for r in files.select(", ".join(cols)).fetchall()]
    for r in rows:
        r["url"] = f"{base}/{r['key']}" if base else None
    rows = [{c: r[c] for c in GENEFAMILY_COLUMNS} for r in rows]
    out.mkdir(parents=True)
    (out / "index.json").write_text(json.dumps(
        {"dataset": dataset, "raw_base_url": base,
         "description": "HUMAnN gene families: one unnormalized table per sample, downloaded "
                        "from cmgd-raw (not in the lake or the release tables).",
         "files": rows}, indent=2))
    with (out / "index.tsv").open("w") as f:
        f.write("\t".join(GENEFAMILY_COLUMNS) + "\n")
        for r in rows:
            f.write("\t".join("" if r[c] is None else str(r[c]) for c in GENEFAMILY_COLUMNS) + "\n")
