"""Public releases of ``lake.cmgd`` (ADR-0011): one dataset per registration.

``publish`` reads one registration's rows from the shared lake at a single lake
snapshot and hands them to cdsci-lake's ``publish_release``, which writes the
release (sorted Parquet, schema/file indexes, frozen read-only DuckLake catalog,
manifest, ``releases.json``/``latest.json``) into a local store. The per-study
downloads and the gene-family index (``export.py``) are built from the same
snapshot and moved into the release directory once ``publish_release`` returns:
it has no hook for extra artifacts, so they are not in the manifest (their own
``index.json`` files carry bytes and checksums).

``sync`` is the separate, explicit upload of a local dataset to ``cmgd-public``.

  <root>/<dataset>/releases.json, latest.json
  <root>/<dataset>/<release>/manifest.json, catalog.ducklake, tables/<table>/...
  <root>/<dataset>/<release>/studies/index.json, studies/<study>/...
  <root>/<dataset>/<release>/genefamilies/index.json, index.tsv

The dataset id is ``<workflow_id>-<version>`` (e.g. ``cmgd_nextflow-2.2.1``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import date
from pathlib import Path

import duckdb
from cdsci.lake.contracts import ColumnContract, DatasetContract, TableContract, TemporalModel  # type: ignore[import-untyped]
from cdsci.lake.publish.builder import LocalDirStore  # type: ignore[import-untyped]
from cdsci.lake.publish.pipeline import publish_release  # type: ignore[import-untyped]
from cdsci.lake.publish.release import ReleaseManifest, SourceAssetVersion  # type: ignore[import-untyped]

from . import export, lake
from .specs import SPECS

PUBLISH_ROOT = os.environ.get("ETL_PUBLISH_ROOT", "/data/cmgd/publish")
SYNC_REMOTE = os.environ.get("ETL_PUBLIC_REMOTE", "r2:cmgd-public")
# Every published table carries it (schema.json, README.md, manifest).
CMGD_DATA_LICENSE = "CC0-1.0"
# Public HTTPS base of cmgd-raw for gene-family download URLs. No default: until
# it is set, genefamilies/index.json carries object keys and url = null.
# Production: https://cmgd-raw.cancerdatasci.org (public since 2026-10-08, monode#53).
RAW_PUBLIC_BASE_URL = os.environ.get("ETL_RAW_PUBLIC_BASE_URL")
LAKE_SCHEMA = "cmgd"
GENEFAMILY_FILES_TABLE = "humann_genefamilies_files"

_ARROW = {"VARCHAR": "string", "INTEGER": "int32", "BIGINT": "int64", "DOUBLE": "double"}

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
    """The lake tables a registration's release carries: whatever its specs write,
    minus gene families (per-sample downloads, ADR-0011)."""
    return [t for t in lake.SCHEMAS
            if t in {s.table for s in SPECS[(workflow_id, version)]} and t in _TABLES]


def has_humann(workflow_id: str, version: str) -> bool:
    return any("humann_bundle" in s.tags for s in SPECS[(workflow_id, version)])


def _sort_by(table: str) -> tuple[str, ...]:
    return tuple(c for c in ("study_name", "sample_key", lake._FEATURE.get(table)) if c)


def table_contract(table: str) -> TableContract:
    description, grain, pk = _TABLES[table]
    return TableContract(
        name=table, description=description, grain=grain, primary_key=pk,
        temporal_model=TemporalModel.UPSERT_LATEST_SNAPSHOT,
        owner="curatedMetagenomicData", license=CMGD_DATA_LICENSE,
        sort_by=_sort_by(table),
        columns=tuple(ColumnContract(name=c, arrow_type=_ARROW[t], description=_COLUMNS[c][0],
                                     nullable=True, units=_COLUMNS[c][1])
                      for c, t in lake.SCHEMAS[table].items()),
    )


def dataset_contract(workflow_id: str, version: str) -> DatasetContract:
    return DatasetContract(
        id=dataset_id(workflow_id, version),
        title=f"curatedMetagenomicData: {workflow_id} {version}",
        description=(f"Profiles from the {workflow_id} {version} registration of "
                     "curatedMetagenomicsNextflow, one full snapshot per release. HUMAnN gene "
                     "families are per-sample downloads listed in genefamilies/index.json."),
        publisher="curatedMetagenomicData",
        tables={t: table_contract(t) for t in published_tables(workflow_id, version)},
    )


def _snapshot(con: duckdb.DuckDBPyConnection, table: str, sid: int, workflow_id: str,
              version: str, out: Path) -> None:
    cols = ", ".join(lake.SCHEMAS[table])
    con.sql(f"SELECT {cols} FROM lake.{LAKE_SCHEMA}.{table} AT (VERSION => {sid}) "
            "WHERE workflow_id = $w AND version = $v ORDER BY " + ", ".join(_sort_by(table)),
            params={"w": workflow_id, "v": version}).write_parquet(str(out))


def publish(con: duckdb.DuckDBPyConnection, workflow_id: str, version: str,
            root: Path | str = PUBLISH_ROOT, *, raw_base_url: str | None = RAW_PUBLIC_BASE_URL,
            today: date | None = None) -> ReleaseManifest:
    """Build one release of a registration's dataset into the local store ``root``.

    ``con`` has the shared lake attached as ``lake`` (``cdsci.lake.lake_connect``)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    contract = dataset_contract(workflow_id, version)
    sid = con.sql("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]  # type: ignore[index]
    sources = [SourceAssetVersion(ref=f"lake.{LAKE_SCHEMA}.{t}", version=f"snapshot:{sid}")
               for t in contract.tables]

    # Staged next to the store so moving the extras into the release is a rename.
    with tempfile.TemporaryDirectory(dir=root, prefix=".staging-") as tmp:
        snap, extras = Path(tmp) / "snapshot", Path(tmp) / "extras"
        snap.mkdir()
        for t in contract.tables:
            _snapshot(con, t, sid, workflow_id, version, snap / f"{t}.parquet")
        export.write_studies(snap, extras / "studies", contract.id)
        if has_humann(workflow_id, version):
            files = con.sql(
                "SELECT q.study_name, f.sample_key, f.readset_id, f.humann_bundle, f.branch, f.key, "
                f"f.bytes, f.rows FROM (SELECT * FROM lake.{LAKE_SCHEMA}.{GENEFAMILY_FILES_TABLE} "
                f"AT (VERSION => {sid})) AS f LEFT JOIN read_parquet('{snap / 'qc_metrics.parquet'}') AS q "
                "USING (sample_key) WHERE f.workflow_id = $w AND f.version = $v "
                "ORDER BY q.study_name, f.sample_key",
                params={"w": workflow_id, "v": version})
            export.write_genefamilies_index(files, extras / "genefamilies", contract.id,
                                            raw_base_url)
            sources.append(SourceAssetVersion(ref=f"lake.{LAKE_SCHEMA}.{GENEFAMILY_FILES_TABLE}",
                                              version=f"snapshot:{sid}"))

        rcon = duckdb.connect()
        manifest = publish_release(
            LocalDirStore(root), contract=contract,
            tables={t: rcon.read_parquet(str(snap / f"{t}.parquet")) for t in contract.tables},
            source_asset_versions=tuple(sources), run_id=str(uuid.uuid4()), today=today)
        rcon.close()
        release_dir = root / contract.id / manifest.release
        for extra in sorted(extras.iterdir()):
            shutil.move(str(extra), str(release_dir / extra.name))
    return manifest


def sync_commands(root: Path | str, dataset: str, remote: str = SYNC_REMOTE) -> list[list[str]]:
    """rclone commands that upload a local dataset to the public bucket. Release
    directories first (``--immutable``: a published object is never rewritten), the
    pointer files last, so ``latest.json`` never names a release that isn't there."""
    if not export.SAFE_NAME.match(dataset):
        raise ValueError(f"bad dataset id {dataset!r}")
    src, dst = f"{Path(root)}/{dataset}", f"{remote}/{dataset}"
    cmds = [["rclone", "copy", "--immutable", "--exclude", "/releases.json",
             "--exclude", "/latest.json", src, dst]]
    for pointer in ("releases.json", "latest.json"):
        cmds.append(["rclone", "copyto", "--header-upload", "Cache-Control: no-cache",
                     f"{src}/{pointer}", f"{dst}/{pointer}"])
    return cmds


def sync(root: Path | str, dataset: str, remote: str = SYNC_REMOTE, dry_run: bool = False) -> None:
    for cmd in sync_commands(root, dataset, remote):
        print(" ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True)
