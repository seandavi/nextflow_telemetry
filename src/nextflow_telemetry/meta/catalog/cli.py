"""CLI adapter for the source catalog; no lake or web-service initialization."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from .models import Access, AssetRole, SourceManifest
from .store import CatalogError, SourceCatalog


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("sources", help="catalog metadata sources; separate from lake loads")
    commands = p.add_subparsers(dest="source_command", required=True)
    for op in ("validate", "apply"):
        c = commands.add_parser(op)
        c.add_argument("manifest", type=Path)
        if op == "apply":
            c.add_argument("--actor", required=True)
            c.add_argument("--commit", action="store_true", help="write (default: preview only)")
    ls = commands.add_parser("list")
    ls.add_argument("--access", choices=[a.value for a in Access])
    ls.add_argument("--role", choices=[r.value for r in AssetRole])
    for op in ("show", "history"):
        commands.add_parser(op).add_argument("source_id")
    cap = commands.add_parser("capture", help="retain a full local file and record its checksum")
    cap.add_argument("source_id")
    cap.add_argument("asset_id")
    cap.add_argument("--version", required=True)
    cap.add_argument("--file", type=Path, required=True)
    cap.add_argument("--archive-dir", required=True,
                     help="private persistent directory, or an rclone path such as "
                          "r2:cmgd-private/metadata-sources (not a temporary download cache)")
    cap.add_argument("--actor", required=True)
    cap.add_argument("--commit", action="store_true", help="write (default: preview only)")


def _json(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


async def _run(a, manifest: SourceManifest | None) -> object:
    # No developer fallback DB: source-catalog operations must select a database
    # explicitly, including reads. No schema auto-creation or migrations here.
    uri = os.environ.get("SQLALCHEMY_URI", "")
    if not uri.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise CatalogError("set SQLALCHEMY_URI to the catalog PostgreSQL URI")
    uri = uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(uri)
    try:
        catalog = SourceCatalog(engine)
        if a.source_command == "apply":
            assert manifest is not None
            return await catalog.apply(manifest, actor=a.actor, commit=a.commit)
        if a.source_command == "list":
            return await catalog.list_sources(access=Access(a.access) if a.access else None,
                                      role=AssetRole(a.role) if a.role else None)
        if a.source_command == "show":
            return await catalog.show(a.source_id)
        if a.source_command == "history":
            return await catalog.history(a.source_id)
        return await catalog.capture(a.source_id, a.asset_id, version=a.version, file=a.file,
                                     archive_dir=a.archive_dir, actor=a.actor, commit=a.commit)
    finally:
        await engine.dispose()


def run(a) -> None:
    try:
        manifest = (SourceManifest.model_validate_json(a.manifest.read_text())
                    if a.source_command in {"validate", "apply"} else None)
        if a.source_command == "validate":
            assert manifest is not None
            out: object = {"valid": True, "manifest": manifest.model_dump(mode="json")}
        else:
            out = asyncio.run(_run(a, manifest))
    except ValidationError as e:
        # Pydantic's default rendering includes input values (possibly a rejected
        # credential-bearing URL). Report locations and messages, not those values.
        errors = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in e.errors(include_input=False, include_url=False))
        raise SystemExit(f"source catalog: invalid manifest: {errors}") from None
    except (CatalogError, OSError, ValueError) as e:
        raise SystemExit(f"source catalog: {e}") from None
    except SQLAlchemyError:
        # Engine errors can embed a DB URI or private record values. Don't print
        # those into shared agent transcripts; leave diagnostics to the operator.
        raise SystemExit("source catalog: database operation failed; check connectivity and migrations") from None
    print(json.dumps(out, default=_json, indent=2))
