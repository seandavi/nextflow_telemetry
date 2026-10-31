"""Samples (single and bulk), lookups, facets, workflows, job summary and job list (ADR-0013 #373).

Every function takes the caller's ``AsyncConnection`` and leaves the transaction to it.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, case, delete, func, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from .. import readset
from ..control_db import (
    collection_samples_tbl,
    dead_letter_tbl,
    jobs_tbl,
    readset_runs_tbl,
    samples_tbl,
    workflows_tbl,
)
from ..control_models import RegisterWorkflowRequest, SampleRegistration

BULK_MAX_ROWS = 100_000
BULK_MAX_ERRORS = 20
_TSV_COLUMNS = ("sample_id", "ncbi_accession", "biosample_id", "collection")


class ReadsetConflict(Exception):
    def __init__(self, runs: list[str]) -> None:
        super().__init__(", ".join(runs))
        self.runs = runs


class BulkParseError(Exception):
    """The message names the offending line."""


class BulkTooLarge(BulkParseError):
    """More than ``BULK_MAX_ROWS`` rows."""


@dataclass(frozen=True)
class RegistrationResult:
    received: int
    samples_inserted: int
    samples_updated: int
    collections_created: int
    memberships_added: int
    readset_runs_added: int


# ---------------------------------------------------------------- bulk parsing


def parse_bulk(body: bytes, media_type: str, default_collection: str | None) -> list[SampleRegistration]:
    """Parse an NDJSON or TSV registration body; raises ``BulkParseError`` naming the bad lines."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as err:
        raise BulkParseError(f"body is not UTF-8: {err}") from err

    candidates: list[tuple[int, dict[str, Any]]] = []
    errors: list[str] = []
    if media_type == "application/x-ndjson":
        for number, line in enumerate(decoded.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as err:
                errors.append(f"line {number}: invalid JSON: {err.msg}")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {number}: expected a JSON object")
                continue
            candidates.append((number, obj))
    else:
        reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
        if not {"sample_id", "ncbi_accession"} <= set(reader.fieldnames or ()):
            raise BulkParseError("line 1: header must include sample_id and ncbi_accession")
        for record in reader:
            cells = {k: (record.get(k) or None) for k in _TSV_COLUMNS if k in record}
            candidates.append((reader.line_num, cells))
    if len(candidates) > BULK_MAX_ROWS:
        raise BulkTooLarge(f"more than {BULK_MAX_ROWS} rows")

    rows: list[SampleRegistration] = []
    for number, obj in candidates:
        if default_collection and not obj.get("collection"):
            obj = {**obj, "collection": default_collection}
        try:
            rows.append(SampleRegistration.model_validate(obj))
        except ValidationError as err:
            errors.extend(
                f"line {number}: {'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in err.errors()
            )
    if errors:
        raise BulkParseError("; ".join(errors[:BULK_MAX_ERRORS]))
    return rows


# ------------------------------------------------------------------ samples

_STAGED = """
    select distinct on (sample_id) sample_id, readset_id, ncbi_accession, biosample_id, metadata, collection
      from sample_stage order by sample_id, line desc
"""
_CONFLICTING_RUNS = text(
    f"""
    with staged as ({_STAGED}),
    runs as (
      select distinct readset_id, unnest(string_to_array(ncbi_accession, ';')) as run
        from staged where readset_id is not null)
    select run from (
      select r.run from runs r join readset_runs x on x.run_accession = r.run where x.readset_id <> r.readset_id
      union
      select run from runs group by run having count(*) > 1
    ) c order by run limit {BULK_MAX_ERRORS}
    """
)
_INSERT_SAMPLES = text(
    f"""
    insert into samples (sample_id, readset_id, ncbi_accession, biosample_id, metadata, created_at, updated_at)
    select sample_id, readset_id, ncbi_accession, biosample_id, metadata::jsonb, cast(:now as timestamptz), cast(:now as timestamptz)
      from ({_STAGED}) staged
    on conflict (sample_id) do update set
      readset_id = excluded.readset_id, ncbi_accession = excluded.ncbi_accession,
      biosample_id = excluded.biosample_id, metadata = excluded.metadata, updated_at = excluded.updated_at
    returning (xmax = 0) as inserted
    """
)
_INSERT_COLLECTIONS = text(
    """
    insert into collections (collection_id, source, created_at, updated_at)
    select c,
           case when c like 'PRJ%' then 'bioproject' when c ~ '^[DES]RP[0-9]+$' then 'sra_study' else 'manual' end,
           cast(:now as timestamptz), cast(:now as timestamptz)
      from (select distinct collection as c from sample_stage where collection is not null) x
    on conflict (collection_id) do update set updated_at = excluded.updated_at
    returning (xmax = 0) as inserted
    """
)
_INSERT_MEMBERSHIPS = text(
    """
    insert into collection_samples (collection_id, sample_id, created_at)
    select distinct collection, sample_id, cast(:now as timestamptz) from sample_stage where collection is not null
    on conflict do nothing
    """
)
_INSERT_READSET_RUNS = text(
    f"""
    insert into readset_runs (run_accession, readset_id)
    select distinct run, readset_id from (
      select unnest(string_to_array(ncbi_accession, ';')) as run, readset_id
        from ({_STAGED}) staged where readset_id is not null) r
    on conflict do nothing
    """
)


async def register_samples(
    conn: AsyncConnection, rows: Sequence[SampleRegistration], *, now: datetime
) -> RegistrationResult:
    """Upsert samples, their collections, memberships and readset runs: COPY into a staging table, then merge."""
    staged = []
    for line, row in enumerate(rows, start=1):
        accessions = readset.normalize_runs(row.ncbi_accession)
        staged.append(
            (
                line,
                row.sample_id,
                readset.readset_id_for_runs(accessions),
                accessions,
                row.biosample_id,
                json.dumps(row.metadata or {}),
                row.collection,
            )
        )
    # This first statement also opens the transaction on the driver connection, so the COPY joins it.
    await conn.execute(
        text(
            "create temp table if not exists sample_stage (line int, sample_id text, readset_id text, "
            "ncbi_accession text, biosample_id text, metadata text, collection text) on commit drop"
        )
    )
    await conn.execute(text("truncate sample_stage"))
    raw = await conn.get_raw_connection()
    await raw.driver_connection.copy_records_to_table(  # type: ignore[union-attr]
        "sample_stage",
        records=staged,
        columns=["line", "sample_id", "readset_id", "ncbi_accession", "biosample_id", "metadata", "collection"],
    )

    conflicts = [r.run for r in await conn.execute(_CONFLICTING_RUNS)]
    if conflicts:
        raise ReadsetConflict(conflicts)

    params = {"now": now}
    samples = (await conn.execute(_INSERT_SAMPLES, params)).all()
    collections = (await conn.execute(_INSERT_COLLECTIONS, params)).all()
    memberships = await conn.execute(_INSERT_MEMBERSHIPS, params)
    runs = await conn.execute(_INSERT_READSET_RUNS)
    inserted = sum(1 for s in samples if s.inserted)
    return RegistrationResult(
        received=len(rows),
        samples_inserted=inserted,
        samples_updated=len(samples) - inserted,
        collections_created=sum(1 for c in collections if c.inserted),
        memberships_added=memberships.rowcount,
        readset_runs_added=runs.rowcount,
    )


def collections_of(sample_id: ColumnElement[Any]) -> ColumnElement[Any]:
    members = (
        select(
            func.coalesce(
                func.array_agg(aggregate_order_by(collection_samples_tbl.c.collection_id, collection_samples_tbl.c.collection_id)),
                literal_column("ARRAY[]::varchar[]"),
            )
        )
        .where(collection_samples_tbl.c.sample_id == sample_id)
        .correlate_except(collection_samples_tbl)
        .scalar_subquery()
    )
    return members.label("collections")


def _sample_columns() -> list[ColumnElement[Any]]:
    return [*samples_tbl.c, collections_of(samples_tbl.c.sample_id)]


async def get_sample(conn: AsyncConnection, sample_or_readset_id: str) -> dict[str, Any] | None:
    column = samples_tbl.c.readset_id if sample_or_readset_id.startswith("RS.") else samples_tbl.c.sample_id
    row = (
        await conn.execute(select(*_sample_columns()).where(column == sample_or_readset_id).order_by(samples_tbl.c.id).limit(1))
    ).mappings().first()
    return dict(row) if row else None


async def get_sample_by_run(conn: AsyncConnection, run_accession: str) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            select(*_sample_columns())
            .select_from(samples_tbl.join(readset_runs_tbl, readset_runs_tbl.c.readset_id == samples_tbl.c.readset_id))
            .where(readset_runs_tbl.c.run_accession == run_accession)
            .order_by(samples_tbl.c.id)
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


async def samples_by_biosample(conn: AsyncConnection, biosample_id: str) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(*_sample_columns()).where(samples_tbl.c.biosample_id == biosample_id).order_by(samples_tbl.c.created_at.desc())
    )
    return [dict(r) for r in rows.mappings()]


