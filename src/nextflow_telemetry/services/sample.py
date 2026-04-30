"""Sample catalog service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import samples_tbl


@dataclass
class SampleService:
    engine: AsyncEngine

    async def register(self, sample_id: str, metadata: dict[str, Any] | None = None) -> dict:
        """Insert or update a sample; returns the row."""
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            stmt = (
                pg_insert(samples_tbl)
                .values(
                    sample_id=sample_id,
                    metadata_=metadata,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["sample_id"],
                    set_={"metadata_": metadata, "updated_at": now},
                )
                .returning(*samples_tbl.c)
            )
            result = await conn.execute(stmt)
            return dict(result.mappings().one())

    async def list_samples(self, limit: int = 100, offset: int = 0) -> list[dict]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(samples_tbl).order_by(samples_tbl.c.created_at.desc())
                .limit(limit).offset(offset)
            )
            return [dict(r) for r in result.mappings()]

    async def get(self, sample_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(samples_tbl).where(samples_tbl.c.sample_id == sample_id)
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None
