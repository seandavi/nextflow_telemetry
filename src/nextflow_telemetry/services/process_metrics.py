from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class ProcessMetricsService:
    engine: AsyncEngine

    def _filter_clause(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        table_alias: str = "t",
    ) -> tuple[str, dict[str, Any]]:
        """Build a composable WHERE fragment and bind-params dict for telemetry queries."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        a = table_alias

        if window_days is not None and window_hours is not None:
            raise ValueError("Provide only one of window_days or window_hours, not both.")

        if window_days is not None:
            if window_days < 1:
                raise ValueError("window_days must be >= 1")
            clauses.append(f"{a}.utc_time >= now() - make_interval(days => :window_days)")
            params["window_days"] = window_days

        if window_hours is not None:
            if window_hours < 1:
                raise ValueError("window_hours must be >= 1")
            clauses.append(f"{a}.utc_time >= now() - make_interval(hours => :window_hours)")
            params["window_hours"] = window_hours

        if since is not None:
            clauses.append(f"{a}.utc_time >= :since")
            params["since"] = since

        if until is not None:
            clauses.append(f"{a}.utc_time <= :until")
            params["until"] = until

        if workflow_id is not None:
            clauses.append(f"{a}.workflow_id = :workflow_id")
            params["workflow_id"] = workflow_id

        if workflow_version is not None:
            clauses.append(f"{a}.workflow_version = :workflow_version")
            params["workflow_version"] = workflow_version

        if run_name is not None:
            clauses.append(f"{a}.run_name = :run_name")
            params["run_name"] = run_name

        if sample_id is not None:
            clauses.append(f"{a}.sample_id = :sample_id")
            params["sample_id"] = sample_id

        fragment = (" and " + " and ".join(clauses)) if clauses else ""
        return fragment, params

    async def summary(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        min_samples: int = 5,
        limit: int = 10,
    ) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        fc2, _ = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
            table_alias="t2",
        )
        params = {**params, "min_samples": min_samples, "limit": limit}

        cards_sql = text(
            f"""
            with x as (
              select
                t.run_id,
                t.utc_time,
                t.trace->>'process' as process,
                coalesce(t.trace->>'status','') as status,
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt,
                nullif(t.trace->>'peak_rss','')::double precision as peak_rss,
                nullif(t.trace->>'memory','')::double precision as requested_memory_bytes
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            )
            select
              count(*) as process_completed_rows,
              count(distinct run_id) as distinct_runs,
              (select count(distinct t2.trace->>'process')
               from telemetry t2
               where t2.event in ('process_submitted','process_started','process_completed')
                 and t2.trace is not null
                 and t2.trace->>'process' is not null
                 and t2.trace->>'process' not like '%MARK_COMPLETE'
                 and t2.trace->>'process' not like '%FINISHED'
                 {fc2}) as distinct_processes,
              count(*) filter (where status = 'COMPLETED') as success_rows,
              count(*) filter (where status in ('FAILED', 'ABORTED')) as failure_rows,
              coalesce(round(100.0 * count(*) filter (where status in ('FAILED', 'ABORTED'))::numeric / nullif(count(*), 0), 2), 0) as failure_pct,
              count(*) filter (where attempt > 1) as retried_rows,
              coalesce(round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2), 0) as retry_pct,
              coalesce(round(100.0 * count(*) filter (where attempt > 1 and status = 'COMPLETED')::numeric /
                    nullif(count(*) filter (where attempt > 1), 0), 2), 0) as retry_success_pct,
              coalesce(round(100.0 * avg(peak_rss / nullif(requested_memory_bytes, 0))
                    filter (where peak_rss is not null and requested_memory_bytes is not null and requested_memory_bytes > 0)::numeric, 2), 0)
                as memory_efficiency_pct,
              max(utc_time) as latest_process_completed_utc
            from x
            """
        )

        top_failures_sql = text(
            f"""
            with x as (
              select
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(t.trace->>'status','') as status
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            )
            select
              process,
              count(*) as total_completed,
              count(*) filter (where status in ('FAILED', 'ABORTED')) as failed,
              round(100.0 * count(*) filter (where status in ('FAILED', 'ABORTED'))::numeric / nullif(count(*), 0), 2) as failure_pct
            from x
            group by process
            having count(*) >= :min_samples
            order by failed desc, failure_pct desc, total_completed desc
            limit :limit
            """
        )

        top_retries_sql = text(
            f"""
            with x as (
              select
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(t.trace->>'status','') as status,
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            )
            select
              process,
              count(*) as total_completed,
              count(*) filter (where attempt > 1) as retried,
              round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2) as retried_pct,
              count(*) filter (where attempt > 1 and status = 'COMPLETED') as retried_success,
              count(*) filter (where attempt > 1 and status in ('FAILED', 'ABORTED')) as retried_failed
            from x
            group by process
            having count(*) >= :min_samples
            order by retried desc, retried_pct desc, total_completed desc
            limit :limit
            """
        )

        top_exit_codes_sql = text(
            f"""
            select
              coalesce(t.trace->>'exit','<null>') as exit_code,
              count(*) as failures
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              and coalesce(t.trace->>'status','') in ('FAILED', 'ABORTED')
              {fc}
            group by exit_code
            order by failures desc, exit_code
            limit :limit
            """
        )

        event_mix_sql = text(
            f"""
            select t.event, count(*) as rows
            from telemetry t
            where true
              {fc}
            group by t.event
            order by rows desc, t.event
            """
        )

        async with self.engine.connect() as conn:
            cards = dict((await conn.execute(cards_sql, params)).mappings().one())
            top_failures = [dict(row) for row in (await conn.execute(top_failures_sql, params)).mappings().all()]
            top_retries = [dict(row) for row in (await conn.execute(top_retries_sql, params)).mappings().all()]
            top_exit_codes = [dict(row) for row in (await conn.execute(top_exit_codes_sql, params)).mappings().all()]
            event_mix = [dict(row) for row in (await conn.execute(event_mix_sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "cards": cards,
            "event_mix": event_mix,
            "top_failures": top_failures,
            "top_retries": top_retries,
            "top_failure_exit_codes": top_exit_codes,
        }

    async def retries(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        min_samples: int = 5,
        limit: int = 50,
    ) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        params = {**params, "min_samples": min_samples, "limit": limit}

        summary_sql = text(
            f"""
            with x as (
              select
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt,
                coalesce(t.trace->>'status','') as status
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            )
            select
              count(*) as process_completed_rows,
              count(*) filter (where attempt > 1) as retried_rows,
              coalesce(round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2), 0) as retried_pct,
              count(*) filter (where attempt > 1 and status = 'COMPLETED') as retry_success_rows,
              count(*) filter (where attempt > 1 and status in ('FAILED', 'ABORTED')) as retry_failure_rows,
              coalesce(round(100.0 * count(*) filter (where attempt > 1 and status = 'COMPLETED')::numeric /
                    nullif(count(*) filter (where attempt > 1), 0), 2), 0) as retry_success_pct
            from x
            """
        )

        by_process_sql = text(
            f"""
            with x as (
              select
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt,
                coalesce(t.trace->>'status','') as status
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            )
            select
              process,
              count(*) as total_completed,
              count(*) filter (where attempt > 1) as retried,
              round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2) as retried_pct,
              count(*) filter (where attempt > 1 and status = 'COMPLETED') as retried_success,
              count(*) filter (where attempt > 1 and status in ('FAILED', 'ABORTED')) as retried_failed,
              max(attempt) as max_attempt
            from x
            group by process
            having count(*) >= :min_samples
            order by retried desc, retried_pct desc, total_completed desc
            limit :limit
            """
        )

        by_attempt_sql = text(
            f"""
            select
              coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt,
              count(*) as rows,
              count(*) filter (where coalesce(t.trace->>'status','') = 'COMPLETED') as success,
              count(*) filter (where coalesce(t.trace->>'status','') in ('FAILED', 'ABORTED')) as failed
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              {fc}
            group by attempt
            order by attempt
            """
        )

        async with self.engine.connect() as conn:
            summary = dict((await conn.execute(summary_sql, params)).mappings().one())
            by_process = [dict(row) for row in (await conn.execute(by_process_sql, params)).mappings().all()]
            by_attempt = [dict(row) for row in (await conn.execute(by_attempt_sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "summary": summary,
            "by_attempt": by_attempt,
            "by_process": by_process,
        }

    async def resources_by_attempt(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        min_samples: int = 5,
        limit: int = 100,
    ) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        params = {**params, "min_samples": min_samples, "limit": limit}

        sql = text(
            f"""
            with x as (
              select
                t.trace->>'process' as process,
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt,
                coalesce(t.trace->>'status','') as status,
                nullif(t.trace->>'cpus','')::double precision as requested_cpus,
                nullif(t.trace->>'memory','')::double precision as requested_memory_bytes,
                nullif(t.trace->>'time','')::double precision as requested_time_ms,
                nullif(t.trace->>'%cpu','')::double precision as pct_cpu,
                nullif(t.trace->>'%mem','')::double precision as pct_mem,
                nullif(t.trace->>'peak_rss','')::double precision as peak_rss,
                nullif(t.trace->>'read_bytes','')::double precision as read_bytes,
                nullif(t.trace->>'write_bytes','')::double precision as write_bytes
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                and t.trace->>'process' is not null
                {fc}
            )
            select
              process,
              attempt,
              count(*) as rows,
              count(*) filter (where status = 'COMPLETED') as success,
              count(*) filter (where status in ('FAILED', 'ABORTED')) as failed,
              round(avg(requested_cpus)::numeric, 2) as avg_requested_cpus,
              round((avg(requested_memory_bytes) / (1024*1024*1024))::numeric, 2) as avg_requested_memory_gb,
              round((avg(requested_time_ms) / (1000*60))::numeric, 2) as avg_requested_time_min,
              round(avg(pct_cpu)::numeric, 2) as avg_pct_cpu,
              round(percentile_cont(0.95) within group (order by pct_cpu)::numeric, 2) as p95_pct_cpu,
              round(avg(pct_cpu / nullif(requested_cpus * 100, 0)) * 100::numeric, 2) as avg_cpu_efficiency_pct,
              round(avg(pct_mem)::numeric, 2) as avg_pct_mem,
              round(percentile_cont(0.95) within group (order by pct_mem)::numeric, 2) as p95_pct_mem,
              round(avg(peak_rss / nullif(requested_memory_bytes, 0)) * 100::numeric, 2) as avg_memory_efficiency_pct,
              round((avg(peak_rss) / (1024*1024*1024))::numeric, 2) as avg_peak_rss_gb,
              round((percentile_cont(0.95) within group (order by peak_rss) / (1024*1024*1024))::numeric, 2) as p95_peak_rss_gb,
              round((avg(read_bytes) / (1024*1024*1024))::numeric, 2) as avg_read_gb,
              round((avg(write_bytes) / (1024*1024*1024))::numeric, 2) as avg_write_gb
            from x
            group by process, attempt
            having count(*) >= :min_samples
            order by rows desc, process, attempt
            limit :limit
            """
        )

        async with self.engine.connect() as conn:
            rows = [dict(row) for row in (await conn.execute(sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }

    async def failures(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        min_samples: int = 5,
        limit: int = 50,
    ) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        params = {**params, "min_samples": min_samples, "limit": limit}

        sql = text(
            f"""
            with x as (
              select
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(t.trace->>'status','') as status,
                coalesce(t.trace->>'exit','<null>') as exit_code,
                nullif(t.trace->>'error_action', '') as error_action
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {fc}
            ),
            grouped as (
              select
                process,
                count(*) as total_completed,
                count(*) filter (where status = 'COMPLETED') as success,
                count(*) filter (where status in ('FAILED', 'ABORTED')) as failed
              from x
              group by process
              having count(*) >= :min_samples
            ),
            fail_exit as (
              select
                process,
                exit_code,
                row_number() over (
                  partition by process
                  order by count(*) desc, exit_code
                ) as rn
              from x
              where status in ('FAILED', 'ABORTED')
              group by process, exit_code
            ),
            fail_action as (
              select
                process,
                error_action,
                row_number() over (
                  partition by process
                  order by count(*) desc, error_action
                ) as rn
              from x
              where status in ('FAILED', 'ABORTED') and error_action is not null
              group by process, error_action
            )
            select
              g.process,
              g.total_completed,
              g.success,
              g.failed,
              round(100.0 * g.failed::numeric / nullif(g.total_completed, 0), 2) as failure_pct,
              f.exit_code as modal_failure_exit_code,
              a.error_action as modal_error_action
            from grouped g
            left join fail_exit f on f.process = g.process and f.rn = 1
            left join fail_action a on a.process = g.process and a.rn = 1
            order by g.failed desc, failure_pct desc, g.total_completed desc
            limit :limit
            """
        )

        async with self.engine.connect() as conn:
            rows = [dict(row) for row in (await conn.execute(sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }

    async def failure_signatures(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        params = {**params, "limit": limit}

        sql = text(
            f"""
            select
              coalesce(t.trace->>'process','<null>') as process,
              coalesce(t.trace->>'exit','<null>') as exit_code,
              nullif(t.trace->>'error_action', '') as error_action,
              count(*) as failures
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              and coalesce(t.trace->>'status','') in ('FAILED', 'ABORTED')
              {fc}
            group by process, exit_code, error_action
            order by failures desc, process, exit_code
            limit :limit
            """
        )

        async with self.engine.connect() as conn:
            rows = [dict(row) for row in (await conn.execute(sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }

    async def timeline(
        self,
        *,
        bucket: Literal["hour", "day", "week"] = "hour",
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        process: str | None = None,
    ) -> dict[str, Any]:
        if bucket not in ("hour", "day", "week"):
            raise ValueError("bucket must be 'hour', 'day', or 'week'")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
        )
        params = {**params, "bucket": bucket}

        process_clause = ""
        if process is not None:
            process_clause = "and t.trace->>'process' = :process"
            params["process"] = process

        sql = text(
            f"""
            select
              date_trunc(:bucket, t.utc_time) as bucket_start,
              count(*) as total,
              count(*) filter (where coalesce(t.trace->>'status','') = 'COMPLETED') as success,
              count(*) filter (where coalesce(t.trace->>'status','') in ('FAILED','ABORTED')) as failed,
              coalesce(round(
                100.0 * count(*) filter (where coalesce(t.trace->>'status','') in ('FAILED','ABORTED'))::numeric
                / nullif(count(*), 0), 2
              ), 0) as failure_pct
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              {fc}
              {process_clause}
            group by bucket_start
            order by bucket_start
            """
        )

        async with self.engine.connect() as conn:
            rows = [dict(row) for row in (await conn.execute(sql, params)).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "bucket": bucket,
            "rows": rows,
        }

    async def running(self) -> dict[str, Any]:
        """Tasks currently in flight across all active Nextflow runs."""
        sql = text(
            """
            with active_runs as (
                select run_name from workflow_runs where status = 'running'
            ),
            events as (
                select
                    t.run_name,
                    t.trace->>'task_id' as task_id,
                    coalesce(t.trace->>'process', '<null>') as process,
                    t.event
                from telemetry t
                join active_runs ar on t.run_name = ar.run_name
                where t.event in ('process_submitted', 'process_started', 'process_completed')
                  and t.trace is not null
            ),
            task_state as (
                select
                    process,
                    bool_or(event = 'process_submitted')  as is_submitted,
                    bool_or(event = 'process_started')    as is_started,
                    bool_or(event = 'process_completed')  as is_completed
                from events
                group by run_name, task_id, process
            )
            select
                process,
                count(*) filter (where is_started and not is_completed)          as running,
                count(*) filter (where is_submitted and not is_started)           as queued
            from task_state
            group by process
            having count(*) filter (where is_started and not is_completed) > 0
                or count(*) filter (where is_submitted and not is_started) > 0
            order by running desc, queued desc
            """
        )

        active_runs_sql = text(
            "select count(*) as n from workflow_runs where status = 'running'"
        )

        async with self.engine.connect() as conn:
            rows = [dict(r) for r in (await conn.execute(sql)).mappings().all()]
            active_nf_runs = (await conn.execute(active_runs_sql)).scalar_one()

        total_running = sum(r["running"] for r in rows)
        total_queued  = sum(r["queued"]  for r in rows)

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "active_nf_runs": active_nf_runs,
            "total_running": total_running,
            "total_queued": total_queued,
            "by_process": rows,
        }

    async def tasks(
        self,
        *,
        window_days: int | None = None,
        window_hours: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        run_name: str | None = None,
        sample_id: str | None = None,
        process: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Individual process_completed rows for the task browser."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        fc, params = self._filter_clause(
            window_days=window_days, window_hours=window_hours,
            since=since, until=until,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_name=run_name, sample_id=sample_id,
        )
        params = {**params, "limit": limit, "offset": offset}

        extra_clauses = ""
        if process is not None:
            extra_clauses += " and t.trace->>'process' = :process"
            params["process"] = process
        if status is not None:
            extra_clauses += " and coalesce(t.trace->>'status','') = :status"
            params["status"] = status

        sql = text(
            f"""
            select
              t.id                                                              as telemetry_id,
              t.run_name,
              t.run_id,
              t.sample_id,
              t.workflow_id,
              t.workflow_version,
              t.utc_time,
              coalesce(t.trace->>'process','<null>')                           as process,
              t.trace->>'name'                                                 as name,
              coalesce(t.trace->>'status','')                                  as status,
              coalesce(nullif(t.trace->>'attempt',''),'1')::int                as attempt,
              nullif(t.trace->>'hash','')                                      as task_hash,
              nullif(t.trace->>'exit','')                                      as exit_code,
              nullif(t.trace->>'error_action','')                              as error_action,
              nullif(t.trace->>'realtime','')::double precision                as realtime_ms,
              nullif(t.trace->>'cpus','')::double precision                    as requested_cpus,
              nullif(t.trace->>'memory','')::double precision / 1073741824.0  as requested_memory_gb,
              nullif(t.trace->>'%cpu','')::double precision                    as pct_cpu,
              nullif(t.trace->>'%mem','')::double precision                    as pct_mem,
              nullif(t.trace->>'peak_rss','')::double precision / 1073741824.0 as peak_rss_gb,
              nullif(t.trace->>'rchar','')::double precision / 1073741824.0    as read_gb,
              nullif(t.trace->>'wchar','')::double precision / 1073741824.0    as write_gb,
              count(*) over ()                                                  as total_count
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              {fc}
              {extra_clauses}
            order by t.utc_time desc
            limit :limit offset :offset
            """
        )

        async with self.engine.connect() as conn:
            result = (await conn.execute(sql, params)).mappings().all()

        total = result[0]["total_count"] if result else 0
        rows = []
        for row in result:
            d = dict(row)
            d.pop("total_count", None)
            rows.append(d)

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": rows,
        }