async def list_samples(
    conn: AsyncConnection, *, limit: int, offset: int, search: str | None, collection: str | None
) -> tuple[list[dict[str, Any]], int]:
    source: Any = samples_tbl
    clauses = []
    if collection:
        source = samples_tbl.join(collection_samples_tbl, collection_samples_tbl.c.sample_id == samples_tbl.c.sample_id)
        clauses.append(collection_samples_tbl.c.collection_id == collection)
    if search:
        pattern = f"%{search}%"
        clauses.append(or_(samples_tbl.c.sample_id.ilike(pattern), samples_tbl.c.readset_id.ilike(pattern)))
    total = (await conn.execute(select(func.count()).select_from(source).where(*clauses))).scalar_one()
    rows = await conn.execute(
        select(*_sample_columns()).select_from(source).where(*clauses).order_by(samples_tbl.c.id).limit(limit).offset(offset)
    )
    return [dict(r) for r in rows.mappings()], total


async def collection_facets(conn: AsyncConnection) -> dict[str, Any]:
    total = (await conn.execute(select(func.count()).select_from(samples_tbl))).scalar_one()
    count = func.count().label("count")
    rows = await conn.execute(
        select(collection_samples_tbl.c.collection_id.label("collection"), count)
        .group_by(collection_samples_tbl.c.collection_id)
        .order_by(count.desc(), collection_samples_tbl.c.collection_id)
    )
    return {"total": total, "collections": [dict(r) for r in rows.mappings()]}


