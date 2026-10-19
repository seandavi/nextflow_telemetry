"""Link ZOE source sample IDs to ENA, from retained snapshots of both sides.

Output ``lake.cmgd_meta.zoe_sample_link`` has one row per ZOE sample and says how (or
whether) it was resolved. Statuses are explicit; nothing is guessed:

  resolved           exactly one ENA shotgun BioSample matches the ID
  not_in_ena         the ID has metadata but no ENA shotgun sample
  ambiguous          the ID matches runs from more than one BioSample
  unresolved_no_key  the ID has no known ENA counterpart (P2: ENA uses a different scheme)

Only library_strategy WGS is linkable: P1's ENA project also holds AMPLICON samples.
ENA shotgun samples with no ZOE metadata are reported in the summary, not as rows.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import duckdb
from cdsci.lake import ops, upsert  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import create_async_engine

from . import zoe
from .catalog import SourceCatalog
from .catalog import archive
from .store import S, _q

ENA_SOURCE = "ena-portal-zoe"
ENA_KEY = {"biosample": "sample_accession", "ena_sample_alias": "sample_alias"}
COLUMNS = ["release", "ena_release", "ena_snapshot_sha256", "cohort", "bioproject", "source_sample",
           "id_scheme", "link_status", "biosample", "sra_sample", "run_accessions", "basis"]


def resolve(scheme: str, ids: list[str], ena_runs: list[dict]) -> tuple[dict[str, dict], int]:
    """(id -> link fields, number of ENA shotgun samples with no ID in ``ids``)."""
    key = ENA_KEY.get(scheme)
    wgs = [r for r in ena_runs if r.get("library_strategy") == "WGS"]
    if key is None:
        return {i: {"link_status": "unresolved_no_key", "basis": f"{scheme}: no ENA key known"} for i in ids}, \
            len({r["sample_accession"] for r in wgs})
    by: dict[str, list[dict]] = defaultdict(list)
    for r in wgs:
        by[r[key]].append(r)
    out = {}
    for i in ids:
        runs = by.get(i)
        if not runs:
            out[i] = {"link_status": "not_in_ena", "basis": f"no WGS run with ENA {key} = ID"}
            continue
        samples = {r["sample_accession"] for r in runs}
        out[i] = {"link_status": "resolved" if len(samples) == 1 else "ambiguous",
                  "basis": f"ENA {key} exact match",
                  "biosample": ";".join(sorted(samples)) if len(samples) > 1 else next(iter(samples)),
                  "sra_sample": ";".join(sorted({r["secondary_sample_accession"] for r in runs})),
                  "run_accessions": ";".join(sorted(r["run_accession"] for r in runs))}
    matched = {r["sample_accession"] for i in ids for r in by.get(i, [])}
    ena_only = len({r["sample_accession"] for r in wgs} - matched)
    return out, ena_only


async def _receipts(db_uri: str, zoe_version: str, ena_version: str) -> dict[str, dict]:
    engine = create_async_engine(db_uri)
    try:
        cat = SourceCatalog(engine)
        return {a: await cat.snapshot(ENA_SOURCE, "filereport-" + a.removesuffix("-metadata"), ena_version)
                for a in zoe.COHORTS}
    finally:
        await engine.dispose()


def link_zoe(con: duckdb.DuckDBPyConnection, db_uri: str, ena_version: str,
             zoe_version: str = zoe.VERSION) -> dict:
    receipts = asyncio.run(_receipts(db_uri, zoe_version, ena_version))
    rows, summary = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for asset, (cohort, scheme, bp) in zoe.COHORTS.items():
            rc = receipts[asset]
            blob = archive.fetch(rc["artifact_uri"], Path(tmp) / asset,
                                 archive.Artifact(rc["sha256"], rc["size_bytes"], rc["artifact_uri"]))
            ena = json.loads(blob.read_text())
            try:
                ids = [r[0] for r in con.execute(
                    f"SELECT DISTINCT source_sample FROM {S}.zoe_sample_values "
                    "WHERE release = ? AND cohort = ? ORDER BY 1", [zoe_version, cohort]).fetchall()]
            except duckdb.CatalogException:
                ids = []
            if not ids:
                raise ValueError(f"{cohort}: no zoe_sample_values for release {zoe_version}; load zoe first")
            links, ena_only = resolve(scheme, ids, ena)
            for i in ids:
                rows.append({"release": zoe_version, "ena_release": ena_version,
                             "ena_snapshot_sha256": rc["sha256"], "cohort": cohort, "bioproject": bp,
                             "source_sample": i, "id_scheme": scheme, "biosample": None,
                             "sra_sample": None, "run_accessions": None, **links[i]})
            counts: dict[str, int] = defaultdict(int)
            for v in links.values():
                counts[v["link_status"]] += 1
            summary[cohort] = {**counts, "ena_shotgun_without_metadata": ena_only}
        out = Path(tmp) / "links.ndjson"
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
        types = ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS)
        sql = f"SELECT * FROM read_json({_q(str(out))}, format = 'newline_delimited', columns = {{{types}}})"
        with ops.run(con, source="cmgd_meta_zoe", target=f"{S}.zoe_sample_link", version=ena_version) as r:
            r.rows = upsert(con, f"{S}.zoe_sample_link", sql, ["release", "cohort", "source_sample"])
    return summary
