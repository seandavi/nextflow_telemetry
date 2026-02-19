from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass
class ProcessMetricsService:
    engine: Engine

    @staticmethod
    def _normalize_window_days(window_days: int | None) -> int | None:
        if window_days is None:
            return None
        if window_days < 1:
            raise ValueError("window_days must be >= 1")
        return window_days

    def _window_clause(self, window_days: int | None) -> tuple[str, dict[str, Any]]:
        normalized = self._normalize_window_days(window_days)
        if normalized is None:
            return "", {}
        return " and t.utc_time >= now() - make_interval(days => :window_days)", {"window_days": normalized}

    def summary(self, *, window_days: int | None = None, min_samples: int = 50, limit: int = 10) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        window_clause, params = self._window_clause(window_days)
        params = {**params, "min_samples": min_samples, "limit": limit}

        cards_sql = text(
            f"""
            with x as (
              select
                t.run_id,
                t.utc_time,
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(t.trace->>'status','') as status,
                coalesce(nullif(t.trace->>'attempt',''),'0')::int as attempt
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {window_clause}
            )
            select
              count(*) as process_completed_rows,
              count(distinct run_id) as distinct_runs,
              count(distinct process) as distinct_processes,
              count(*) filter (where status = 'COMPLETED') as success_rows,
              count(*) filter (where status in ('FAILED', 'ABORTED')) as failure_rows,
              round(100.0 * count(*) filter (where status in ('FAILED', 'ABORTED'))::numeric / nullif(count(*), 0), 2) as failure_pct,
              count(*) filter (where attempt > 1) as retried_rows,
              round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2) as retry_pct,
              round(100.0 * count(*) filter (where attempt > 1 and status = 'COMPLETED')::numeric /
                    nullif(count(*) filter (where attempt > 1), 0), 2) as retry_success_pct,
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
                {window_clause}
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
                {window_clause}
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
              {window_clause}
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
              {window_clause}
            group by t.event
            order by rows desc, t.event
            """
        )

        with self.engine.connect() as conn:
            cards = dict(conn.execute(cards_sql, params).mappings().one())
            top_failures = [dict(row) for row in conn.execute(top_failures_sql, params).mappings().all()]
            top_retries = [dict(row) for row in conn.execute(top_retries_sql, params).mappings().all()]
            top_exit_codes = [dict(row) for row in conn.execute(top_exit_codes_sql, params).mappings().all()]
            event_mix = [dict(row) for row in conn.execute(event_mix_sql, params).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "cards": cards,
            "event_mix": event_mix,
            "top_failures": top_failures,
            "top_retries": top_retries,
            "top_failure_exit_codes": top_exit_codes,
        }

    def retries(self, *, window_days: int | None = None, min_samples: int = 50, limit: int = 50) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        window_clause, params = self._window_clause(window_days)
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
                {window_clause}
            )
            select
              count(*) as process_completed_rows,
              count(*) filter (where attempt > 1) as retried_rows,
              round(100.0 * count(*) filter (where attempt > 1)::numeric / nullif(count(*), 0), 2) as retried_pct,
              count(*) filter (where attempt > 1 and status = 'COMPLETED') as retry_success_rows,
              count(*) filter (where attempt > 1 and status in ('FAILED', 'ABORTED')) as retry_failure_rows,
              round(100.0 * count(*) filter (where attempt > 1 and status = 'COMPLETED')::numeric /
                    nullif(count(*) filter (where attempt > 1), 0), 2) as retry_success_pct
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
                {window_clause}
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
              {window_clause}
            group by attempt
            order by attempt
            """
        )

        with self.engine.connect() as conn:
            summary = dict(conn.execute(summary_sql, params).mappings().one())
            by_process = [dict(row) for row in conn.execute(by_process_sql, params).mappings().all()]
            by_attempt = [dict(row) for row in conn.execute(by_attempt_sql, params).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "summary": summary,
            "by_attempt": by_attempt,
            "by_process": by_process,
        }

    def resources_by_attempt(
        self,
        *,
        window_days: int | None = None,
        min_samples: int = 50,
        limit: int = 100,
    ) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        window_clause, params = self._window_clause(window_days)
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
                {window_clause}
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
              round(avg(pct_mem)::numeric, 2) as avg_pct_mem,
              round(percentile_cont(0.95) within group (order by pct_mem)::numeric, 2) as p95_pct_mem,
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

        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }

    def failures(self, *, window_days: int | None = None, min_samples: int = 50, limit: int = 50) -> dict[str, Any]:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        window_clause, params = self._window_clause(window_days)
        params = {**params, "min_samples": min_samples, "limit": limit}

        sql = text(
            f"""
            with x as (
              select
                coalesce(t.trace->>'process','<null>') as process,
                coalesce(t.trace->>'status','') as status,
                coalesce(t.trace->>'exit','<null>') as exit_code
              from telemetry t
              where t.event = 'process_completed'
                and t.trace is not null
                {window_clause}
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
            )
            select
              g.process,
              g.total_completed,
              g.success,
              g.failed,
              round(100.0 * g.failed::numeric / nullif(g.total_completed, 0), 2) as failure_pct,
              f.exit_code as modal_failure_exit_code
            from grouped g
            left join fail_exit f
              on f.process = g.process and f.rn = 1
            order by g.failed desc, failure_pct desc, g.total_completed desc
            limit :limit
            """
        )

        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }

    def failure_signatures(self, *, window_days: int | None = None, limit: int = 100) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        window_clause, params = self._window_clause(window_days)
        params = {**params, "limit": limit}

        sql = text(
            f"""
            select
              coalesce(t.trace->>'process','<null>') as process,
              coalesce(t.trace->>'exit','<null>') as exit_code,
              count(*) as failures
            from telemetry t
            where t.event = 'process_completed'
              and t.trace is not null
              and coalesce(t.trace->>'status','') in ('FAILED', 'ABORTED')
              {window_clause}
            group by process, exit_code
            order by failures desc, process, exit_code
            limit :limit
            """
        )

        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).mappings().all()]

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "rows": rows,
        }
