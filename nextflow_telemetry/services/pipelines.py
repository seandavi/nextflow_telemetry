from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class PipelinesService:
    engine: AsyncEngine

    async def create(
        self,
        *,
        pipeline_id: str,
        repository: Optional[str] = None,
        branch: Optional[str] = "main",
        description: Optional[str] = None,
        default_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        sql = text("""
            INSERT INTO pipelines (pipeline_id, repository, branch, description, default_params)
            VALUES (:pipeline_id, :repository, :branch, :description, :default_params::jsonb)
            RETURNING id, pipeline_id, repository, branch, description, default_params, created_at, updated_at
        """)
        params = {
            "pipeline_id": pipeline_id,
            "repository": repository,
            "branch": branch,
            "description": description,
            "default_params": json.dumps(default_params) if default_params is not None else None,
        }
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(sql, params)).mappings().one()
                return dict(row)
        except IntegrityError as exc:
            raise ValueError(f"Pipeline '{pipeline_id}' already exists") from exc

    async def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        count_sql = text("SELECT count(*) FROM pipelines")
        list_sql = text("""
            SELECT id, pipeline_id, repository, branch, description, default_params, created_at, updated_at
            FROM pipelines ORDER BY id LIMIT :limit OFFSET :offset
        """)
        async with self.engine.connect() as conn:
            total = (await conn.execute(count_sql)).scalar_one()
            rows = [dict(r) for r in (await conn.execute(list_sql, {"limit": limit, "offset": offset})).mappings().all()]
        return {"items": rows, "total": total}

    async def get(self, pipeline_id: str) -> Optional[dict[str, Any]]:
        sql = text("""
            SELECT id, pipeline_id, repository, branch, description, default_params, created_at, updated_at
            FROM pipelines WHERE pipeline_id = :pipeline_id
        """)
        async with self.engine.connect() as conn:
            row = (await conn.execute(sql, {"pipeline_id": pipeline_id})).mappings().one_or_none()
            return dict(row) if row else None

    async def update(self, pipeline_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        sets: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"pipeline_id": pipeline_id}

        for field in ("repository", "branch", "description"):
            if field in kwargs:
                sets.append(f"{field} = :{field}")
                params[field] = kwargs[field]

        if "default_params" in kwargs:
            sets.append("default_params = :default_params::jsonb")
            params["default_params"] = json.dumps(kwargs["default_params"]) if kwargs["default_params"] is not None else None

        if len(sets) == 1:
            return await self.get(pipeline_id)

        sql = text(f"""
            UPDATE pipelines SET {', '.join(sets)}
            WHERE pipeline_id = :pipeline_id
            RETURNING id, pipeline_id, repository, branch, description, default_params, created_at, updated_at
        """)
        async with self.engine.begin() as conn:
            row = (await conn.execute(sql, params)).mappings().one_or_none()
            return dict(row) if row else None

    async def delete(self, pipeline_id: str) -> bool:
        sql = text("DELETE FROM pipelines WHERE pipeline_id = :pipeline_id RETURNING id")
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(sql, {"pipeline_id": pipeline_id})).one_or_none()
                return row is not None
        except IntegrityError as exc:
            raise ValueError(f"Cannot delete pipeline '{pipeline_id}': referenced by jobs") from exc
