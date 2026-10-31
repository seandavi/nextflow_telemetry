"""Public releases of ``lake.cmgd`` (ADR-0011): one dataset per registration.

``publish`` reads one registration's rows from the shared lake at a single lake
snapshot and hands them to cdsci-lake's ``publish_release``, which writes the
release (sorted Parquet, schema/file indexes, frozen read-only DuckLake catalog,
manifest, ``releases.json``/``latest.json``) into a local store. The per-study
downloads and the gene-family index (``export.py``) are built from the same
snapshot and moved into the release directory once ``publish_release`` returns:
it has no hook for extra artifacts, so they are not in the manifest (their own
``index.json`` files carry sizes and checksums; cdsci-lake#134). Last, the
store's root ``index.json`` gets the dataset's entry, so clients can find every
dataset without LIST.

``sync`` is the separate, explicit upload of a local dataset to ``cmgd-public``.

  <root>/index.json                                    every dataset in the store
  <root>/<dataset>/releases.json, latest.json
  <root>/<dataset>/<release>/manifest.json, catalog.ducklake, tables/<table>/...
  <root>/<dataset>/<release>/studies/index.json, studies/<study>/...
  <root>/<dataset>/<release>/genefamilies/index.json, <study>.json, index.tsv

The dataset id is ``<workflow_id>-<version>`` (e.g. ``cmgd_nextflow-2.2.1``).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from cdsci.lake.contracts import ColumnContract, DatasetContract, TableContract, TemporalModel  # type: ignore[import-untyped]
from cdsci.lake.publish.builder import LocalDirStore  # type: ignore[import-untyped]
from cdsci.lake.publish.pipeline import publish_release  # type: ignore[import-untyped]
from cdsci.lake.publish.release import ReleaseManifest, SourceAssetVersion  # type: ignore[import-untyped]

from . import export, lake, site
from .specs import DATABASES, SPECS
from .v2 import CompletedJob, Registration

PUBLISH_ROOT = os.environ.get("ETL_PUBLISH_ROOT", "/data/cmgd/publish")
SYNC_REMOTE = os.environ.get("ETL_PUBLIC_REMOTE", "r2:cmgd-public")
# Every published table carries it (schema.json, README.md, manifest).
CMGD_DATA_LICENSE = "CC0-1.0"
# Public HTTPS base of cmgd-raw for gene-family download URLs. No default: until
# it is set, genefamilies/index.json carries object keys and url = null.
# Production: https://cmgd-raw.cancerdatasci.org (public since 2026-10-08, monode#53).
RAW_PUBLIC_BASE_URL = os.environ.get("ETL_RAW_PUBLIC_BASE_URL")
LAKE_SCHEMA = "cmgd"
# Multi-source sample metadata (plan D1: lake.cmgd_meta.*). Optional: a release
# carries the metadata tables only when the schema has the views.
META_SCHEMA = "cmgd_meta"
GENEFAMILY_FILES_TABLE = "humann_genefamilies_files"
# Public HTTPS base of cmgd-public: absolute URLs in llms.txt, pages and snippets.
PUBLIC_BASE_URL = os.environ.get("ETL_PUBLIC_BASE_URL",
                                 "https://cmgd-public.cancerdatasci.org").rstrip("/")
# Tables derived from Olm's Microbiome Repo (CC-BY-4.0, plan D4). Everything
# else in a release is CMGD_DATA_LICENSE.
OLM_LICENSE = "CC-BY-4.0"

_ARROW = {"VARCHAR": "string", "INTEGER": "int32", "BIGINT": "int64", "DOUBLE": "double",
          "BOOLEAN": "bool"}

# The release's sample hub (plan §4): one row per sample, join keys to sources.
CITATION = {
    "cmgd": ("Pasolli E, Schiffer L, Manghi P, Renson A, Obenchain V, Truong DT, Beghini F, "
             "Malik F, Ramos M, Dowd JB, Huttenhower C, Morgan M, Segata N, Waldron L. "
             "Accessible, curated metagenomic data through ExperimentHub. Nat Methods. "
             "2017;14(11):1023-1024. doi:10.1038/nmeth.4468"),
    "olm": ("Microbiome Repo, OlmLab, release R2026.25 (data package 1.20.0), "
            "doi:10.5281/zenodo.23087712; values derived. Required attribution for the "
            "CC-BY-4.0 tables."),
    "zoe": ("ZOE PREDICT open metadata (PREDICT 1-3: age, sex, BMI), Zenodo record 15308000 "
            "v1, doi:10.5281/zenodo.15308000; values derived. Required attribution for the "
            "CC-BY-4.0 tables."),
}

# Metadata tables (plan §4.2), name -> (lake.cmgd_meta view, SELECT). The SELECT
# filters to the release's samples ({samples}: the samples parquet) and puts
# study_name and sample_key first so the tables sort and join like the rest.
_PINNED = ("JOIN {meta}.source_releases p ON p.source = v.source AND p.release = v.release "
           "AND p.pinned")


def _source_rows(source: str, cols: str) -> str:
    """One source's pinned rows of sample_values on the release's samples. A
    source row belongs to a sample when one of its runs is in the sample's
    readset (sample_xref: one row per readset x run); an equi-join on the
    unnested runs, so only the release's runs are matched."""
    return (
        "WITH rs AS (SELECT DISTINCT x.run_accession, s.study_name, s.sample_key "
        "FROM {meta}.sample_xref x JOIN {samples} s ON s.readset_id = x.readset_id), "
        "v AS (SELECT v.*, unnest(v.run_accessions) AS run FROM {meta}.sample_values v "
        f"{_PINNED} WHERE v.source = '{source}') "
        f"SELECT DISTINCT rs.study_name, rs.sample_key, {cols} FROM v JOIN rs ON rs.run_accession = v.run "
        "ORDER BY rs.study_name, rs.sample_key, v.field, source_sample")


