"""Structured JSON logging for the telemetry server.

Every log record emits a single line of JSON to stdout. Caller-supplied
context via ``logger.info(..., extra={...})`` becomes top-level fields,
so callers don't have to string-interpolate structured data into the
message. Downstream consumers (journald + loki, jq pipelines, audit
queries) can parse the output without log-line regex archaeology.

Env vars:
    LOG_LEVEL — root logger level, default "INFO".
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


# stdlib LogRecord attributes that we don't want to surface as message
# fields. Anything passed via ``extra={...}`` lands on the record but
# isn't in this set, so it gets emitted as a top-level JSON key.
_RESERVED_RECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "taskName",
})


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single line of JSON.

    Top-level fields are ``ts``, ``level``, ``logger``, ``msg``. Any
    extras passed by the caller appear alongside.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | int = "INFO") -> None:
    """Replace the root logger's handlers with one stdout JSON handler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(level)


# Configure at import time so any code that just does ``from .log import
# logger`` gets the right shape. Override via LOG_LEVEL in deployment.
configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())


logger = logging.getLogger("nextflow_telemetry")
