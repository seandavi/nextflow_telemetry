"""Curated sample annotations service.

Handles import and querying of curatedMetagenomicData-style TSV files.
Each TSV represents a study; rows become curated_sample_annotations entries
keyed by the content-addressed sample_id (md5 of sorted SRR accessions).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import curated_sample_annotations_tbl, curated_studies_tbl
from ..utils import parse_srrs, srrs_to_sample_id


# ---------------------------------------------------------------------------
# Data-transfer objects
# ---------------------------------------------------------------------------

@dataclass
class DroppedRow:
    """Describes a row that was skipped during import (missing ncbi_accession)."""
    row_index: int
    subject_id: str | None = None


@dataclass
class ImportSummary:
    """Summary returned by CuratedService.import_tsv()."""
    study_name: str
    rows_loaded: int
    rows_updated: int
    rows_dropped: int
    dropped_rows: list[DroppedRow] = field(default_factory=list)


@dataclass
class StudyRow:
    """Represents a row from curated_studies."""
    id: int
    study_name: str
    source_file: str | None
    metadata_: dict[str, Any] | None
    loaded_at: datetime


@dataclass
class AnnotationRow:
    """Represents a row from curated_sample_annotations."""
    id: int
    sample_id: str
    study_name: str
    ncbi_accession: str | None
    metadata_: dict[str, Any]
    loaded_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

@dataclass
class CuratedService:
    """Business logic for curated study imports and queries."""

    engine: AsyncEngine

    async def import_tsv(
        self,
        tsv_content: bytes,
        study_name: str,
        source_file: str | None = None,
        pubmed_id: str | None = None,
        doi: str | None = None,
    ) -> ImportSummary:
        """Parse a TSV and upsert rows into curated_studies / curated_sample_annotations.

        Steps:
        1. Decode the bytes as UTF-8 and parse with csv.DictReader (tab-separated).
        2. Locate the ncbi_accession column case-insensitively; raise ValueError if absent.
        3. For each row:
           - If ncbi_accession is null/empty → record as a dropped row.
           - If parse_srrs() returns an empty list → record as a dropped row.
           - Otherwise compute sample_id = srrs_to_sample_id(parse_srrs(ncbi_accession))
             and build a metadata_ JSONB from all columns except ncbi_accession
             (None and empty-string keys are excluded to avoid JSONB errors).
        4. Upsert curated_studies first (preserving existing metadata_ when new
           import has no pubmed_id/doi), then bulk-upsert all annotation rows.
        5. Return an ImportSummary.
        """
        text = tsv_content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")

        if reader.fieldnames is None:
            return ImportSummary(
                study_name=study_name,
                rows_loaded=0,
                rows_updated=0,
                rows_dropped=0,
            )

        # Case-insensitive lookup for the ncbi_accession column
        fieldnames: list[str] = list(reader.fieldnames)
        accession_col: str | None = None
        for col in fieldnames:
            if col.lower() == "ncbi_accession":
                accession_col = col
                break
        if accession_col is None:
            raise ValueError(
                "TSV is missing an 'ncbi_accession' column (case-insensitive). "
                f"Found columns: {fieldnames}"
            )

        now = datetime.now(timezone.utc)
        study_meta: dict[str, Any] = {}
        if pubmed_id is not None:
            study_meta["pubmed_id"] = pubmed_id
        if doi is not None:
            study_meta["doi"] = doi

        annotation_rows: list[dict[str, Any]] = []
        dropped: list[DroppedRow] = []

        for row_index, row in enumerate(reader):
            raw_accession: str = (row.get(accession_col) or "").strip()

            # Derive a human-readable subject label for dropped-row reporting.
            subject_id: str | None = (
                row.get("subject_id")
                or row.get("sample_id")
                or row.get("Subject_id")
                or row.get("Sample_id")
            )

            if not raw_accession:
                dropped.append(DroppedRow(row_index=row_index, subject_id=subject_id))
                continue

            # Fix #2: treat rows where parse_srrs yields no valid SRRs as dropped.
            srrs = parse_srrs(raw_accession)
            if not srrs:
                dropped.append(
                    DroppedRow(
                        row_index=row_index,
                        subject_id=subject_id,
                    )
                )
                continue

            sample_id = srrs_to_sample_id(srrs)

            # Fix #3: exclude None keys (csv.DictReader overflow columns) and the
            # accession column itself from the metadata_ dict to avoid JSONB errors.
            acol = accession_col
            meta: dict[str, Any] = {
                k: v
                for k, v in row.items()
                if k is not None and k != "" and k != acol
            }

            annotation_rows.append({
                "sample_id": sample_id,
                "study_name": study_name,
                "ncbi_accession": raw_accession,
                "metadata_": meta,
                "loaded_at": now,
            })

        async with self.engine.begin() as conn:
            # 1. Upsert the study record.
            # Fix #1: only update metadata_ when the new import provides non-empty
            # study_meta — this prevents overwriting previously stored pubmed_id/doi
            # with NULL when re-importing without those fields.
            study_values: dict[str, Any] = {
                "study_name": study_name,
                "source_file": source_file,
                "metadata_": study_meta or None,
                "loaded_at": now,
            }
            conflict_set: dict[str, Any] = {
                "source_file": source_file,
                "loaded_at": now,
            }
            if study_meta:
                conflict_set["metadata_"] = study_meta

            study_stmt = (
                pg_insert(curated_studies_tbl)
                .values(**study_values)
                .on_conflict_do_update(
                    index_elements=["study_name"],
                    set_=conflict_set,
                )
            )
            await conn.execute(study_stmt)

            # Fix #4: count pre-existing annotation rows for this study to compute
            # rows_updated accurately.
            existing_sample_ids: set[str] = set()
            if annotation_rows:
                candidate_ids = [r["sample_id"] for r in annotation_rows]
                existing_result = await conn.execute(
                    select(curated_sample_annotations_tbl.c.sample_id).where(
                        curated_sample_annotations_tbl.c.study_name == study_name,
                        curated_sample_annotations_tbl.c.sample_id.in_(candidate_ids),
                    )
                )
                existing_sample_ids = {row[0] for row in existing_result}

            rows_updated = len(existing_sample_ids)

            # Fix #5: bulk-upsert all annotation rows in a single round-trip instead
            # of one execute() call per row.
            if annotation_rows:
                ann_insert = pg_insert(curated_sample_annotations_tbl).values(
                    annotation_rows
                )
                ann_stmt = ann_insert.on_conflict_do_update(
                    constraint="uq_csa_sample_study",
                    set_={
                        "ncbi_accession": ann_insert.excluded.ncbi_accession,
                        "metadata_": ann_insert.excluded.metadata_,
                        "loaded_at": ann_insert.excluded.loaded_at,
                    },
                )
                await conn.execute(ann_stmt)

        return ImportSummary(
            study_name=study_name,
            rows_loaded=len(annotation_rows),
            rows_updated=rows_updated,
            rows_dropped=len(dropped),
            dropped_rows=dropped,
        )

    async def list_studies(self) -> list[StudyRow]:
        """Return all studies ordered by name."""
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(curated_studies_tbl).order_by(curated_studies_tbl.c.study_name)
            )
            return [_map_study(row) for row in result.mappings()]

    async def get_study(self, study_name: str) -> StudyRow | None:
        """Return a single study by name, or None if not found."""
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(curated_studies_tbl).where(
                    curated_studies_tbl.c.study_name == study_name
                )
            )
            row = result.mappings().one_or_none()
            return _map_study(row) if row else None

    async def list_study_samples(
        self, study_name: str, limit: int = 100, offset: int = 0
    ) -> list[AnnotationRow]:
        """Return paginated annotations for a given study."""
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(curated_sample_annotations_tbl)
                .where(curated_sample_annotations_tbl.c.study_name == study_name)
                .order_by(curated_sample_annotations_tbl.c.id)
                .limit(limit)
                .offset(offset)
            )
            return [_map_annotation(row) for row in result.mappings()]

    async def get_sample_annotations(self, sample_id: str) -> list[AnnotationRow]:
        """Return all curated annotations for a given sample_id across all studies."""
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(curated_sample_annotations_tbl)
                .where(curated_sample_annotations_tbl.c.sample_id == sample_id)
                .order_by(curated_sample_annotations_tbl.c.study_name)
            )
            return [_map_annotation(row) for row in result.mappings()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _map_study(row: Any) -> StudyRow:
    return StudyRow(
        id=row["id"],
        study_name=row["study_name"],
        source_file=row["source_file"],
        metadata_=row["metadata_"],
        loaded_at=row["loaded_at"],
    )


def _map_annotation(row: Any) -> AnnotationRow:
    return AnnotationRow(
        id=row["id"],
        sample_id=row["sample_id"],
        study_name=row["study_name"],
        ncbi_accession=row["ncbi_accession"],
        metadata_=row["metadata_"],
        loaded_at=row["loaded_at"],
    )
