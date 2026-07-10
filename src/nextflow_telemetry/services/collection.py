"""Collection membership — the single write seam for putting samples in a collection.

Every registration path (accession submission, single-sample register, curated
import later) routes its membership write through `add_to_collection`. See
docs/adr/0005-single-collection-membership-seam.md and CONTEXT.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db import collection_samples_tbl, collections_tbl


async def add_to_collection(
    conn: AsyncConnection,
    collection_id: str,
    *,
    source: str,
    type_: str | None = None,
    label: str | None = None,
    sample_ids: list[str],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Upsert a collection and attach samples to it, within the caller's transaction.

    Takes a live connection (not an engine) so "insert samples + attach
    membership" stays atomic in the caller's `engine.begin()` block. Idempotent:
    re-attaching an existing (collection, sample) pair is a no-op; re-declaring a
    collection only bumps `updated_at`. The samples must already be inserted in
    the same transaction (FK on `collection_samples.sample_id`).
    """
    now = datetime.now(timezone.utc)
    await conn.execute(
        pg_insert(collections_tbl)
        .values(
            collection_id=collection_id,
            source=source,
            type=type_,
            label=label if label is not None else collection_id,
            metadata_=metadata,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[collections_tbl.c.collection_id],
            set_={"updated_at": now},
        )
    )
    if sample_ids:
        await conn.execute(
            pg_insert(collection_samples_tbl)
            .values([{"collection_id": collection_id, "sample_id": sid} for sid in sample_ids])
            .on_conflict_do_nothing(constraint="uq_collection_sample")
        )
