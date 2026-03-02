from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class SamplesService:
    engine: AsyncEngine

    async def create(self, *, sample_id: str, srr_accessions: Optional[list[str]] = None, metadata_: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        sql = text("""
            INSERT INTO samples (sample_id, srr_accessions, metadata_)
            VALUES (:sample_id, :srr_accessions::jsonb, :metadata_::jsonb)
            RETURNING id, sample_id, srr_accessions, metadata_, created_at, updated_at
        """)
        params = {
            "sample_id": sample_id,
            "srr_accessions": json.dumps(srr_accessions) if srr_accessions is not None else None,
            "metadata_": json.dumps(metadata_) if metadata_ is not None else None,
        }
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(sql, params)).mappings().one()
                return dict(row)
        except IntegrityError as exc:
            raise ValueError(f"Sample '{sample_id}' already exists") from exc

    async def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        count_sql = text("SELECT count(*) FROM samples")
        list_sql = text("""
            SELECT id, sample_id, srr_accessions, metadata_, created_at, updated_at
            FROM samples ORDER BY id LIMIT :limit OFFSET :offset
        """)
        async with self.engine.connect() as conn:
            total = (await conn.execute(count_sql)).scalar_one()
            rows = [dict(r) for r in (await conn.execute(list_sql, {"limit": limit, "offset": offset})).mappings().all()]
        return {"items": rows, "total": total}

    async def get(self, sample_id: str) -> Optional[dict[str, Any]]:
        sql = text("""
            SELECT id, sample_id, srr_accessions, metadata_, created_at, updated_at
            FROM samples WHERE sample_id = :sample_id
        """)
        async with self.engine.connect() as conn:
            row = (await conn.execute(sql, {"sample_id": sample_id})).mappings().one_or_none()
            return dict(row) if row else None

    async def update(self, sample_id: str, *, srr_accessions: Any = ..., metadata_: Any = ...) -> Optional[dict[str, Any]]:
        sets: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"sample_id": sample_id}
        if srr_accessions is not ...:
            sets.append("srr_accessions = :srr_accessions::jsonb")
            params["srr_accessions"] = json.dumps(srr_accessions) if srr_accessions is not None else None
        if metadata_ is not ...:
            sets.append("metadata_ = :metadata_::jsonb")
            params["metadata_"] = json.dumps(metadata_) if metadata_ is not None else None

        if len(sets) == 1:
            return await self.get(sample_id)

        sql = text(f"""
            UPDATE samples SET {', '.join(sets)}
            WHERE sample_id = :sample_id
            RETURNING id, sample_id, srr_accessions, metadata_, created_at, updated_at
        """)
        async with self.engine.begin() as conn:
            row = (await conn.execute(sql, params)).mappings().one_or_none()
            return dict(row) if row else None

    async def delete(self, sample_id: str) -> bool:
        sql = text("DELETE FROM samples WHERE sample_id = :sample_id RETURNING id")
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(sql, {"sample_id": sample_id})).one_or_none()
                return row is not None
        except IntegrityError as exc:
            raise ValueError(f"Cannot delete sample '{sample_id}': referenced by jobs") from exc