META_TABLES: dict[str, tuple[str, str]] = {
    "sample_metadata_cmd": ("sample_values", _source_rows("cmd", (
        "v.field, v.value, v.source_field, v.value_raw, v.value_unit, "
        "v.value_ontology::VARCHAR AS value_ontology, v.curator, v.source, v.release, "
        "v.sample AS source_sample"))),
    # evidence_quote never leaves the lake (plan D4): sample_values doesn't carry it.
    "sample_metadata_olm": ("sample_values", _source_rows("olm", (
        "v.field, v.value, v.source_field, v.value_raw, v.value_unit, "
        "v.confidence::DOUBLE AS confidence, v.route, v.evidence_source, v.determined_by, "
        "v.biosample, v.source, v.release, v.sample AS source_sample"))),
    "sample_metadata_harmonized": ("sample_harmonized", (
        "SELECT s.study_name, s.sample_key, h.field, h.value, h.source, h.release, h.route, "
        "h.confidence::DOUBLE AS confidence FROM {meta}.sample_harmonized h "
        "JOIN {samples} s USING (readset_id) ORDER BY s.study_name, s.sample_key, h.field")),
    "metadata_comparison": ("compare_cmd_olm", (
        "SELECT study AS study_name, field, olm_rows, n_both::BIGINT AS n_both, "
        "n_agree::BIGINT AS n_agree, n_disagree::BIGINT AS n_disagree, "
        "n_cmd_only::BIGINT AS n_cmd_only, n_olm_only::BIGINT AS n_olm_only, "
        "agreement::DOUBLE AS agreement FROM {meta}.compare_cmd_olm "
        "WHERE study IN (SELECT study_name FROM {samples}) ORDER BY ALL")),
    "metadata_sources": ("source_releases", (
        "SELECT source, release, package_version, doi, license, sha256, "
        "loaded_at::VARCHAR AS loaded_at, pinned FROM {meta}.source_releases ORDER BY ALL")),
}
# Olm-derived tables need attribution (plan D4); the rest is CMGD_DATA_LICENSE.
LICENSES: dict[str, str] = {t: OLM_LICENSE for t in (
    "sample_metadata_olm", "sample_metadata_harmonized", "metadata_comparison")}

