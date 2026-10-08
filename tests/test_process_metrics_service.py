"""Unit + service-level tests for ProcessMetricsService default-window behaviour.

The default-window default (7 days, applied when no time filter is supplied)
prevents unparameterised metrics calls from scanning the full telemetry table
once event volume grows. These tests lock in the behaviour so a regression
back to "all-time" is caught at PR time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nextflow_telemetry.services.event_archive import EventArchive
from nextflow_telemetry.services.process_metrics import (
    _DEFAULT_WINDOW_DAYS,
    _normalize_window,
    ProcessMetricsService,
)


# ---------------------------------------------------------------------------
# Pure unit tests for _normalize_window
# ---------------------------------------------------------------------------

def test_normalize_window_applies_default_when_all_time_filters_none():
    out = _normalize_window(window_days=None, window_hours=None, since=None, until=None)
    assert out == (_DEFAULT_WINDOW_DAYS, None)


def test_normalize_window_passes_through_explicit_window_days():
    out = _normalize_window(window_days=30, window_hours=None, since=None, until=None)
    assert out == (30, None)


def test_normalize_window_passes_through_explicit_window_hours():
    out = _normalize_window(window_days=None, window_hours=12, since=None, until=None)
    assert out == (None, 12)


def test_normalize_window_passes_through_when_since_supplied():
    """Any time filter present should suppress the default."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = _normalize_window(window_days=None, window_hours=None, since=since, until=None)
    assert out == (None, None)


def test_normalize_window_passes_through_when_until_supplied():
    until = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = _normalize_window(window_days=None, window_hours=None, since=None, until=until)
    assert out == (None, None)


# ---------------------------------------------------------------------------
# Service-level: bare call returns window_days=7 in payload
# ---------------------------------------------------------------------------

def _empty_service(tmp_path) -> ProcessMetricsService:
    archive = EventArchive(source=str(tmp_path), v2_api_url="http://v2.invalid", refresh_seconds=300)
    return ProcessMetricsService(engine=None, archive=archive)  # type: ignore[arg-type]


async def test_bare_summary_call_reports_default_window_in_response(tmp_path):
    """Exercising one representative method end-to-end with no time args.

    The empty archive has no events so the SQL just returns zeros, but
    we don't care about the row counts — we care that the response payload
    echoes back `window_days = _DEFAULT_WINDOW_DAYS`. That's what tells a
    client whether all-time data was scanned or just the default window.
    """
    result = await _empty_service(tmp_path).summary()
    assert result["window_days"] == _DEFAULT_WINDOW_DAYS


async def test_explicit_window_days_overrides_default(tmp_path):
    result = await _empty_service(tmp_path).summary(window_days=30)
    assert result["window_days"] == 30
