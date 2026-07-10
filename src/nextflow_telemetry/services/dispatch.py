"""Dispatch orchestration — claiming, submitted-confirmation, requeue.

Pulled out of routers/dispatch.py so the router stays a thin HTTP/Pydantic
adapter (repo convention, see services/sample.py + routers/samples.py). The
STATE writes still go through services/lifecycle.py — this module owns the
orchestration around those calls (the pick-then-lock query, run_name
minting, sample-metadata fetch, TTL policy), not the transitions themselves.

HTTP/Pydantic-agnostic: returns plain dataclasses/bool/int, never an
HTTPException or a Pydantic model.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import jobs_tbl, samples_tbl, workflows_tbl
from . import lifecycle

if sys.version_info >= (3, 13):
    from uuid import uuid7 as _uuid7  # type: ignore[attr-defined]
else:
    from uuid_extensions import uuid7 as _uuid7

CLAIM_TTL_MINUTES = 5


@dataclass
class ClaimedJob:
    """A single job included in a claimed batch."""
    sample_id: str
    ncbi_accession: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimedBatch:
    """Result of DispatchService.claim_batch — everything the caller needs
    to build a `nextflow run` command and report it back on the wire."""
    run_name: str
    workflow_id: str
    workflow_version: str
    workflow_pk: int
    repository_url: str
    revision: str
    jobs: list[ClaimedJob]


@dataclass
class DispatchService:
    engine: AsyncEngine

    async def claim_batch(
        self,
        limit: int,
        workflow_id: list[str] | None = None,
        workflow_version: str | None = None,
    ) -> ClaimedBatch | None:
        """Atomically claim a batch of pending jobs for one workflow version.

        Returns None when there are no pending jobs matching the filter
        (the caller maps that to HTTP 204).
        """
        now = datetime.now(timezone.utc)

        async with self.engine.begin() as conn:
            # Two-step pick-then-lock: first decide *which* (workflow_id,
            # workflow_version) batch to claim, then take a row-level lock
            # only on jobs in that batch. Issue #74: a single FOR UPDATE
            # SKIP LOCKED LIMIT N could lock rows across multiple workflows
            # that we'd then narrow away in Python — those locks blocked
            # other dispatchers needlessly.
            pick_q = (
                select(jobs_tbl.c.workflow_id, jobs_tbl.c.workflow_version)
                .join(workflows_tbl, jobs_tbl.c.workflow_pk == workflows_tbl.c.id)
                .where(
                    jobs_tbl.c.status == "pending",
                    workflows_tbl.c.status == "active",
                )
            )
            if workflow_id:
                pick_q = pick_q.where(jobs_tbl.c.workflow_id.in_(workflow_id))
            if workflow_version:
                pick_q = pick_q.where(jobs_tbl.c.workflow_version == workflow_version)
            pick_q = (
                pick_q.order_by(
                    jobs_tbl.c.workflow_id,
                    jobs_tbl.c.workflow_version,
                    jobs_tbl.c.created_at,
                )
                .limit(1)
                # Skip rows another dispatcher is already claiming. Without
                # this, a competing transaction holding a lock on the
                # otherwise-first pending job would cause every subsequent
                # caller to pick that workflow, fail step 2 (FOR UPDATE
                # SKIP LOCKED returns 0 rows because all of that workflow's
                # rows are locked too), and 204 → spin. SKIP LOCKED on
                # pick steers us to a workflow that *has* claimable rows.
                .with_for_update(of=jobs_tbl, skip_locked=True)
            )

            pick_row = (await conn.execute(pick_q)).mappings().first()
            if pick_row is None:
                return None
            first_wf_id = pick_row["workflow_id"]
            first_wf_ver = pick_row["workflow_version"]

            # Step 2: lock and claim jobs in that single batch only.
            # If a competing dispatcher swept through between step 1 and
            # step 2, this returns nothing and the caller retries — the
            # next pick may resolve to a different workflow.
            claim_q = (
                select(jobs_tbl, workflows_tbl)
                .join(workflows_tbl, jobs_tbl.c.workflow_pk == workflows_tbl.c.id)
                .where(
                    jobs_tbl.c.status == "pending",
                    workflows_tbl.c.status == "active",
                    jobs_tbl.c.workflow_id == first_wf_id,
                    jobs_tbl.c.workflow_version == first_wf_ver,
                )
                .order_by(jobs_tbl.c.created_at)
                .limit(limit)
                .with_for_update(of=jobs_tbl, skip_locked=True)
            )
            rows = (await conn.execute(claim_q)).mappings().all()

            if not rows:
                return None

            job_ids = [r["id"] for r in rows]
            run_name = "r" + str(_uuid7())
            workflow_pk = rows[0]["workflow_pk"]
            repository_url = rows[0]["repository_url"]
            revision = rows[0]["revision"]

            await lifecycle.claim(
                conn,
                job_ids,
                run_name,
                {
                    "workflow_id": first_wf_id,
                    "workflow_version": first_wf_ver,
                    "workflow_pk": workflow_pk,
                    "revision": revision,
                    "claimed_at": now,
                },
            )

            # Fetch sample fields in a separate query to avoid JOIN conflicts
            # with the FOR UPDATE SKIP LOCKED above.
            sample_ids = [r["sample_id"] for r in rows]
            meta_result = await conn.execute(
                select(
                    samples_tbl.c.sample_id,
                    samples_tbl.c.ncbi_accession,
                    samples_tbl.c.metadata_,
                )
                .where(samples_tbl.c.sample_id.in_(sample_ids))
            )
            sample_map: dict[str, Any] = {
                r["sample_id"]: r
                for r in meta_result.mappings().all()
            }

        return ClaimedBatch(
            run_name=run_name,
            workflow_id=first_wf_id,
            workflow_version=first_wf_ver,
            workflow_pk=workflow_pk,
            repository_url=repository_url,
            revision=revision,
            jobs=[
                ClaimedJob(
                    sample_id=r["sample_id"],
                    ncbi_accession=sample_map.get(r["sample_id"], {}).get("ncbi_accession"),
                    metadata=sample_map.get(r["sample_id"], {}).get("metadata_") or {},
                )
                for r in rows
            ],
        )

    async def report_submitted(self, run_name: str, executor_job_id: str | None) -> bool:
        """Confirm a run has been submitted to the executor.

        Returns False if the run isn't found or isn't in `claimed` state (the
        caller maps that to HTTP 404).
        """
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            return await lifecycle.mark_submitted(conn, run_name, executor_job_id, now)

    async def requeue_expired(self) -> int:
        """Expire stale `claimed` runs (older than CLAIM_TTL_MINUTES) and
        reset their jobs back to `pending`. Returns the count of runs expired."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=CLAIM_TTL_MINUTES)
        async with self.engine.begin() as conn:
            return await lifecycle.requeue_expired(conn, cutoff)
