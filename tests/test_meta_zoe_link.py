"""ZOE -> ENA linkage: explicit statuses, and the whole path from retained snapshots."""
from __future__ import annotations

import asyncio
import bz2
import json

import pytest
from cdsci.lake import Settings
from sqlalchemy.ext.asyncio import create_async_engine

from nextflow_telemetry.meta import store, zoe, zoe_link
from nextflow_telemetry.meta.catalog import SourceCatalog, SourceManifest


def run(i, bs, alias, strat="WGS", srs=None):
    return {"run_accession": i, "sample_accession": bs, "secondary_sample_accession": srs or "ERS" + bs[-1],
            "sample_alias": alias, "library_strategy": strat, "library_source": "METAGENOMIC"}


def test_resolve_alias_statuses_and_shotgun_only():
    ena = [run("ERR1", "SAMEA1", "P3_s1"), run("ERR2", "SAMEA2", "P3_s2"), run("ERR3", "SAMEA3", "P3_s2"),
           run("ERR4", "SAMEA4", "P3_s4", strat="AMPLICON"), run("ERR5", "SAMEA5", "P3_s5")]
    links, ena_only = zoe_link.resolve("ena_sample_alias", ["P3_s1", "P3_s2", "P3_s3", "P3_s4"], ena)
    assert links["P3_s1"] == {"link_status": "resolved", "basis": "ENA sample_alias exact match",
                              "biosample": "SAMEA1", "sra_sample": "ERS1", "run_accessions": "ERR1"}
    assert links["P3_s2"]["link_status"] == "ambiguous" and links["P3_s2"]["biosample"] == "SAMEA2;SAMEA3"
    assert links["P3_s3"]["link_status"] == "not_in_ena"
    assert links["P3_s4"]["link_status"] == "not_in_ena"  # only an amplicon run: not linkable
    assert ena_only == 1  # SAMEA5 is shotgun in ENA with no metadata row


def test_resolve_biosample_scheme_and_unresolvable_scheme():
    ena = [run("ERR1", "SAMEA1", "x"), run("ERR2", "SAMEA2", "x", strat="AMPLICON")]
    links, only = zoe_link.resolve("biosample", ["SAMEA1", "SAMEA2"], ena)
    assert (links["SAMEA1"]["link_status"], links["SAMEA2"]["link_status"]) == ("resolved", "not_in_ena")
    links, _ = zoe_link.resolve("source_local", ["predict2_000001"], [run("ERR9", "SAMEA9", "15001")])
    # Never infer 'predict2_000001' == '15001': no evidence.
    assert links["predict2_000001"]["link_status"] == "unresolved_no_key"
    assert "biosample" not in links["predict2_000001"]


def test_link_end_to_end_from_retained_snapshots(db_url, tmp_path):
    ena_version = "ena-test"
    ena = {a: [run(f"ERR{n}", f"SAMEA{n}", f"{c}_1") for n in (1,)] for a, (c, _, _) in zoe.COHORTS.items()}
    ena["p1-metadata"] = [run("ERR7", "P1_1", "predict1_x")]   # biosample scheme: the ID is the accession
    async def go():
        e = create_async_engine(db_url)
        try:
            cat = SourceCatalog(e)
            await cat.apply(SourceManifest.model_validate({
                "source": {"source_id": zoe.SOURCE_ID, "title": "ZOE", "kind": "zenodo", "url": "https://example.org/z"},
                "assets": [{"asset_id": a, "locator": a, "role": "sample_metadata"} for a in zoe.COHORTS]}),
                actor="t", commit=True)
            await cat.apply(SourceManifest.model_validate({
                "source": {"source_id": zoe_link.ENA_SOURCE, "title": "ENA", "kind": "other", "url": "https://example.org/e"},
                "assets": [{"asset_id": "filereport-" + a.removesuffix("-metadata"), "locator": a, "role": "other"}
                           for a in zoe.COHORTS]}), actor="t", commit=True)
            for asset, (cohort, _, _) in zoe.COHORTS.items():
                f = tmp_path / f"{asset}.bz2"
                f.write_bytes(bz2.compress(f"sampleID\tage\tsex\tbmi\n{cohort}_1\t40\tF\t22\n{cohort}_2\t41\tM\t23\n".encode()))
                await cat.capture(zoe.SOURCE_ID, asset, version=zoe.VERSION, file=f, archive_dir=tmp_path / "a", actor="t", commit=True)
                j = tmp_path / f"{asset}.json"
                j.write_text(json.dumps(ena[asset]))
                await cat.capture(zoe_link.ENA_SOURCE, "filereport-" + asset.removesuffix("-metadata"),
                                  version=ena_version, file=j, archive_dir=tmp_path / "a", actor="t", commit=True)
        finally:
            await e.dispose()
    asyncio.run(go())
    con = store.connect(Settings(_env_file=None, lake_backend="local", storage_base_uri=f"file://{tmp_path}/lake"))
    try:
        with pytest.raises(ValueError, match="load zoe first"):
            zoe_link.link_zoe(con, db_url, ena_version)
        zoe.load_zoe(con, db_url)
        summary = zoe_link.link_zoe(con, db_url, ena_version)
        assert summary["P2"] == {"unresolved_no_key": 2, "ena_shotgun_without_metadata": 1}
        assert summary["P3_US21"]["resolved"] == 1 and summary["P3_US21"]["not_in_ena"] == 1
        assert zoe_link.link_zoe(con, db_url, ena_version) == summary   # idempotent
        got = con.execute(f"SELECT link_status, count(*) FROM {store.S}.zoe_sample_link GROUP BY 1 ORDER BY 1").fetchall()
        assert got == [("not_in_ena", 5), ("resolved", 5), ("unresolved_no_key", 2)]
        assert con.execute(f"SELECT count(*) FROM {store.S}.zoe_sample_link WHERE ena_snapshot_sha256 IS NULL").fetchone() == (0,)
    finally:
        con.close()
