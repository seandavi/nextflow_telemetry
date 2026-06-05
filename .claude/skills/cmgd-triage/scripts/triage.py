#!/usr/bin/env python3
"""One-shot triage dump for a cmgd run.

Usage:
    triage.py <run_name> [--api URL]

Prints the run's classification, status, wrapper exit code, SLURM state, and
task_status_counts — the four things you look at first. Highlights the failing
process count so you know whether it "almost worked" (high COMPLETED, low FAILED)
or died early.
"""
import argparse
import json
import sys
import urllib.request

DEFAULT_API = "https://nf-telemetry.cancerdatasci.org"


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("--api", default=DEFAULT_API)
    args = ap.parse_args()

    url = f"{args.api}/api/runs/{args.run_name}"
    try:
        d = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR fetching {url}: {e}", file=sys.stderr)
        return 1

    keys = [
        "run_name", "revision", "status", "classification",
        "wrapper_exit_code", "last_known_slurm_state", "slurm_reason",
        "executor_job_id", "nextflow_log_uploaded_at", "last_heartbeat_at",
    ]
    for k in keys:
        print(f"{k:28} {d.get(k)}")

    counts = d.get("task_status_counts") or {}
    print("\ntask_status_counts:")
    for k, v in sorted(counts.items()):
        print(f"  {k:12} {v}")

    failed = counts.get("FAILED", 0)
    completed = counts.get("COMPLETED", 0)
    if failed and completed:
        print(
            f"\n=> {completed} COMPLETED, {failed} FAILED: pipeline ran far and "
            f"died on {failed} task(s). Pull that process's command_err / "
            f"command_out via /api/task-logs/{args.run_name}/<task_hash>."
        )
    elif d.get("classification") == "ended-no-log":
        print(
            "\n=> ended-no-log: driver likely killed (OOM?). Check SLURM: "
            f"sacct -j {d.get('executor_job_id')} "
            "--format=JobID,State,ExitCode,MaxRSS,ReqMem"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
