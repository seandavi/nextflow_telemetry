"""nf-meta — source catalog and versioned sample metadata (ADR-0012).

  nf-meta sources --help                              source inventory (Postgres, no lake)

Backend and credentials as for nf-etl (cdsci-lake ``CU_OPENALEX_*`` settings,
docs/etl-runbook.md).

  nf-meta load olm ZIP_URL_OR_PATH [--doi DOI] [--pin]   one Olm data package release
  nf-meta load cmd [REF] [--pin]                         cMD curation at a commit (default master)
  nf-meta load zoe [--version V]                         ZOE open metadata from catalog snapshots;
                                                         records (first time: pins) the release
  nf-meta load xref                                      rebuild sample_xref (v2 + pinned sources)
  nf-meta link zoe --ena-version V                       resolve ZOE sample IDs to ENA (reproducible)
  nf-meta views                                          seeds + sample_values/_harmonized/compare_cmd_olm
                                                         (needs load olm/cmd/zoe, link zoe)
"""
from __future__ import annotations

import argparse
import json
import os

from .catalog import cli as catalog_cli


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nf-meta", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    catalog_cli.add_parser(sub)
    load = sub.add_parser("load")
    src = load.add_subparsers(dest="source", required=True)
    olm = src.add_parser("olm")
    olm.add_argument("zip")
    olm.add_argument("--doi")
    olm.add_argument("--pin", action="store_true")
    cmd = src.add_parser("cmd")
    cmd.add_argument("ref", nargs="?", default="master")
    cmd.add_argument("--pin", action="store_true")
    zoe_p = src.add_parser("zoe", help="ZOE open metadata from catalog snapshots")
    zoe_p.add_argument("--version", default="zenodo-15308000-v1")
    src.add_parser("xref")
    link = sub.add_parser("link", help="resolve source sample IDs to ENA (from catalog snapshots)")
    link.add_argument("source", choices=["zoe"])
    link.add_argument("--ena-version", required=True, help="catalog version of the ENA listings, e.g. ena-2026-10-19")
    link.add_argument("--version", default="zenodo-15308000-v1")
    sub.add_parser("views")
    a = p.parse_args(argv)
    if a.cmd == "sources":
        catalog_cli.run(a)
        return

    # Catalog operations must not initialize the lake (or its credentials).
    from . import store

    con = store.connect()
    try:
        if a.cmd == "link":
            from . import zoe_link

            out: object = zoe_link.link_zoe(con, os.environ["SQLALCHEMY_URI"].replace(
                "postgresql://", "postgresql+asyncpg://", 1), a.ena_version, a.version)
        elif a.cmd == "views":
            out = store.create_views(con)
        elif a.source == "olm":
            out = store.load_olm(con, a.zip, doi=a.doi, pin=a.pin)
        elif a.source == "zoe":
            from . import zoe

            out = zoe.load_zoe(con, os.environ["SQLALCHEMY_URI"].replace(
                "postgresql://", "postgresql+asyncpg://", 1), a.version)
        elif a.source == "cmd":
            out = store.load_cmd(con, a.ref, pin=a.pin)
        else:
            out = store.load_xref(con)
    finally:
        con.close()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
