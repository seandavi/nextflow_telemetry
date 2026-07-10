"""Sample catalog service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import collection_samples_tbl, samples_tbl
from ..utils import normalize_srrs, parse_srrs
from .collection import add_to_collection


@dataclass
class SampleService:
    engine: AsyncEngine

    async def register(
        self,
        sample_id: str,
        ncbi_accession: str | None = None,
        biosample_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        collection: str | None = None,
    ) -> dict:
        """Insert or update a sample; returns the row.

        ncbi_accession is normalised (sorted, deduplicated) before storage.
        metadata_ is replaced on conflict; ncbi_accession and biosample_id are
        updated only when explicitly supplied. When ``collection`` is given, the
        sample is attached to that collection (the single membership write seam)
        in the same transaction.
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
            row = dict(result.mappings().one())
            if collection:
                await add_to_collection(
                    conn, collection, source="manual", sample_ids=[sample_id]
                )
            # Reflect the sample's current membership in the response (all
            # collections, not just one just-added — the sample may already
            # belong to others), consistent with what list_samples returns.
            mrows = await conn.execute(
                select(collection_samples_tbl.c.collection_id)
                .where(collection_samples_tbl.c.sample_id == sample_id)
                .order_by(collection_samples_tbl.c.collection_id)
            )
            row["collections"] = [m[0] for m in mrows.all()]
            return row

    async def list_samples(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        search: str | None = None,
        collection: str | None = None,
    ) -> tuple[list[dict], int]:
        """Return (page_of_samples, total_matching_count).

        Each returned row carries a ``collections`` list (its collection_ids).
        Filtering is server-side so the catalog is usable past the old
        client-side 1000-row ceiling (#118): ``search`` is a case-insensitive
        substring match on ``sample_id``; ``collection`` matches membership in a
        collection (via ``collection_samples``) — the single source of truth,
        not the retired ``metadata.cohort`` scalar.
        """
        conds = []
        if search:
            conds.append(samples_tbl.c.sample_id.ilike(f"%{search}%"))
        if collection:
            conds.append(samples_tbl.c.sample_id.in_(
                select(collection_samples_tbl.c.sample_id)
                .where(collection_samples_tbl.c.collection_id == collection)
            ))

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

            # Attach each sample's collection memberships (one extra query for
            # the page, not a GROUP BY on the main list query).
            sample_ids = [r["sample_id"] for r in rows]
            memberships: dict[str, list[str]] = {}
            if sample_ids:
                mrows = await conn.execute(
                    select(collection_samples_tbl.c.sample_id, collection_samples_tbl.c.collection_id)
                    .where(collection_samples_tbl.c.sample_id.in_(sample_ids))
                    .order_by(collection_samples_tbl.c.collection_id)
                )
                for m in mrows.mappings():
                    memberships.setdefault(m["sample_id"], []).append(m["collection_id"])
            for r in rows:
                r["collections"] = memberships.get(r["sample_id"], [])
        return rows, total

    async def collection_facets(self) -> tuple[int, list[dict]]:
        """Return (total_samples, [{collection, count}]) across the whole catalog.

        Powers the Samples-page collection chips at scale — global counts that
        don't shift as the user pages. Membership-driven (``collection_samples``),
        the same source the Cohorts dashboard reads, so the two agree. Counts are
        overlap-allowed: a sample in N collections contributes to N chips, so the
        chip counts need not sum to ``total`` (many-to-many membership).
        """
        async with self.engine.connect() as conn:
            total = (await conn.execute(
                select(func.count()).select_from(samples_tbl)
            )).scalar_one()
            rows = (await conn.execute(
                select(
                    collection_samples_tbl.c.collection_id.label("collection"),
                    func.count().label("count"),
                )
                .group_by(collection_samples_tbl.c.collection_id)
                .order_by(func.count().desc(), collection_samples_tbl.c.collection_id)
            )).mappings().all()
        return total, [{"collection": r["collection"], "count": r["count"]} for r in rows]

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
