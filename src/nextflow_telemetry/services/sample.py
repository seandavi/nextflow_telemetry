"""Sample catalog service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import samples_tbl
from ..utils import normalize_srrs, parse_srrs


@dataclass
class SampleService:
    engine: AsyncEngine

    async def register(
        self,
        sample_id: str,
        ncbi_accession: str | None = None,
        biosample_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Insert or update a sample; returns the row.

        ncbi_accession is normalised (sorted, deduplicated) before storage.
        metadata_ is replaced on conflict; ncbi_accession and biosample_id are
        updated only when explicitly supplied.
        """
        now = datetime.now(timezone.utc)
        normalised: str | None = None
        if ncbi_accession:
            normalised = normalize_srrs(parse_srrs(ncbi_accession))

        values: dict[str, Any] = {
            "sample_id": sample_id,
            "metadata_": metadata,
            "created_at": now,
            "updated_at": now,
        }
        if normalised is not None:
            values["ncbi_accession"] = normalised
        if biosample_id is not None:
            values["biosample_id"] = biosample_id

        set_: dict[str, Any] = {"metadata_": metadata, "updated_at": now}
        if normalised is not None:
            set_["ncbi_accession"] = normalised
        if biosample_id is not None:
            set_["biosample_id"] = biosample_id

        async with self.engine.begin() as conn:
            stmt = (
                pg_insert(samples_tbl)
                .values(**values)
                .on_conflict_do_update(index_elements=["sample_id"], set_=set_)
                .returning(*samples_tbl.c)
            )
            result = await conn.execute(stmt)
            return dict(result.mappings().one())

    async def list_samples(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        search: str | None = None,
        cohort: str | None = None,
    ) -> tuple[list[dict], int]:
        """Return (page_of_samples, total_matching_count).

        Filtering is server-side so the catalog is usable past the old
        client-side 1000-row ceiling (#118): ``search`` is a case-insensitive
        substring match on ``sample_id``; ``cohort`` matches
        ``metadata_->>'cohort'`` exactly.
        """
        conds = []
        if search:
            conds.append(samples_tbl.c.sample_id.ilike(f"%{search}%"))
        if cohort:
            conds.append(samples_tbl.c.metadata_["cohort"].astext == cohort)

        async with self.engine.connect() as conn:
            page = await conn.execute(
                select(samples_tbl).where(*conds)
                .order_by(samples_tbl.c.created_at.desc())
                .limit(limit).offset(offset)
            )
            rows = [dict(r) for r in page.mappings()]
            total = (await conn.execute(
                select(func.count()).select_from(samples_tbl).where(*conds)
            )).scalar_one()
        return rows, total

    async def cohort_facets(self) -> tuple[int, list[dict]]:
        """Return (total_samples, [{cohort, count}]) across the whole catalog.

        Powers the Samples-page cohort chips at scale — global counts that don't
        shift as the user pages. Uses the current free-text ``metadata_->>'cohort''``
        representation; this becomes membership-driven once the Epic A
        consolidation lands (docs/study-sample-version-identity.md).
        """
        cohort_expr = samples_tbl.c.metadata_["cohort"].astext
        async with self.engine.connect() as conn:
            total = (await conn.execute(
                select(func.count()).select_from(samples_tbl)
            )).scalar_one()
            rows = (await conn.execute(
                select(cohort_expr.label("cohort"), func.count().label("count"))
                .where(cohort_expr.isnot(None))
                .group_by(cohort_expr)
                .order_by(func.count().desc(), cohort_expr)
            )).mappings().all()
        return total, [{"cohort": r["cohort"], "count": r["count"]} for r in rows]

    async def get(self, sample_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(samples_tbl).where(samples_tbl.c.sample_id == sample_id)
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None

    async def get_by_srr(self, srr: str) -> dict | None:
        """Return the sample whose ncbi_accession contains the given SRR accession."""
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(samples_tbl).where(
                    samples_tbl.c.ncbi_accession.contains(srr)
                )
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None

    async def get_by_biosample(self, biosample_id: str) -> list[dict]:
        """Return all samples with the given biosample_id, newest first.

        Multiple rows can exist for one BioSample if SRRs were added to it over
        time (each distinct SRR set produces a distinct sample_id).
        """
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(samples_tbl)
                .where(samples_tbl.c.biosample_id == biosample_id)
                .order_by(samples_tbl.c.created_at.desc())
            )
            return [dict(r) for r in result.mappings()]
