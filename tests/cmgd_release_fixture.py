"""A local ``cdsci.lake`` with ``lake.cmgd.*`` fixture rows, published with
``nf-etl publish`` into a local store and served over HTTP with Range support
(what DuckDB needs to read Parquet remotely). Shared by test_etl_publish.py and
test_data_access_docs.py.

Site layout (one HTTP server): ``/public`` is the cmgd-public store root,
``/raw`` stands in for cmgd-raw (gene-family files).
"""
from __future__ import annotations

import functools
import gzip
import hashlib
import http.server
import io
import os
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from cdsci.lake import Settings, lake_connect

from nextflow_telemetry.etl import lake, publish

FIX = Path(__file__).parent / "fixtures" / "etl"
RELEASE_DAY = date(2026, 10, 8)  # docs/data-access.md's example release id
STUDIES = {"ArtachoA_2021": 3, "ZellerG_2014": 2}
LEGACY = ("cmgd_nextflow", "2.2.1")
HUMANN = ("cmgd_humann3.9", "2.3.0")
SPECIES = ["k__Bacteria|p__Bacillota|c__Clostridia|o__Eubacteriales|f__Lachnospiraceae|g__Blautia|s__Blautia_wexlerae",
           "k__Bacteria|p__Bacteroidota|c__Bacteroidia|o__Bacteroidales|f__Bacteroidaceae|g__Bacteroides|s__Bacteroides_uniformis",
           "k__Bacteria|p__Pseudomonadota|c__Gammaproteobacteria|o__Enterobacterales|f__Enterobacteriaceae|g__Escherichia|s__Escherichia_coli"]
GF_FILE = gzip.compress((FIX / "humann3.9" / "out_genefamilies.tsv").read_bytes(), mtime=0)


def sample_keys(workflow_id: str, study: str) -> list[str]:
    """md5-style keys for 2.2.1, readset-style for 2.3.0 registrations."""
    keys = [hashlib.md5(f"{study}{i}".encode()).hexdigest() for i in range(STUDIES[study])]
    return keys if workflow_id == "cmgd_nextflow" else [f"RS.{k[:32]}" for k in keys]