SAMPLES_TABLE = "samples"
SAMPLES_COLUMNS = {c: "VARCHAR" for c in (
    "study_name", "sample_key", "readset_id", "run_accessions", "biosample", "bioproject",
    "pipeline_version", "git_commit", "completed_at")}

# (description, grain, primary key) per published table. Gene families are not
# here: they are per-sample downloads (genefamilies/index.json), not a table.
_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "taxonomic_profile_metaphlan": (
        "MetaPhlAn taxonomic profiles: the main pass, plus a HUMAnN bundle's own pass "
        "(humann_bundle set).",
        "one row per sample, branch, MetaPhlAn pass and clade",
        ("sample_key", "data_type", "metaphlan_profile", "humann_bundle", "clade_name")),
    "taxonomic_profile_bracken": (
        "Kraken2/Bracken species and genus profiles.",
        "one row per sample, branch and taxon",
        ("sample_key", "data_type", "clade_name")),
    "resistome": (
        "Antimicrobial-resistance genes (KMA against CARD).",
        "one row per sample, branch and CARD template",
        ("sample_key", "data_type", "gene")),
    "qc_metrics": (
        "Per-sample read accounting and provenance.",
        "one row per sample",
        ("sample_key",)),
    "marker_abundance": (
        "MetaPhlAn marker abundances.",
        "one row per sample, branch and marker",
        ("sample_key", "data_type", "marker_name")),
    "marker_presence": (
        "MetaPhlAn markers present in a sample (a row exists iff the marker is present).",
        "one row per sample, branch and present marker",
        ("sample_key", "data_type", "marker_name")),
    "humann_pathabundance": (
        "HUMAnN pathway abundances, unnormalized, in the bundle's units.",
        "one row per sample, pathway and stratum (NULL = community total)",
        ("sample_key", "humann_bundle", "pathway", "stratum")),
    "humann_pathcoverage": (
        "HUMAnN pathway coverage.",
        "one row per sample, pathway and stratum (NULL = community total)",
        ("sample_key", "humann_bundle", "pathway", "stratum")),
    SAMPLES_TABLE: (
        "One row per sample in the release: the join hub from readset id to run "
        "accessions, BioSample and BioProject, with the pipeline version that produced it.",
        "one row per sample",
        ("sample_key",)),
    "sample_metadata_cmd": (
        "Sample metadata curated by curatedMetagenomicData (curation repo, pinned commit), one "
        "row per field. `field`/`value` are harmonized where the field map covers the field.",
        "one row per sample, field and cMD sample record",
        ("sample_key", "field", "source_sample")),
    "sample_metadata_olm": (
        "Sample metadata from the Olm lab Microbiome Repo (pinned release), one row per field, "
        "with Olm's route and confidence; no evidence quotes. Attribution required: "
        f"{CITATION['olm']}",
        "one row per sample, field and Olm sample",
        ("sample_key", "field", "source_sample")),
    "sample_metadata_harmonized": (
        "One value per sample and harmonized field, with the source and source release it "
        "came from (precedence: cMD curation, then ZOE PREDICT, then Olm R1/R2 not imported "
        "from cMD, then Olm R3/R4 with confidence >= 0.5). Mixes Olm- and ZOE-derived values; "
        f"attribution required: {CITATION['olm']} {CITATION['zoe']}",
        "one row per sample and field",
        ("sample_key", "field")),
    "metadata_comparison": (
        "Agreement between cMD curation and the Olm Microbiome Repo per study and field, "
        "with Olm's cMD-imported rows included (olm_rows = all) and excluded (independent). "
        f"Attribution required: {CITATION['olm']}",
        "one row per study, field and olm_rows scope",
        ("study_name", "field", "olm_rows")),
    "metadata_sources": (
        "The metadata source releases in the store; `pinned` marks the one the metadata "
        "tables read.",
        "one row per source release",
        ("source", "release")),
}