# ---------------------------------------------------------------- workflows


def _params_json(params: dict[str, Any] | None) -> str:
    return json.dumps(params or {}, sort_keys=True)


async def register_workflow(
    conn: AsyncConnection, req: RegisterWorkflowRequest, *, now: datetime
) -> tuple[dict[str, Any], bool]:
    """Upsert a workflow version. Returns ``(row, conflict)``; on a params clash nothing is written."""
    existing = (
        await conn.execute(
            select(workflows_tbl).where(
                workflows_tbl.c.workflow_id == req.workflow_id, workflows_tbl.c.version == req.version
            )
        )
    ).mappings().first()
    if existing is not None and _params_json(existing["params"]) != _params_json(req.params):
        return dict(existing), True

    values: dict[str, Any] = {
        "workflow_id": req.workflow_id,
        "version": req.version,
        "repository_url": req.repository_url,
        "revision": req.revision,
        "manifest_version": req.manifest_version,
        "max_retries": 3 if req.max_retries is None else req.max_retries,
        "description": req.description,
        "params": req.params or {},
        "status": "active",
        "sample_key": "readset_id",
        "created_at": now,
        "updated_at": now,
    }
    if "collections" in req.model_fields_set:
        values["collections"] = req.collections
    stmt = pg_insert(workflows_tbl).values(**values)
    updated = {
        k: stmt.excluded[k]
        for k in ("repository_url", "revision", "manifest_version", "max_retries", "description", "updated_at")
    }
    if "collections" in req.model_fields_set:
        updated["collections"] = stmt.excluded.collections
    row = (
        await conn.execute(
            stmt.on_conflict_do_update(index_elements=["workflow_id", "version"], set_=updated).returning(workflows_tbl)
        )
    ).mappings().one()
    return dict(row), False


