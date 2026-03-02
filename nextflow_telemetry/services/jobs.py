from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

VALID_STATUSES = {"pending", "running", "success", "failure"}


@dataclass
class JobsService:
    engine: AsyncEngine

    async def create(self, *, sample_id: str, pipeline_id: str) -> dict[str, Any]:
        """Create a job by resolving human-readable sample_id and pipeline_id to FK integers."""
        sql = text("""
            INSERT INTO jobs (sample_id, pipeline_id)
            SELECT s.id, p.id
            FROM samples s, pipelines p
            WHERE s.sample_id = :sample_id AND p.pipeline_id = :pipeline_id
            RETURNING id, sample_id AS sample_fk, pipeline_id AS pipeline_fk, status, submitted_at, updated_at
        """)
        async with self.engine.begin() as conn:
            row = (await conn.execute(sql, {"sample_id": sample_id, "pipeline_id": pipeline_id})).mappings().one_or_none()
            if row is None:
                raise ValueError(f"Sample '{sample_id}' or pipeline '{pipeline_id}' not found")
            # Re-query to get human-readable IDs via JOIN
            return await self._get_by_pk(conn, row["id"])

    async def _get_by_pk(self, conn: Any, job_id: int) -> dict[str, Any]:
        sql = text("""
            SELECT j.id, s.sample_id, p.pipeline_id, j.status, j.submitted_at, j.updated_at
            FROM jobs j
            JOIN samples s ON j.sample_id = s.id
            JOIN pipelines p ON j.pipeline_id = p.id
            WHERE j.id = :id
        """)
        row = (await conn.execute(sql, {"id": job_id})).mappings().one()
        return dict(row)

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        sample_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
    ) -> dict[str, Any]:
        wheres: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if status is not None:
            wheres.append("j.status = :status")
            params["status"] = status
        if sample_id is not None:
            wheres.append("s.sample_id = :sample_id")
            params["sample_id"] = sample_id
        if pipeline_id is not None:
            wheres.append("p.pipeline_id = :pipeline_id")
            params["pipeline_id"] = pipeline_id

        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        count_sql = text(f"""
            SELECT count(*)
            FROM jobs j
            JOIN samples s ON j.sample_id = s.id
            JOIN pipelines p ON j.pipeline_id = p.id
            {where_clause}
        """)
        list_sql = text(f"""
            SELECT j.id, s.sample_id, p.pipeline_id, j.status, j.submitted_at, j.updated_at
            FROM jobs j
            JOIN samples s ON j.sample_id = s.id
            JOIN pipelines p ON j.pipeline_id = p.id
            {where_clause}
            ORDER BY j.id
            LIMIT :limit OFFSET :offset
        """)

        async with self.engine.connect() as conn:
            total = (await conn.execute(count_sql, params)).scalar_one()
            rows = [dict(r) for r in (await conn.execute(list_sql, params)).mappings().all()]
        return {"items": rows, "total": total}

    async def get(self, job_id: int) -> Optional[dict[str, Any]]:
        sql = text("""
            SELECT j.id, s.sample_id, p.pipeline_id, j.status, j.submitted_at, j.updated_at
            FROM jobs j
            JOIN samples s ON j.sample_id = s.id
            JOIN pipelines p ON j.pipeline_id = p.id
            WHERE j.id = :id
        """)
        async with self.engine.connect() as conn:
            row = (await conn.execute(sql, {"id": job_id})).mappings().one_or_none()
            return dict(row) if row else None

    async def update_status(self, job_id: int, status: str) -> Optional[dict[str, Any]]:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}")

        sql = text("""
            UPDATE jobs SET status = :status, updated_at = now()
            WHERE id = :id
            RETURNING id
        """)
        async with self.engine.begin() as conn:
            row = (await conn.execute(sql, {"id": job_id, "status": status})).one_or_none()
            if row is None:
                return None
            return await self._get_by_pk(conn, job_id)