# (description, units) per column, shared by every table that carries it.
_COLUMNS: dict[str, tuple[str, str | None]] = {
    "sample_key": ("The id the registration published the sample under: a readset id "
                   "(RS.…) for new registrations, the md5 sample_id for cmgd_nextflow 2.2.1.",
                   None),
    "sample_id": ("md5 of the sorted run accessions (NULL for readset-keyed registrations).",
                  None),
    "readset_id": ("Readset id (RS.…, ADR-0007) once known.", None),
    "study_name": ("curatedMetagenomicData study name.", None),
    "run_ids": ("Semicolon-separated SRA run accessions.", None),
    "workflow_id": ("Registration workflow id.", None),
    "version": ("Registration version.", None),
    "data_type": ("full_data (all reads) or rarefied_data (1M-read subsample).", None),
    "metaphlan_profile": ("MetaPhlAn pass (release + index) the row comes from.", None),
    "humann_bundle": ("HUMAnN bundle; NULL for rows outside a HUMAnN bundle.", None),
    "clade_name": ("Clade label exactly as the profiler reported it.", None),
    "rank": ("Taxonomic rank (kingdom … species, strain).", None),
    "ncbi_taxid": ("NCBI taxonomy id of the clade.", None),
    "sgb_id": ("MetaPhlAn species-level genome bin (t__SGB…), on SGB leaves.", None),
    "relative_abundance": ("MetaPhlAn relative abundance.", "percent"),
    "coverage": ("Coverage as reported by the tool.", None),
    "estimated_reads": ("Estimated reads assigned to the clade.", "reads"),
    "fraction_total_reads": ("Bracken fraction of total reads.", "fraction (0-1)"),
    "gene": ("CARD reference template.", None),
    "template_coverage": ("KMA template coverage.", "percent"),
    "template_identity": ("KMA template identity.", "percent"),
    "depth": ("KMA depth.", None),
    "score": ("KMA score.", None),
    "reads_raw": ("Reads before host decontamination.", "reads"),
    "reads_decontaminated": ("Reads after host decontamination.", "reads"),
    "bases_raw": ("Bases before host decontamination.", "bases"),
    "bases_decontaminated": ("Bases after host decontamination.", "bases"),
    "reads_surviving_fraction": ("reads_decontaminated / reads_raw.", "fraction (0-1)"),
    "bases_surviving_fraction": ("bases_decontaminated / bases_raw.", "fraction (0-1)"),
    "metaphlan_index": ("MetaPhlAn index (2.2.x manifests).", None),
    "pipeline_version": ("curatedMetagenomicsNextflow version that produced the sample.", None),
    "git_commit": ("Pipeline git commit.", None),
    "marker_name": ("MetaPhlAn marker id.", None),
    "value": ("Marker abundance as reported by MetaPhlAn.", None),
    "pathway": ("HUMAnN pathway (or UNMAPPED / UNINTEGRATED).", None),
    "stratum": ("Contributing taxon; NULL for the community total.", None),
    "abundance": ("HUMAnN abundance, unnormalized.", "bundle units (3.9 RPK, 4.x CPM)"),
    "run_accessions": ("Semicolon-separated INSDC run accessions of the readset.", None),
    "biosample": ("INSDC BioSample accession(s), semicolon-separated (NULL if not yet known).",
                  None),
    "bioproject": ("INSDC BioProject/study accession(s), semicolon-separated.", None),
    "completed_at": ("When the pipeline run for this sample completed (UTC, ISO 8601).", None),
    "field": ("Metadata field: the harmonized name (age_years, sex, country, bmi, "
              "antibiotics_current_use, health_condition) where mapped, else the source's own.",
              None),
    "source_field": ("The field name as the source spells it.", None),
    "value_raw": ("The value exactly as the source gives it.", None),
    "value_unit": ("The source's unit for value_raw, when it gives one.", None),
    "value_ontology": ("Ontology term ids the source attaches to the value.", None),
    "curator": ("cMD curator of the record.", None),
    "confidence": ("Source-reported confidence (Olm routes R3/R4); NULL for curated values.",
                   "0-1"),
    "route": ("How the value was determined: 'curated' (cMD) or Olm's route R1-R4.", None),
    "evidence_source": ("Olm evidence source (external.cmd = imported from cMD).", None),
    "determined_by": ("Olm determination method.", None),
    "source": ("Metadata source: cmd (curatedMetagenomicData curation) or olm (Microbiome "
               "Repo).", None),
    "release": ("Source release: cMD curation commit, or Olm release id.", None),
    "source_sample": ("The source's own sample id (cMD sample_id, Olm sample_key).", None),
    "olm_rows": ("Olm rows compared: all, or independent (without external.cmd).", None),
    "n_both": ("Samples with a value in both sources.", "samples"),
    "n_agree": ("Of n_both, values that agree (age within 1 year).", "samples"),
    "n_disagree": ("Of n_both, values that disagree.", "samples"),
    "n_cmd_only": ("Samples with a cMD value only.", "samples"),
    "n_olm_only": ("Samples with an Olm value only.", "samples"),
    "agreement": ("n_agree / n_both.", "fraction (0-1)"),
    "package_version": ("Source data package version.", None),
    "doi": ("Source release DOI.", None),
    "license": ("Source licence.", None),
    "sha256": ("sha256 of the loaded source file. (Its download URL is in provenance.json: a frozen catalog may not carry URLs.)", None),
    "loaded_at": ("When the release was loaded into the lake (UTC).", None),
    "pinned": ("Whether this is the release the metadata tables read.", None),
}