async def list_workflows(conn: AsyncConnection, status: str | None) -> list[dict[str, Any]]:
    stmt = select(workflows_tbl).order_by(workflows_tbl.c.workflow_id, workflows_tbl.c.version)
    if status:
        stmt = stmt.where(workflows_tbl.c.status == status)
    return [dict(r) for r in (await conn.execute(stmt)).mappings()]


async def get_workflow(conn: AsyncConnection, pk: int) -> dict[str, Any] | None:
    row = (await conn.execute(select(workflows_tbl).where(workflows_tbl.c.id == pk))).mappings().first()
    return dict(row) if row else None


async def set_workflow_status(conn: AsyncConnection, pk: int, status: str, *, now: datetime) -> dict[str, Any] | None:
    """Pause, resume or retire. Retiring deletes the workflow's pending jobs (and their dead letters)."""
    row = (
        await conn.execute(
            update(workflows_tbl).where(workflows_tbl.c.id == pk).values(status=status, updated_at=now).returning(workflows_tbl)
        )
    ).mappings().first()
    if row is None:
        return None
    if status == "retired":
        pending = select(jobs_tbl.c.id).where(jobs_tbl.c.workflow_pk == pk, jobs_tbl.c.status == "pending")
        await conn.execute(delete(dead_letter_tbl).where(dead_letter_tbl.c.job_id.in_(pending)))
        await conn.execute(delete(jobs_tbl).where(jobs_tbl.c.workflow_pk == pk, jobs_tbl.c.status == "pending"))
    return dict(row)


async def set_workflow_revision(conn: AsyncConnection, pk: int, revision: str, *, now: datetime) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            update(workflows_tbl).where(workflows_tbl.c.id == pk).values(revision=revision, updated_at=now).returning(workflows_tbl)
        )
    ).mappings().first()
    return dict(row) if row else None


async def job_summary(conn: AsyncConnection, pk: int) -> dict[str, Any] | None:
    workflow = await get_workflow(conn, pk)
    if workflow is None:
        return None
    counted = await conn.execute(
        select(jobs_tbl.c.status, func.count()).where(jobs_tbl.c.workflow_pk == pk).group_by(jobs_tbl.c.status)
    )
    counts = {status: n for status, n in counted}
    dead = (
        await conn.execute(
            select(func.count())
            .select_from(dead_letter_tbl.join(jobs_tbl, jobs_tbl.c.id == dead_letter_tbl.c.job_id))
            .where(jobs_tbl.c.workflow_pk == pk)
        )
    ).scalar_one()
    total = sum(counts.values())
    summary: dict[str, Any] = {
        "workflow_pk": pk,
        "workflow_id": workflow["workflow_id"],
        "version": workflow["version"],
        "total": total,
    }
    for status in ("pending", "claimed", "submitted", "running", "completed", "failed"):
        summary[status] = counts.get(status, 0)
    summary["dead_letter"] = dead
    summary["completion_pct"] = round(100 * summary["completed"] / total, 2) if total else 0
    return summary


async def list_jobs(conn: AsyncConnection, pk: int, *, status: str | None, after: int, limit: int) -> list[dict[str, Any]]:
    sample_key = case((workflows_tbl.c.sample_key == "readset_id", samples_tbl.c.readset_id), else_=jobs_tbl.c.sample_id)
    query = (
        select(
            jobs_tbl.c.id.label("job_id"),
            sample_key.label("sample_key"),
            jobs_tbl.c.status,
            jobs_tbl.c.completed_at,
            samples_tbl.c.sample_id,
            samples_tbl.c.readset_id,
            samples_tbl.c.ncbi_accession,
            collections_of(jobs_tbl.c.sample_id),
        )
        .select_from(
            jobs_tbl.join(workflows_tbl, workflows_tbl.c.id == jobs_tbl.c.workflow_pk).outerjoin(
                samples_tbl, samples_tbl.c.sample_id == jobs_tbl.c.sample_id
            )
        )
        .where(jobs_tbl.c.workflow_pk == pk, jobs_tbl.c.id > after)
        .order_by(jobs_tbl.c.id)
        .limit(limit)
    )
    if status:
        query = query.where(jobs_tbl.c.status == status)
    return [dict(r) for r in (await conn.execute(query)).mappings()]
