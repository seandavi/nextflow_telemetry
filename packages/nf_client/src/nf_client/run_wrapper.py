"""Instrumentation wrapper around `nextflow run`.

Runs inside a SLURM job (or any executor) and emits run-lifecycle events
to the telemetry server before, during, and after the nextflow subprocess.
Implements Phase 3 of issue #62 / sub-issue #67.

Event sequence per run:

    wrapper_started   — wrapper began execution on a compute node
    pre_nextflow      — about to exec nextflow (queue wait recorded)
    heartbeat         — every N seconds while nextflow is alive
    wrapper_exited    — nextflow returned (any exit code); .nextflow.log attached

Telemetry POSTs are best-effort; any failure is logged to stderr and
swallowed. The wrapper must never fail a run because of instrumentation.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


_HEARTBEAT_SECONDS_DEFAULT = 60
_LOG_UPLOAD_TIMEOUT = 60
_EVENT_POST_TIMEOUT = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post_event(
    client: httpx.Client | None,
    run_name: str,
    body: dict,
    files: dict | None = None,
    *,
    timeout: int = _EVENT_POST_TIMEOUT,
) -> None:
    """POST a run-lifecycle event. Errors are logged and swallowed.

    Logs both transport-level failures (network down, timeout) and HTTP-level
    failures (4xx/5xx). httpx does not raise on non-2xx responses, so a
    server-side rejection (e.g. the orphan-log 404 from PR #64) would otherwise
    look like a successful no-op in the wrapper logs.

    Accepts ``client=None`` (no-op telemetry mode) for the case where the
    httpx client could not be constructed — see main(); the run still runs.

    The path is *relative* (``runs/{run_name}/event``) so it composes
    cleanly with whatever path prefix the operator chose for ``base_url``.
    Mirrors the JobClient convention: ``base_url`` ends with ``/`` and is
    treated as the API root.
    """
    if client is None:
        return
    event_type = body.get("type", "<unknown>")
    try:
        response = client.post(
            f"runs/{run_name}/event",
            data={"event": json.dumps(body)},
            files=files,
            timeout=timeout,
        )
    except Exception as e:
        print(
            f"[run_wrapper] telemetry POST {event_type} failed: {e}",
            file=sys.stderr,
            flush=True,
        )
        return
    if not response.is_success:
        # Truncate body so a stray HTML error page doesn't flood the log.
        body_preview = response.text[:500].replace("\n", " ")
        print(
            f"[run_wrapper] telemetry POST {event_type} returned "
            f"{response.status_code}: {body_preview}",
            file=sys.stderr,
            flush=True,
        )


def _wait_seconds_from_slurm() -> int | None:
    """Compute submit→start queue wait from SLURM env, if available.

    Both SLURM_SUBMIT_TIME and SLURM_JOB_START_TIME are unix timestamps
    (string-encoded integers). Returns None when either is absent,
    unparseable, or when the computed delta is negative (which would
    indicate inconsistent env vars and is never a meaningful queue wait).
    The caller still includes the key in the event payload (as null) so
    the schema is uniform across SLURM and non-SLURM runs.
    """
    submit = os.environ.get("SLURM_SUBMIT_TIME")
    start = os.environ.get("SLURM_JOB_START_TIME")
    if not submit or not start:
        return None
    try:
        delta = int(start) - int(submit)
    except ValueError:
        return None
    return delta if delta >= 0 else None


def _hostname() -> str:
    return os.environ.get("HOSTNAME") or os.uname().nodename


def _read_nextflow_log(log_path: Path, max_bytes: int = 16 * 1024 * 1024) -> bytes | None:
    """Best-effort read of the .nextflow.log file, capped at the server's size limit.

    Returns None if the file is missing or unreadable. Truncates from the
    *front* (keeps the tail) on oversized files, since the failure context
    we care about is at the end.
    """
    if not log_path.is_file():
        return None
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read()
    except Exception as e:
        print(f"[run_wrapper] could not read {log_path}: {e}", file=sys.stderr, flush=True)
        return None


class _Heartbeat:
    """Daemon thread that POSTs heartbeat events on a fixed interval.

    Pass ``interval_seconds <= 0`` to disable heartbeats entirely (start()
    becomes a no-op, no thread is created). Otherwise the value is the
    number of seconds between heartbeats; sub-second values are allowed
    so tests can exercise the loop without real waits.
    """

    def __init__(self, client: httpx.Client | None, run_name: str, interval_seconds: float) -> None:
        self._client = client
        self._run_name = run_name
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._enabled = interval_seconds > 0
        self._thread = (
            threading.Thread(target=self._loop, daemon=True, name="nf-heartbeat")
            if self._enabled else None
        )

    def start(self) -> None:
        if self._thread is not None:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        """Block until the heartbeat thread exits (or *timeout* seconds elapse).

        The caller should call stop() first; join() guarantees that any
        in-flight heartbeat POST completes before resources (like the httpx
        client) are torn down.
        """
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        # First heartbeat fires after one full interval — Nextflow startup
        # is fast and we don't want to double up with pre_nextflow.
        while not self._stop.wait(self._interval):
            _post_event(
                self._client, self._run_name,
                {"type": "heartbeat", "utc_time": _now_iso()},
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nf-client.run_wrapper",
        description="Instrumentation wrapper around `nextflow run`.",
    )
    parser.add_argument("--run-name", required=True, help="Nextflow run name (-name).")
    parser.add_argument(
        "--telemetry-url", required=True,
        help=(
            "API base URL of the telemetry server (the same value as "
            "ClientConfig.server_url, e.g. 'https://nf-telemetry.example.com/api'). "
            "Trailing slash is optional; we append one. The wrapper POSTs to "
            "<base>/runs/<run_name>/event."
        ),
    )
    parser.add_argument(
        "--heartbeat-seconds", type=float, default=_HEARTBEAT_SECONDS_DEFAULT,
        help=(
            f"Heartbeat interval in seconds (default {_HEARTBEAT_SECONDS_DEFAULT}). "
            "Set to 0 (or negative) to disable heartbeats."
        ),
    )
    parser.add_argument(
        "--nextflow-log", type=Path, default=Path.cwd() / ".nextflow.log",
        help="Path to .nextflow.log to upload on exit (default: cwd/.nextflow.log).",
    )
    parser.add_argument(
        "nextflow_cmd", nargs="+",
        help="The nextflow command to run, e.g. `nextflow run repo -revision X ...`. "
             "Use `--` before the command if any of its args start with `-`.",
    )
    args = parser.parse_args(argv)

    # httpx.Client(base_url=...) can raise for malformed URLs. Telemetry
    # must NEVER prevent the run, so fall back to no-op mode (client=None
    # → _post_event silently returns) and still execute the subprocess.
    #
    # httpx resolves relative paths against base_url per RFC 3986: without a
    # trailing slash, the last segment of base_url is replaced. We force a
    # trailing slash so callers can pass `.../api` or `.../api/`
    # interchangeably and our `runs/{run_name}/event` path always appends.
    base_url = args.telemetry_url.rstrip("/") + "/"
    client: httpx.Client | None
    try:
        client = httpx.Client(base_url=base_url)
    except Exception as e:
        print(
            f"[run_wrapper] could not construct httpx.Client for "
            f"'{base_url}': {e}; continuing with telemetry disabled.",
            file=sys.stderr, flush=True,
        )
        client = None

    try:
        host = _hostname()
        slurm_job_id = os.environ.get("SLURM_JOB_ID")

        # 1. wrapper_started — first thing, even before queue-wait calculation
        _post_event(client, args.run_name, {
            "type": "wrapper_started",
            "utc_time": _now_iso(),
            "hostname": host,
            "slurm_job_id": slurm_job_id,
        })

        # 2. pre_nextflow — we have a node, computing queue wait
        _post_event(client, args.run_name, {
            "type": "pre_nextflow",
            "utc_time": _now_iso(),
            "hostname": host,
            "wait_seconds": _wait_seconds_from_slurm(),
        })

        # 3. spawn nextflow. If Popen itself fails (command-not-found,
        # permission denied, ENOEXEC) we want a wrapper_exited event with a
        # conventional shell-style exit code so the failure is observable
        # via telemetry — otherwise an OSError here would crash silently
        # before any "I tried to run nextflow" signal reached the server.
        start_ts = time.time()
        try:
            proc = subprocess.Popen(args.nextflow_cmd)
        except OSError as e:
            print(
                f"[run_wrapper] could not start nextflow subprocess "
                f"({args.nextflow_cmd[0]!r}): {e}",
                file=sys.stderr, flush=True,
            )
            _post_event(client, args.run_name, {
                "type": "wrapper_exited",
                "utc_time": _now_iso(),
                "exit_code": 127,  # POSIX shell convention: command not found
                "duration_seconds": int(time.time() - start_ts),
            })
            return 127

        heartbeat = _Heartbeat(client, args.run_name, args.heartbeat_seconds)
        heartbeat.start()

        # SLURM sends SIGTERM before the wall-time hard kill; forward it
        # to nextflow so it has a chance to write a final .nextflow.log
        # before SIGKILL hits.
        def _forward_signal(signum, _frame):
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, _forward_signal)
        signal.signal(signal.SIGINT, _forward_signal)

        # 4. wait for nextflow to finish (any exit code is fine)
        try:
            exit_code = proc.wait()
        finally:
            # Stop the heartbeat thread AND wait for any in-flight POST to
            # return before the outer `finally` closes the httpx client.
            # Without the join, a heartbeat could try to use a closed client.
            heartbeat.stop()
            heartbeat.join(timeout=_EVENT_POST_TIMEOUT + 1)
        duration = int(time.time() - start_ts)

        # 5. read .nextflow.log and upload as the wrapper_exited attachment
        log_bytes = _read_nextflow_log(args.nextflow_log)
        files = (
            {"nextflow_log": (".nextflow.log", log_bytes, "text/plain")}
            if log_bytes is not None else None
        )

        _post_event(
            client, args.run_name,
            {
                "type": "wrapper_exited",
                "utc_time": _now_iso(),
                "exit_code": exit_code,
                "duration_seconds": duration,
            },
            files=files,
            timeout=_LOG_UPLOAD_TIMEOUT,
        )

        return exit_code
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