def dataset_id(workflow_id: str, version: str) -> str:
    return f"{workflow_id}-{version}"


def parse_registration(text: str) -> tuple[str, str]:
    """``<workflow_id>/<version>`` → (workflow_id, version); only registrations with a spec."""
    workflow_id, _, version = text.partition("/")
    if (workflow_id, version) not in SPECS:
        raise ValueError(f"no OutputSpec for registration {text!r} "
                         f"(known: {', '.join(f'{w}/{v}' for w, v in SPECS)})")
    return workflow_id, version


def published_tables(workflow_id: str, version: str) -> list[str]:
    """The tables every release of a registration carries: the lake tables its
    specs write, minus gene families (per-sample downloads, ADR-0011), plus the
    ``samples`` hub. Metadata tables are added when the lake has them
    (``_metadata``)."""
    return [t for t in lake.SCHEMAS
            if t in {s.table for s in SPECS[(workflow_id, version)]} and t in _TABLES] + [SAMPLES_TABLE]


def has_humann(workflow_id: str, version: str) -> bool:
    return any("humann_bundle" in s.tags for s in SPECS[(workflow_id, version)])


def _sort_by(table: str, columns: dict[str, str] | None = None) -> tuple[str, ...]:
    feature = lake._FEATURE.get(table) or ("field" if table in META_TABLES else None)
    return tuple(c for c in ("study_name", "sample_key", feature)
                 if c and (columns is None or c in columns))


def table_contract(table: str, columns: dict[str, str] | None = None) -> TableContract:
    """``columns`` (name -> DuckDB type) defaults to the lake table's (or the
    samples hub's) schema; metadata tables pass the columns they were built with."""
    description, grain, pk = _TABLES[table]
    cols = columns or (SAMPLES_COLUMNS if table == SAMPLES_TABLE else lake.SCHEMAS[table])
    return TableContract(
        name=table, description=description, grain=grain, primary_key=pk,
        temporal_model=TemporalModel.UPSERT_LATEST_SNAPSHOT,
        owner="curatedMetagenomicData", license=LICENSES.get(table, CMGD_DATA_LICENSE),
        sort_by=_sort_by(table, cols),
        columns=tuple(ColumnContract(name=c, arrow_type=_ARROW[t], description=_COLUMNS[c][0],
                                     nullable=True, units=_COLUMNS[c][1])
                      for c, t in cols.items()),
    )


def dataset_contract(workflow_id: str, version: str,
                     extra: dict[str, dict[str, str]] | None = None) -> DatasetContract:
    """``extra``: metadata tables present in this release, name -> columns."""
    tables = {t: table_contract(t) for t in published_tables(workflow_id, version)}
    tables |= {t: table_contract(t, cols) for t, cols in (extra or {}).items()}
    return DatasetContract(
        id=dataset_id(workflow_id, version),
        title=f"curatedMetagenomicData: {workflow_id} {version}",
        description=(f"Profiles from the {workflow_id} {version} registration of "
                     "curatedMetagenomicsNextflow, one full snapshot per release, with a "
                     "samples table and, when available, per-source sample metadata. HUMAnN "
                     "gene families are per-sample downloads listed in genefamilies/index.json."),
        publisher="curatedMetagenomicData",
        tables=tables,
    )


