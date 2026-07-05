#!/usr/bin/env python3
"""Daily cmgd_nextflow fleet health report — read-only, safe to cron.

Runs the `cmgd-triage` "Fleet health report" sweep against the telemetry API,
applies the documented signal-vs-noise rules, prints a concise report, and sets
its exit code so cron/monitoring (or a follow-on LLM deep-dive) can react:

    exit 0  → GREEN     (healthy, or only expected background noise)
    exit 1  → ATTENTION (a documented regression signal fired)
    exit 2  → ERROR     (couldn't reach the API / malformed response)

Mutates nothing (GET only). Stdlib only — no deps. See the skill's
"Expected background failures" section for why each rule is what it is.

Usage:
    health_report.py [--api URL] [--version X.Y.Z] [--window-hours N]
                     [--daemon-stale-min N] [--json]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

DEFAULT_API = "https://nf-telemetry.cancerdatasci.org"
WORKFLOW_ID = "cmgd_nextflow"

# Processes whose failures are expected background (retry-recovers or per-sample
# ignore). See skill. Anything NOT here that fails notably is worth a look.
OOM_RECOVER_PREFIXES = ("metaphlan",)          # exit 137, recovers on retry w/ more mem
DOWNLOAD_NOISE = ("fasterq_dump",)             # SRA/ENA download flakiness -> retry/ignore/DLQ
# These were fixed in 2.2.1 (KMA TMPDIR bug, exit 2). A *logic* failure here is a
# regression; an infra-abort/OOM (killed task, retries) is not — discriminate below.
REGRESSION_ZERO = ("resistome_kma_full", "resistome_kma_rarefied")

# Exit codes that mean "task was killed" (node failure, preemption, SIGKILL, OOM),
# not "the code ran and errored". Telemetry uses 2147483647 (INT_MAX) when no exit
# code was captured (ABORTED). These retry and are infra noise, not logic bugs.
INFRA_ABORT_CODES = {"2147483647", "-1", "143"}


def is_infra_abort(exit_code):
    if str(exit_code) in INFRA_ABORT_CODES:
        return True
    try:
        return 137 <= int(exit_code) <= 140  # SIGKILL/OOM family
    except (TypeError, ValueError):
        return False


def get(api, path, timeout=30):
    url = f"{api}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def total_for(api, version, process, status, window_hours, limit=1):
    q = (f"/api/metrics/processes/tasks?workflow_version={version}"
         f"&process={process}&status={status}&window_hours={window_hours}&limit={limit}")
    return get(api, q)


class Report:
    def __init__(self):
        self.lines = []
        self.attention = []   # regression signals -> exit 1
        self.notes = []       # expected background -> informational

    def say(self, s=""):
        self.lines.append(s)

    def flag(self, s):
        self.attention.append(s)

    def note(self, s):
        self.notes.append(s)

    def verdict(self):
        return "ATTENTION" if self.attention else "GREEN"


def run(args):
    api = args.api
    r = Report()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    r.say(f"cmgd_nextflow health report — {now}")
    r.say(f"api: {api}")
    r.say("=" * 60)

    # --- active workflow (pk + version) ---
    workflows = get(api, "/api/workflows?status=active")
    active = [w for w in workflows if w["workflow_id"] == WORKFLOW_ID]
    if len(active) != 1:
        r.flag(f"expected exactly 1 active {WORKFLOW_ID} row, found {len(active)} "
               f"(reconcile double-dispatches every sample if >1)")
        if not active:
            r.say("no active cmgd_nextflow workflow — nothing to report")
            return r
    wf = active[0]
    pk = wf["id"]
    version = args.version or wf["version"]
    r.say(f"active workflow: pk={pk} version={version} revision={wf['revision']}")

    # --- job summary (authoritative) ---
    js = get(api, f"/api/workflows/{pk}/job-summary")
    r.say("")
    r.say(f"jobs: {js['completed']}/{js['total']} complete ({js['completion_pct']}%)  "
          f"running={js['running']} pending={js['pending']} "
          f"failed={js['failed']} dead_letter={js['dead_letter']}")
    if js["total"]:
        dlq_pct = 100.0 * js["dead_letter"] / js["total"]
        if dlq_pct >= args.dlq_pct:
            r.flag(f"dead-letter is {dlq_pct:.1f}% of this batch "
                   f"({js['dead_letter']}/{js['total']}) — over {args.dlq_pct}% threshold")

    # --- global stats ---
    stats = get(api, "/api/admin/stats")
    r.say(f"global dead_letter_unresolved: {stats.get('dead_letter_unresolved')} "
          f"(all versions; batch's own = {js['dead_letter']})")

    # --- dispatchability ---
    disp = get(api, "/api/admin/dispatchability")
    stuck = [s for s in disp.get("stuck", [])
             if WORKFLOW_ID in json.dumps(s)]  # ignore nf_testing/onclappc02
    if stuck:
        r.flag(f"{WORKFLOW_ID} has pending jobs but no daemon claiming them: {stuck}")
    else:
        r.say(f"dispatchability: ok (cmgd not stuck)")

    # --- daemons freshness ---
    daemons = get(api, "/api/daemons/")
    r.say("")
    r.say("daemons:")
    for d in daemons:
        if not d.get("is_active") and "nf_testing" in (d.get("workflow_id") or ""):
            continue  # dead onclappc02 daemon — documented, ignore
        seen = d.get("last_seen_at")
        stale = _age_minutes(seen)
        flagstr = ""
        if WORKFLOW_ID in (d.get("workflow_id") or ""):
            if stale is None or stale > args.daemon_stale_min:
                r.flag(f"daemon {d['agent_id']} heartbeat stale "
                       f"(last_seen {seen}, ~{stale}min)")
                flagstr = "  <-- STALE"
        r.say(f"  {d['agent_id']:<40} status={d.get('status')} "
              f"active_runs={d.get('active_runs')} last_seen={seen}{flagstr}")

    # --- failure signatures (what's breaking) ---
    sigs = get(api, f"/api/metrics/processes/failure-signatures"
                    f"?workflow_version={version}&window_hours={args.window_hours}")
    r.say("")
    r.say(f"failure signatures (last {args.window_hours}h):")
    if not sigs.get("rows"):
        r.say("  none")
    for row in sigs.get("rows", []):
        r.say(f"  {row['process']:<40} exit={row['exit_code']:<6} "
              f"action={row['error_action']:<8} n={row['failures']}")

    # --- classify each failing process ---
    r.say("")
    r.say("assessment:")
    for row in sigs.get("rows", []):
        proc, n, code = row["process"], row["failures"], row["exit_code"]
        if any(proc.startswith(p) for p in REGRESSION_ZERO):
            # fixed in 2.2.1 (exit 2). A logic exit code = regression; a killed
            # task (infra-abort/OOM) is retryable noise, not the bug's return.
            if is_infra_abort(code):
                r.note(f"{proc}: {n} killed/aborted (exit {code}) — infra, retried, "
                       f"not the KMA bug")
            else:
                r.flag(f"{proc}: {n} FAILED with exit {code} — regression "
                       f"(fixed 2.2.1, expected 0)")
        elif any(proc.startswith(p) for p in OOM_RECOVER_PREFIXES):
            # healthy iff failures are attempt-1 only
            tasks = total_for(api, version, proc, "FAILED", args.window_hours, limit=500)
            attempts = sorted({t["attempt"] for t in tasks.get("rows", [])})
            retried = [a for a in attempts if a and a >= 2]
            if retried:
                r.flag(f"{proc}: {n} OOM failures include retries (attempts {attempts}) "
                       f"— memory scaling not keeping up")
            else:
                r.note(f"{proc}: {n} attempt-1 OOMs, all recover on retry — expected")
        elif any(proc.startswith(p) for p in DOWNLOAD_NOISE):
            r.note(f"{proc}: {n} download failures (exit {row['exit_code']}) "
                   f"— SRA/ENA noise; undownloadable accessions -> DLQ")
        elif is_infra_abort(code):
            r.note(f"{proc}: {n} killed/aborted (exit {code}) — infra (node/preempt/"
                   f"kill), retried; noise")
        elif n >= args.min_unknown or row["error_action"] not in ("RETRY", "IGNORE"):
            # unexpected process failing in volume, or giving up (not recovering)
            r.flag(f"{proc}: {n} failures (exit {row['exit_code']}, "
                   f"action {row['error_action']}) — not a known-noise process, investigate")
        else:
            # a handful of retryable failures on an unlisted process — recovers, noise
            r.note(f"{proc}: {n} failure(s) (exit {row['exit_code']}, retryable) "
                   f"— below investigate threshold ({args.min_unknown})")

    # --- recent failure-rate trend ---
    tl = get(api, f"/api/metrics/processes/timeline"
                  f"?workflow_version={version}&bucket=hour&window_hours={args.window_hours}")
    rows = tl.get("rows", [])
    if rows:
        recent = rows[-3:]
        tot = sum(x["total"] for x in recent)
        fail = sum(x["failed"] for x in recent)
        pct = 100.0 * fail / tot if tot else 0.0
        r.say("")
        r.say(f"recent task failure rate (last {len(recent)}h): {pct:.1f}% ({fail}/{tot})")
        if pct >= args.fail_pct:
            r.flag(f"recent failure rate {pct:.1f}% over {args.fail_pct}% threshold "
                   f"— check failure-signatures / for an SRA/cluster incident")

    for nt in r.notes:
        r.say(f"  [expected] {nt}")

    return r


def _age_minutes(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - t).total_seconds() // 60)
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--version", default=None, help="override workflow version (default: active row's)")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--daemon-stale-min", type=int, default=15)
    ap.add_argument("--dlq-pct", type=float, default=3.0, help="flag if batch DLQ exceeds this %%")
    ap.add_argument("--fail-pct", type=float, default=8.0, help="flag if recent hourly failure %% exceeds this")
    ap.add_argument("--min-unknown", type=int, default=10, help="flag an unlisted failing process only at/above this count")
    ap.add_argument("--json", action="store_true", help="emit machine-readable summary too")
    args = ap.parse_args()

    try:
        r = run(args)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"ERROR: could not produce report: {e}", file=sys.stderr)
        return 2

    print("\n".join(r.lines))
    print("=" * 60)
    verdict = r.verdict()
    print(f"VERDICT: {verdict}")
    for a in r.attention:
        print(f"  ! {a}")
    if args.json:
        print(json.dumps({"verdict": verdict, "attention": r.attention}, indent=2))
    return 1 if r.attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
