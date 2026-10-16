"""nf-meta — sample metadata sources into ``lake.cmgd_meta`` (ADR-0012).

Backend and credentials as for nf-etl (cdsci-lake ``CU_OPENALEX_*`` settings,
docs/etl-runbook.md).

  nf-meta load olm ZIP_URL_OR_PATH [--doi DOI] [--pin]   one Olm data package release
  nf-meta load cmd [REF] [--pin]                         cMD curation at a commit (default master)
  nf-meta load xref                                      rebuild sample_xref (v2 + pinned sources)
  nf-meta views                                          seeds + sample_values/_harmonized/compare_cmd_olm
"""
from __future__ import annotations

import argparse
import json

from . import store


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nf-meta", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    load = sub.add_parser("load")
    src = load.add_subparsers(dest="source", required=True)
    olm = src.add_parser("olm")
    olm.add_argument("zip")
    olm.add_argument("--doi")
    olm.add_argument("--pin", action="store_true")
    cmd = src.add_parser("cmd")
    cmd.add_argument("ref", nargs="?", default="master")
    cmd.add_argument("--pin", action="store_true")
    src.add_parser("xref")
    sub.add_parser("views")
    a = p.parse_args(argv)

    con = store.connect()
    try:
        if a.cmd == "views":
            out: object = store.create_views(con)
        elif a.source == "olm":
            out = store.load_olm(con, a.zip, doi=a.doi, pin=a.pin)
        elif a.source == "cmd":
            out = store.load_cmd(con, a.ref, pin=a.pin)
        else:
            out = store.load_xref(con)
    finally:
        con.close()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