def _snapshot(con: duckdb.DuckDBPyConnection, table: str, sid: int, workflow_id: str,
              version: str, out: Path) -> None:
    cols = ", ".join(lake.SCHEMAS[table])
    con.sql(f"SELECT {cols} FROM lake.{LAKE_SCHEMA}.{table} AT (VERSION => {sid}) "
            "WHERE workflow_id = $w AND version = $v ORDER BY " + ", ".join(_sort_by(table)),
            params={"w": workflow_id, "v": version}).write_parquet(str(out))


def _has_meta(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Whether ``lake.cmgd_meta.<name>`` (table or view) exists."""
    return con.execute(
        "SELECT count(*) FROM (SELECT table_name AS n FROM duckdb_tables() WHERE "
        "database_name = 'lake' AND schema_name = $s UNION ALL SELECT view_name FROM "
        "duckdb_views() WHERE database_name = 'lake' AND schema_name = $s) WHERE n = $n",
        {"s": META_SCHEMA, "n": name}).fetchall()[0][0] > 0


def _samples(con: duckdb.DuckDBPyConnection, snap: Path, jobs: Sequence[CompletedJob]) -> None:
    """``samples.parquet`` from the qc snapshot (one row per ingested sample), the
    v2 jobs (runs, completion time) and, when the metadata store has it, the
    BioSample/BioProject cross-reference."""
    con.execute("CREATE OR REPLACE TEMP TABLE publish_jobs "
                "(sample_key VARCHAR, run_accessions VARCHAR, completed_at VARCHAR)")
    if jobs:
        # ponytail: executemany is ~1 s per 1k rows; fine below ~50k samples.
        con.executemany("INSERT INTO publish_jobs VALUES (?, ?, ?)",
                        [[j.sample_key, j.run_accessions, j.completed_at] for j in jobs])
    xref = (f"SELECT readset_id, string_agg(DISTINCT biosample, ';' ORDER BY biosample) AS biosample, "
            f"string_agg(DISTINCT bioproject, ';' ORDER BY bioproject) AS bioproject "
            f"FROM lake.{META_SCHEMA}.sample_xref GROUP BY readset_id"
            if _has_meta(con, "sample_xref") else
            "SELECT NULL::VARCHAR AS readset_id, NULL::VARCHAR AS biosample, "
            "NULL::VARCHAR AS bioproject WHERE false")
    con.sql(
        f"WITH x AS ({xref}) SELECT q.study_name, q.sample_key, q.readset_id, "
        "coalesce(j.run_accessions, q.run_ids) AS run_accessions, x.biosample, x.bioproject, "
        "q.pipeline_version, q.git_commit, j.completed_at "
        f"FROM read_parquet('{snap / 'qc_metrics.parquet'}') q "
        "LEFT JOIN publish_jobs j USING (sample_key) LEFT JOIN x USING (readset_id) "
        "ORDER BY q.study_name, q.sample_key").write_parquet(str(snap / f"{SAMPLES_TABLE}.parquet"))


def _metadata(con: duckdb.DuckDBPyConnection, snap: Path) -> dict[str, dict[str, str]]:
    """Write each metadata table the lake has into ``snap``, filtered to the
    release's samples; returns name -> columns. Empty without ``lake.cmgd_meta``."""
    out: dict[str, dict[str, str]] = {}
    samples = f"read_parquet('{snap / f'{SAMPLES_TABLE}.parquet'}')"
    for table, (view, select) in META_TABLES.items():
        if not _has_meta(con, view):
            continue
        rel = con.sql(select.format(meta=f"lake.{META_SCHEMA}", samples=samples))
        rel.write_parquet(str(snap / f"{table}.parquet"))
        out[table] = dict(zip(rel.columns, (str(t) for t in rel.types)))
    return out


def publish(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str,
            root: Path | str = PUBLISH_ROOT, *, jobs: Sequence[CompletedJob] = (),
            registration: Registration | None = None,
            raw_base_url: str | None = RAW_PUBLIC_BASE_URL,
            public_base_url: str = PUBLIC_BASE_URL,
            today: date | None = None) -> ReleaseManifest:
    """Build one release of a registration's dataset into the local store ``root``.

    ``con`` has the shared lake attached as ``lake`` (``cdsci.lake.lake_connect``).
    ``jobs`` are the registration's completed v2 jobs (run accessions and
    completion times for the ``samples`` table); ``registration`` its v2 record
    (revision, params) for the provenance block."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    sid = con.sql("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]  # type: ignore[index]

    # Staged next to the store so moving the extras into the release is a rename.
    with tempfile.TemporaryDirectory(dir=root, prefix=".staging-") as tmp:
        snap, extras = Path(tmp) / "snapshot", Path(tmp) / "extras"
        snap.mkdir()
        for t in published_tables(workflow_id, version):
            if t != SAMPLES_TABLE:
                _snapshot(con, t, sid, workflow_id, version, snap / f"{t}.parquet")
        _samples(con, snap, jobs)
        meta = _metadata(con, snap)
        contract = dataset_contract(workflow_id, version, meta)
        sources = [SourceAssetVersion(ref=f"lake.{LAKE_SCHEMA}.{t}", version=f"snapshot:{sid}")
                   for t in contract.tables if t in lake.SCHEMAS]
        if registration:  # the samples table's runs and completion times
            sources.append(SourceAssetVersion(ref="nf_telemetry.v2.jobs",
                                              version=f"registration:{registration.pk}"))
        sources += [SourceAssetVersion(ref=f"lake.{META_SCHEMA}.{META_TABLES[t][0]}",
                                       version=f"snapshot:{sid}") for t in meta]
        artifacts = []
        if has_humann(workflow_id, version):
            files = con.sql(
                "SELECT q.study_name, f.sample_key, f.readset_id, f.humann_bundle, f.branch, f.key, "
                f"f.size, f.sha256, f.rows FROM (SELECT * FROM lake.{LAKE_SCHEMA}.{GENEFAMILY_FILES_TABLE} "
                f"AT (VERSION => {sid})) AS f LEFT JOIN read_parquet('{snap / 'qc_metrics.parquet'}') AS q "
                "USING (sample_key) WHERE f.workflow_id = $w AND f.version = $v "
                "ORDER BY q.study_name, f.sample_key",
                params={"w": workflow_id, "v": version})
            export.write_genefamilies_index(files, extras / "genefamilies", contract.id,
                                            raw_base_url)
            artifacts.append(export.entry(extras, extras / "genefamilies" / "index.json"))
            sources.append(SourceAssetVersion(ref=f"lake.{LAKE_SCHEMA}.{GENEFAMILY_FILES_TABLE}",
                                              version=f"snapshot:{sid}"))
        export.write_studies(snap, extras / "studies", contract.id, artifacts)

        rcon = duckdb.connect()
        manifest = publish_release(
            LocalDirStore(root), contract=contract,
            tables={t: rcon.read_parquet(str(snap / f"{t}.parquet")) for t in contract.tables},
            source_asset_versions=tuple(sources), run_id=str(uuid.uuid4()), today=today)
        rcon.close()
        release_dir = root / contract.id / manifest.release
        for extra in sorted(extras.iterdir()):
            shutil.move(str(extra), str(release_dir / extra.name))
        source_releases = []
        if meta and _has_meta(con, "source_releases"):
            rel = con.sql(f"SELECT * REPLACE (loaded_at::VARCHAR AS loaded_at) "
                          f"FROM lake.{META_SCHEMA}.source_releases WHERE pinned ORDER BY ALL")
            source_releases = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    add_provenance(release_dir, workflow_id, version, registration, manifest, source_releases)
    site.write_study_pages(release_dir, public_base_url)
    update_root_index(root, workflow_id, version, manifest.release)
    site.write_site(root, public_base_url)
    return manifest


def add_provenance(release_dir: Path, workflow_id: str, version: str,
                   registration: Registration | None, manifest: ReleaseManifest,
                   source_releases: list[dict]) -> dict:
    """Append the ``cmgd`` block to the release's ``provenance.json`` (plan §4.3):
    the registration, the pipeline versions the samples actually ran, the
    reference databases pinned for the registration, the metadata source
    releases, and each table's licence. ``verify_release`` only checks that the
    document is readable, so adding to it after the build keeps the release valid."""
    samples = release_dir / "tables" / SAMPLES_TABLE / "data" / "part-00000.parquet"
    ran = duckdb.sql(
        "SELECT pipeline_version, git_commit, count(*) AS n_samples, "
        f"count(DISTINCT study_name) AS n_studies FROM read_parquet('{samples}') "
        "GROUP BY ALL ORDER BY ALL").fetchall()
    block = {
        "registration": {
            "workflow_id": workflow_id, "version": version,
            "revision": registration.revision if registration else None,
            "repository_url": registration.repository_url if registration else None,
            "params": registration.params if registration else None,
        },
        "pipeline_runs": [{"pipeline_version": v, "git_commit": c, "n_samples": n, "n_studies": s}
                          for v, c, n, s in ran],
        "databases": DATABASES.get((workflow_id, version)),
        "metadata_sources": [{k: (str(v) if isinstance(v, (datetime, date)) else v)
                              for k, v in r.items()} for r in source_releases],
        "licenses": {t.name: t.license for t in manifest.tables},
        "citation": CITATION,
    }
    path = release_dir / "provenance.json"
    doc = json.loads(path.read_text())
    doc["cmgd"] = block
    path.write_text(json.dumps(doc, indent=2, default=str))
    return block




def update_root_index(root: Path, workflow_id: str, version: str, release: str) -> None:
    """Add or replace one dataset's entry in ``<root>/index.json`` (read, merge,
    atomic replace). ``latest`` is relative to the store root."""
    path = root / "index.json"
    datasets = json.loads(path.read_text())["datasets"] if path.exists() else []
    did = dataset_id(workflow_id, version)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    datasets = sorted([d for d in datasets if d["id"] != did] + [
        {"id": did, "workflow_id": workflow_id, "version": version, "latest_release": release,
         "latest": f"{did}/latest.json", "updated_at": now}], key=lambda d: d["id"])
    with tempfile.NamedTemporaryFile("w", dir=root, prefix=".index-", suffix=".json",
                                     delete=False) as f:
        json.dump({"spec_version": export.INDEX_SPEC_VERSION, "updated_at": now,
                   "datasets": datasets}, f, indent=2)
    os.replace(f.name, path)


def sync_commands(root: Path | str, dataset: str, remote: str = SYNC_REMOTE) -> list[list[str]]:
    """rclone commands that upload a local dataset to the public bucket. Release
    directories first (``--immutable``: a published object is never rewritten), the
    pointer files next, so ``latest.json`` never names a release that isn't there,
    and the store's root ``index.json`` last. The root index lists every dataset
    built in the local store, so sync each one after building it."""
    if not export.SAFE_NAME.match(dataset):
        raise ValueError(f"bad dataset id {dataset!r}")
    src, dst = f"{Path(root)}/{dataset}", f"{remote}/{dataset}"
    cmds = [["rclone", "copy", "--immutable", "--exclude", "/releases.json",
             "--exclude", "/latest.json", src, dst]]
    for pointer in ("releases.json", "latest.json"):
        cmds.append(["rclone", "copyto", "--header-upload", "Cache-Control: no-cache",
                     f"{src}/{pointer}", f"{dst}/{pointer}"])
    for name in ("index.json", "index.html", "llms.txt"):  # the site's root, all regenerated
        if name == "index.json" or (Path(root) / name).exists():
            cmds.append(["rclone", "copyto", "--header-upload", "Cache-Control: no-cache",
                         f"{Path(root)}/{name}", f"{remote}/{name}"])
    return cmds


def sync(root: Path | str, dataset: str, remote: str = SYNC_REMOTE, dry_run: bool = False) -> None:
    for cmd in sync_commands(root, dataset, remote):
        print(" ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True)