def _rows(workflow_id: str, version: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {t: [] for t in lake.SCHEMAS}
    out["humann_genefamilies_files"] = []
    humann = (workflow_id, version) == HUMANN
    run = 866581
    for study in STUDIES:
        for i, key in enumerate(sample_keys(workflow_id, study)):
            ids = {"sample_key": key, "sample_id": None if humann else key,
                   "readset_id": key if humann else None, "study_name": study,
                   "run_ids": f"ERR{run}", "workflow_id": workflow_id, "version": version}
            run += 1
            out["qc_metrics"].append({**ids, "reads_raw": 2_000_000 + i, "reads_decontaminated": 1_500_000,
                                      "metaphlan_profile": "mpa4.2.2_vJan25",
                                      "pipeline_version": version})
            for data_type in ("full_data", "rarefied_data"):
                b = {**ids, "data_type": data_type}
                # sample i has species[0..i]: absent species exercise the 0-fill
                for j, clade in enumerate(SPECIES[: i + 1]):
                    out["taxonomic_profile_metaphlan"].append(
                        {**b, "metaphlan_profile": "mpa4.2.2_vJan25", "clade_name": clade,
                         "rank": "species", "relative_abundance": 10.0 * (j + 1) + i})
                out["taxonomic_profile_metaphlan"].append(
                    {**b, "metaphlan_profile": "mpa4.2.2_vJan25", "clade_name": "k__Bacteria",
                     "rank": "kingdom", "relative_abundance": 100.0})
                out["taxonomic_profile_bracken"].append(
                    {**b, "clade_name": "Blautia wexlerae", "rank": "species", "ncbi_taxid": 418240,
                     "fraction_total_reads": 0.25, "estimated_reads": 1000})
                out["resistome"].append({**b, "gene": "gb|AY536519.1|ARO:3002312|tetQ", "depth": 0.78})
                out["marker_abundance"].append({**b, "marker_name": "UniClust90_X|SGB1478", "value": 1.5})
            if humann:
                hb = {**ids, "data_type": "full_data", "humann_bundle": "humann3.9"}
                out["taxonomic_profile_metaphlan"].append(
                    {**hb, "metaphlan_profile": "mpa4.1.1_vJun23", "clade_name": SPECIES[0],
                     "rank": "species", "relative_abundance": 55.0})
                for pw, stratum in (("UNMAPPED", None), ("PWY-1042", None), ("PWY-1042", "g__Blautia")):
                    out["humann_pathabundance"].append({**hb, "pathway": pw, "stratum": stratum, "abundance": 12.5})
                    out["humann_pathcoverage"].append({**hb, "pathway": pw, "stratum": stratum, "coverage": 0.5})
                out["humann_genefamilies_files"].append(
                    {"sample_key": key, "readset_id": key, "workflow_id": workflow_id, "version": version,
                     "humann_bundle": "humann3.9", "branch": "full_data",
                     "key": f"{workflow_id}/{version}/{key}/humann/humann3.9/out_genefamilies.tsv.gz",
                     "bytes": len(GF_FILE), "rows": 3})
    return out


GENEFAMILY_FILES = {"sample_key": "VARCHAR", "readset_id": "VARCHAR", "workflow_id": "VARCHAR",
                    "version": "VARCHAR", "humann_bundle": "VARCHAR", "branch": "VARCHAR",
                    "key": "VARCHAR", "bytes": "BIGINT", "rows": "BIGINT"}


def build_lake(root: Path) -> duckdb.DuckDBPyConnection:
    """A local cdsci lake with the #234 contract: ``lake.cmgd.<table>`` per
    ``lake.SCHEMAS`` plus ``lake.cmgd.humann_genefamilies_files``."""
    con = lake_connect(Settings(lake_backend="local", storage_base_uri=f"file://{root}"))
    con.execute("CREATE SCHEMA lake.cmgd")
    schemas = {**lake.SCHEMAS, "humann_genefamilies_files": GENEFAMILY_FILES}
    for table, cols in schemas.items():
        con.execute(f"CREATE TABLE lake.cmgd.{table} ({', '.join(f'{c} {t}' for c, t in cols.items())})")
    for reg in (LEGACY, HUMANN):
        for table, rows in _rows(*reg).items():
            if rows:
                cols = list(schemas[table])
                con.executemany(f"INSERT INTO lake.cmgd.{table} ({', '.join(cols)}) "
                                f"VALUES ({', '.join('?' for _ in cols)})",
                                [[r.get(c) for c in cols] for r in rows])
    return con


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler plus single-range GETs (DuckDB httpfs needs 206)."""

    def send_head(self):
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if not rng or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start_s, _, end_s = rng.removeprefix("bytes=").partition("-")
        start = int(start_s) if start_s else 0
        end = min(int(end_s), size - 1) if end_s else size - 1
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        return io.BytesIO(chunk)

    def log_message(self, format, *args):  # noqa: A002 -- quiet test output
        pass


@pytest.fixture(scope="session")
def public_site(tmp_path_factory):
    """Both fixture registrations published into ``<site>/public`` and served over HTTP."""
    site = tmp_path_factory.mktemp("site")
    for row in _rows(*HUMANN)["humann_genefamilies_files"]:
        dest = site / "raw" / row["key"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(GF_FILE)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(RangeHandler, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_port}"
    con = build_lake(tmp_path_factory.mktemp("lake"))
    manifests = {reg: publish.publish(con, *reg, site / "public", raw_base_url=f"{host}/raw",
                                      today=RELEASE_DAY)
                 for reg in (LEGACY, HUMANN)}
    con.close()
    try:
        yield SimpleNamespace(root=site / "public", base=f"{host}/public", raw=f"{host}/raw",
                              manifests=manifests)
    finally:
        server.shutdown()
