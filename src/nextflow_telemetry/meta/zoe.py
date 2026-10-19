"""ZOE PREDICT open metadata -> ``lake.cmgd_meta.zoe_sample_values`` (exploration:
docs/research/metadata-sources/).

Reads only bytes the source catalog retained (receipt -> fetch -> sha256 check). Keeps
the source's own sample ID and says what kind of ID it is; no ENA lookup, readsets or
harmonization here, those are linkage. Values stay raw. Every row carries the sha256 of
the snapshot it came from.
"""
from __future__ import annotations

import asyncio
import bz2
import csv
import io
import json
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import duckdb
from cdsci.lake import ops, upsert  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import create_async_engine

from .catalog import SourceCatalog
from .catalog import archive
from .store import S, _q

SOURCE_ID = "zoe-predict-open"
VERSION = "zenodo-15308000-v1"
# asset -> (cohort, what sampleID is, ENA BioProject the IDs are expected to link to)
COHORTS = {
    "p1-metadata": ("P1", "biosample", "PRJEB39223"),
    "p2-metadata": ("P2", "source_local", "PRJEB75460"),   # no public ENA key (exploration)
    "p3-us21-metadata": ("P3_US21", "ena_sample_alias", "PRJEB75462"),
    "p3-us22a-metadata": ("P3_US22A", "ena_sample_alias", "PRJEB75463"),
    "p3-uk22a-metadata": ("P3_UK22A", "ena_sample_alias", "PRJEB75464"),
    "p3-uk23art-metadata": ("P3_UK23ART", "ena_sample_alias", "PRJEB75465"),
}
UNITS = {"age": "year", "bmi": "kg/m2", "sex": None}
_ABSENT = {"", "NA", "NaN", "nan"}
COLUMNS = ["source", "release", "snapshot_sha256", "cohort", "bioproject", "source_sample",
           "id_scheme", "field", "value_raw", "value_unit"]


def parse(text: Iterable[str], *, cohort: str, scheme: str, bioproject: str, sha256: str,
          release: str = VERSION) -> Iterator[dict]:
    """One row per (sample, field) that has a value. Unknown columns are an error: a
    changed layout must be looked at, not silently dropped. Duplicate IDs are an error."""
    reader = csv.DictReader(text, delimiter="\t")
    if set(reader.fieldnames or []) != {"sampleID", *UNITS}:
        raise ValueError(f"{cohort}: unexpected columns {reader.fieldnames}")
    seen: set[str] = set()
    for r in reader:
        sid = r["sampleID"].strip()
        if not sid or sid in seen:
            raise ValueError(f"{cohort}: empty or duplicate sampleID {sid!r}")
        seen.add(sid)
        for field, unit in UNITS.items():
            v = r[field].strip()
            if v not in _ABSENT:
                yield {"source": "zoe", "release": release, "snapshot_sha256": sha256,
                       "cohort": cohort, "bioproject": bioproject, "source_sample": sid,
                       "id_scheme": scheme, "field": field, "value_raw": v, "value_unit": unit}


async def _receipts(db_uri: str, version: str) -> dict[str, dict]:
    engine = create_async_engine(db_uri)
    try:
        cat = SourceCatalog(engine)
        return {a: await cat.snapshot(SOURCE_ID, a, version) for a in COHORTS}
    finally:
        await engine.dispose()


def load_zoe(con: duckdb.DuckDBPyConnection, db_uri: str, version: str = VERSION) -> dict[str, int]:
    """Load every ZOE cohort file captured under ``version``. All receipts must exist."""
    receipts = asyncio.run(_receipts(db_uri, version))
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for asset, (cohort, scheme, bp) in COHORTS.items():
            rc = receipts[asset]
            blob = archive.fetch(rc["artifact_uri"], Path(tmp) / asset,
                                 archive.Artifact(rc["sha256"], rc["size_bytes"], rc["artifact_uri"]))
            with bz2.open(blob, "rt", newline="") as f:
                rows += list(parse(f, cohort=cohort, scheme=scheme, bioproject=bp,
                                   sha256=rc["sha256"], release=version))
        out = Path(tmp) / "rows.ndjson"
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
        types = ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS)
        sql = f"SELECT * FROM read_json({_q(str(out))}, format = 'newline_delimited', columns = {{{types}}})"
        with ops.run(con, source="cmgd_meta_zoe", target=f"{S}.zoe_sample_values", version=version) as r:
            r.rows = n = upsert(con, f"{S}.zoe_sample_values", sql,
                                ["release", "cohort", "source_sample", "field"])
    return {"rows": n, "samples": len({(r["cohort"], r["source_sample"]) for r in rows})}
