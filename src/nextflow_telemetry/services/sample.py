"""Sample catalog service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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
