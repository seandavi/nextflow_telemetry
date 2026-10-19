"""Metadata source catalog: audited descriptors and immutable capture receipts.

This module never registers processing samples, fetches external sources or opens
 the analytical lake. PostgreSQL is the inventory; archive.py retains raw bytes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ...db import (
    metadata_assets_tbl as assets,
    metadata_source_events_tbl as events,
    metadata_snapshots_tbl as snapshots,
    metadata_sources_tbl as sources,
)
from . import archive
from .models import Access, AssetRole, SourceManifest


class CatalogError(ValueError):
    """A missing record or conflicting immutable version (not a DB outage)."""


def _text(value: str, label: str, max_length: int = 300) -> str:
    value = value.strip()
    if not value or len(value) > max_length:
        raise CatalogError(f"{label} must contain 1–{max_length} characters")
    return value


@dataclass
class SourceCatalog:
    engine: AsyncEngine

    async def apply(self, manifest: SourceManifest, *, actor: str,
                    commit: bool = False) -> dict:
        """Apply one source atomically; omitted assets remain, repeats are no-ops.

        Committed operations serialize per source. Preview never writes, including
        no audit records or schema creation. It is a plan, not a reservation.
        """
        actor = _text(actor, "actor")
        source = manifest.source.model_dump(mode="json")
        sid = source["source_id"]
        now = datetime.now(UTC)
        changes = []
        async with self.engine.begin() as con:
            created = False
            if commit:
                created = (await con.execute(insert(sources).values(
                    source_id=sid, descriptor=source, created_at=now, updated_at=now,
                ).on_conflict_do_nothing().returning(sources.c.source_id))).scalar_one_or_none() is not None
            q = select(sources).where(sources.c.source_id == sid)
            row = (await con.execute(q.with_for_update() if commit else q)).mappings().one_or_none()
            before = None if created or row is None else row["descriptor"]
            descriptors: list[tuple[str | None, dict | None, dict]] = [(None, before, source)]
            old_assets = {r["asset_id"]: r["descriptor"] for r in (await con.execute(
                select(assets).where(assets.c.source_id == sid)
            )).mappings()}
            descriptors += [(a.asset_id, old_assets.get(a.asset_id), a.model_dump(mode="json"))
                            for a in manifest.assets]
            for aid, old, new in descriptors:
                if old == new:
                    continue
                changes.append({"asset_id": aid, "action": "added" if old is None else "updated"})
                if not commit:
                    continue
                if aid is None:
                    if not created:
                        await con.execute(update(sources).where(sources.c.source_id == sid).values(
                            descriptor=new, updated_at=now))
                else:
                    await con.execute(insert(assets).values(
                        source_id=sid, asset_id=aid, descriptor=new, created_at=now, updated_at=now,
                    ).on_conflict_do_update(index_elements=[assets.c.source_id, assets.c.asset_id],
                                            set_={"descriptor": new, "updated_at": now}))
                await con.execute(events.insert().values(
                    source_id=sid, asset_id=aid, actor=actor, before=old, after=new, recorded_at=now))
        return {"source_id": sid, "committed": commit, "changes": changes}

    async def list_sources(self, *, access: Access | None = None,
                           role: AssetRole | None = None) -> list[dict]:
        """Source descriptors; role filters by any asset, access by the source."""
        q = select(sources.c.descriptor).order_by(sources.c.source_id)
        if access is not None:
            q = q.where(sources.c.descriptor["access"].astext == access.value)
        if role is not None:
            q = q.where(sources.c.source_id.in_(select(assets.c.source_id).where(
                assets.c.descriptor["role"].astext == role.value)))
        async with self.engine.connect() as con:
            return list((await con.execute(q)).scalars())

    async def show(self, source_id: str) -> dict:
        async with self.engine.connect() as con:
            row = (await con.execute(select(sources).where(
                sources.c.source_id == source_id))).mappings().one_or_none()
            if row is None:
                raise CatalogError(f"unknown source: {source_id}")
            asset_rows = [dict(r) for r in (await con.execute(select(assets).where(
                assets.c.source_id == source_id).order_by(assets.c.asset_id))).mappings()]
            captures = [dict(r) for r in (await con.execute(select(snapshots).where(
                snapshots.c.source_id == source_id).order_by(
                    snapshots.c.asset_id, snapshots.c.captured_at, snapshots.c.version))).mappings()]
            return {**dict(row), "assets": asset_rows, "snapshots": captures}

    async def snapshot(self, source_id: str, asset_id: str, version: str) -> dict:
        """One capture receipt, or CatalogError. Loaders start here."""
        async with self.engine.connect() as con:
            row = (await con.execute(select(snapshots).where(
                snapshots.c.source_id == source_id, snapshots.c.asset_id == asset_id,
                snapshots.c.version == version))).mappings().one_or_none()
        if row is None:
            raise CatalogError(f"no snapshot {source_id}/{asset_id}@{version}; capture it first")
        return dict(row)

    async def history(self, source_id: str) -> list[dict]:
        async with self.engine.connect() as con:
            if (await con.execute(select(sources.c.source_id).where(
                    sources.c.source_id == source_id))).scalar_one_or_none() is None:
                raise CatalogError(f"unknown source: {source_id}")
            return [dict(r) for r in (await con.execute(select(events).where(
                events.c.source_id == source_id).order_by(events.c.id))).mappings()]

    async def capture(self, source_id: str, asset_id: str, *, version: str, file: Path,
                      archive_dir: str | Path, actor: str, commit: bool = False) -> dict:
        """Capture a whole local file, retaining its bytes before receipt commit.

        A version is operator-supplied: a commit SHA, Zenodo record/file version,
        or a timestamped sheet export. Content must be immutable under that key.
        Snapshots are observations of source bytes, not a promise of data quality.
        """
        actor, version = _text(actor, "actor"), _text(version, "version")
        async with self.engine.begin() as con:
            q = select(sources.c.descriptor).where(sources.c.source_id == source_id)
            source = (await con.execute(q.with_for_update() if commit else q)).scalar_one_or_none()
            if source is None:
                raise CatalogError(f"unknown source: {source_id}")
            asset = (await con.execute(select(assets.c.descriptor).where(
                assets.c.source_id == source_id, assets.c.asset_id == asset_id))).scalar_one_or_none()
            if asset is None:
                raise CatalogError(f"unknown asset: {source_id}/{asset_id}")
            inspected = await asyncio.to_thread(archive.inspect_file, file, archive_dir)
            old = (await con.execute(select(snapshots).where(
                snapshots.c.source_id == source_id, snapshots.c.asset_id == asset_id,
                snapshots.c.version == version))).mappings().one_or_none()
            if old is not None:
                if (old["sha256"], old["size_bytes"]) != (inspected.sha256, inspected.size_bytes):
                    raise CatalogError("version already captured with different bytes; use a new version")
                if commit:
                    # The first capture's URI is authoritative. Don't silently relocate
                    # receipts if someone chooses a different archive root on a repeat.
                    await asyncio.to_thread(archive.verify, old["artifact_uri"], inspected)
                return {"committed": commit, "action": "unchanged", "snapshot": dict(old)}
            row = {
                "source_id": source_id, "asset_id": asset_id, "version": version,
                "sha256": inspected.sha256, "size_bytes": inspected.size_bytes,
                "artifact_uri": inspected.uri, "source_descriptor": source, "asset_descriptor": asset,
                "captured_at": datetime.now(UTC), "captured_by": actor,
            }
            if commit:
                await asyncio.to_thread(archive.retain, file, archive_dir, inspected)
                await con.execute(snapshots.insert().values(**row))
            return {"committed": commit, "action": "added", "snapshot": row}
