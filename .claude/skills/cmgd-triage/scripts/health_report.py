#!/usr/bin/env python3
"""Daily cmgd fleet health report (every active registration) — read-only, safe to cron.

Runs the `cmgd-triage` "Fleet health report" sweep against the telemetry API,
applies the documented signal-vs-noise rules, prints a concise report, and sets
its exit code so cron/monitoring (or a follow-on LLM deep-dive) can react:

    exit 0  → GREEN     (healthy, or only expected background noise)
    exit 1  → ATTENTION (a documented regression signal fired)
    exit 2  → ERROR     (couldn't reach the API / malformed response)

Mutates nothing (API GETs; one read-only ssh per cluster for queue waits and
run logs, skipped with --no-ssh). Stdlib only — no deps. See the skill's
"Expected background failures" section for why each rule is what it is.

Usage:
    health_report.py [--api URL] [--workflow-id ID] [--window-hours N]
                     [--daemon-stale-min N] [--no-ssh] [--json]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

DEFAULT_API = "https://nf-telemetry.seandavi.workers.dev"
DEFAULT_WORKFLOW_ID = None  # None = every active registration

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
    # Cloudflare rejects urllib's default User-Agent with 403.
    req = urllib.request.Request(url, headers={"User-Agent": "cmgd-health-report/2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_or_none(api, path):
    """GET, but return None on 501 (v2 historical-metrics routes, nextflow_telemetry #175)."""
    try:
        return get(api, path)
    except urllib.error.HTTPError as e:
        if e.code == 501:
            return None
        raise


def total_for(api, version, process, status, window_hours, limit=1):
    q = (f"/api/metrics/processes/tasks?workflow_version={version}"
         f"&process={process}&status={status}&window_hours={window_hours}&limit={limit}")
    return get(api, q)


REPO_API = "https://api.github.com/repos/seandavi/nextflow_telemetry"
CLIENT_PATHS = ("packages/nf_client/", "templates/", "config/")


def check_client_drift(r, d):
    """Flag a daemon whose commit (nf_client_version `<ver>+<sha7>`, #254) is
    missing changes on main that the cluster runs: client, templates, config."""
    ver = d.get("nf_client_version") or ""
    if "+" not in ver:
        r.note(f"daemon {d['agent_id']} reports no commit ({ver or 'no version'}); "
               f"redeploy with `just deploy-clients` (#254)")
        return
    sha = ver.rsplit("+", 1)[1]
    try:
        req = urllib.request.Request(f"{REPO_API}/compare/{sha}...main",
                                     headers={"User-Agent": "cmgd-health-report/2"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            files = [f["filename"] for f in json.load(resp).get("files", [])]
    except Exception as e:  # GitHub down or rate-limited: say so, don't fail the report
        r.note(f"daemon {d['agent_id']}: drift check against main failed ({e})")
        return
    behind = [f for f in files if f.startswith(CLIENT_PATHS)]
    if behind:
        r.flag(f"daemon {d['agent_id']} runs {sha}; main changed {len(behind)} deployed "
               f"file(s) since (e.g. {behind[0]}); run `just deploy-clients`")


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
    r.say(f"cmgd health report — {now}")
    r.say(f"api: {api}")
    r.say("=" * 60)
    workflows = get(api, "/api/workflows?status=active")
    ids = sorted({w["workflow_id"] for w in workflows if not w["workflow_id"].startswith("nf_testing")})
    for wid in ([args.workflow_id] if args.workflow_id else ids):
        r.say("")
        r.say(f"## {wid}")
        workflow_section(r, args, workflows, wid)
    daemons_section(r, args)
    runs = runs_section(r, args)
    if not args.no_ssh:
        clusters_section(r, args, runs)
    if r.notes:
        r.say("")
        r.say("expected noise:")
    for nt in r.notes:
        r.say(f"  [expected] {nt}")
    return r


def workflow_section(r, args, workflows, WORKFLOW_ID):
    api = args.api
    active = [w for w in workflows if w["workflow_id"] == WORKFLOW_ID]
    if len(active) != 1:
        r.flag(f"expected exactly 1 active {WORKFLOW_ID} row, found {len(active)} "
               f"(reconcile double-dispatches every sample if >1)")
        if not active:
            r.say(f"no active {WORKFLOW_ID} workflow — nothing to report")
            return
    wf = active[0]
    pk = wf["id"]
    version = args.version or wf["version"]
    r.say(f"active workflow: pk={pk} version={version} revision={wf['revision']}")

    # --- job summary (authoritative) ---
    js = get(api, f"/api/workflows/{pk}/job-summary")
    r.say("")
    r.say(f"jobs: {js['completed']}/{js['total']} complete ({js['completion_pct']}%)  "
          f"running={js['running']} pending={js['pending']} "
          f"claimed={js.get('claimed', 0)} submitted={js.get('submitted', 0)} "
          f"failed={js['failed']} dead_letter={js['dead_letter']}")
    if js["total"]:
        dlq_pct = 100.0 * js["dead_letter"] / js["total"]
        if dlq_pct >= args.dlq_pct:
            r.flag(f"dead-letter is {dlq_pct:.1f}% of this batch "
                   f"({js['dead_letter']}/{js['total']}) — over {args.dlq_pct}% threshold")

    throughput(r, api, pk, js)

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

    # --- failure signatures (what's breaking) ---
    sigs = get_or_none(api, f"/api/metrics/processes/failure-signatures"
                            f"?workflow_version={version}&window_hours={args.window_hours}")
    r.say("")
    if sigs is None:
        r.say("failure signatures / assessment: historical metrics not available yet (#175)")
        sigs = {}
    else:
        r.say(f"failure signatures (last {args.window_hours}h):")
    if sigs and not sigs.get("rows"):
        r.say("  none")
    for row in sigs.get("rows", []):
        r.say(f"  {row['process']:<40} exit={row['exit_code']:<6} "
              f"action={row['error_action']:<8} n={row['failures']}")

    # --- classify each failing process ---
    if sigs:
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
    tl = get_or_none(api, f"/api/metrics/processes/timeline"
                          f"?workflow_version={version}&bucket=hour&window_hours={args.window_hours}")
    if tl is None:
        r.say("")
        r.say("recent task failure rate: historical metrics not available yet (#175)")
    rows = (tl or {}).get("rows", [])
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



def daemons_section(r, args):
    api = args.api
    daemons = get(api, "/api/daemons/")
    r.say("")
    r.say("daemons:")
    for d in daemons:
        if not d.get("is_active") and "nf_testing" in (d.get("workflow_id") or ""):
            continue  # dead onclappc02 daemon — documented, ignore
        seen = d.get("last_seen_at")
        stale = _age_minutes(seen)
        flagstr = ""
        # v2 marks a daemon is_active=False once its heartbeat lapses, so
        # staleness is checked regardless; a dead daemon is the thing to flag.
        if stale is None or stale > args.daemon_stale_min:
            r.flag(f"daemon {d['agent_id']} heartbeat stale "
                   f"(last_seen {seen}, ~{stale}min)")
            flagstr = "  <-- STALE"
        if d.get("is_active", True) is not False:
            check_client_drift(r, d)
        r.say(f"  {d['agent_id']:<40} status={d.get('status')} "
              f"active_runs={d.get('active_runs')} last_seen={seen}{flagstr}")



def throughput(r, api, pk, js):
    """Completions per 6h over the last 24h (from completed_at) and an ETA."""
    now = datetime.now(timezone.utc)
    buckets = [0, 0, 0, 0]  # oldest..newest 6h window
    after = 0
    while True:
        page = get(api, f"/api/workflows/{pk}/jobs?status=completed&limit=500&after={after}", timeout=60)
        items = page.get("items", page) if isinstance(page, dict) else page
        for j in items:
            age = _age_minutes(j.get("completed_at"))
            if age is not None and age < 24 * 60:
                buckets[3 - age // 360] += 1
        if len(items) < 500:
            break
        after = items[-1]["job_id"]
    left = js["pending"] + js["running"] + js.get("claimed", 0) + js.get("submitted", 0)
    rate = buckets[3] / 6 or sum(buckets) / 24
    eta = f"~{left / rate:.0f}h at {rate:.0f}/h" if rate else "no completions in 24h"
    r.say(f"completed per 6h (oldest..newest, last 24h): {buckets}; remaining {left}: {eta}")
    if left and not sum(buckets):
        r.flag(f"pk {pk}: {left} jobs left but nothing completed in 24h — claims take the "
               f"alphabetically first workflow_id with pending jobs (cf/src/control-do.ts "
               f"claimBatch), so it may be starved behind another registration")


def runs_section(r, args):
    """Active runs: age, heartbeat, job counts. /runs lists only the newest 50, so a
    shortfall against /admin/stats means older live runs are hidden from this view."""
    api = args.api
    listed = get(api, "/api/runs")["runs"]
    runs = [x for x in listed if x["status"] in ("running", "submitted")]
    live = sum(v for k, v in get(api, "/api/admin/stats").get("runs_by_status", {}).items()
               if k in ("running", "submitted"))
    r.say("")
    r.say(f"runs: {live} live ({len(runs)} visible in the newest-50 list)")
    if live > len(runs):
        r.flag(f"{live - len(runs)} live run(s) older than the newest 50 — not checked here; "
               f"long-lived runs hide there (see #350)")
    old = []
    for x in sorted(runs, key=lambda x: x.get("started_at") or "~"):
        x["jobs"] = get(api, f"/api/runs/{x['run_name']}").get("job_status_counts") or {}
        age = _age_minutes(x.get("started_at"))
        hb = _age_minutes(x.get("last_heartbeat_at"))
        if x["status"] == "running" and (hb is None or hb > args.daemon_stale_min):
            r.flag(f"run {x['run_name']} heartbeat stale (~{hb}min)")
        if age is not None and age > args.run_max_hours * 60:
            old.append(f"{x['executor_job_id']} {age // 60}h "
                       f"{x['jobs'].get('completed', 0)}/{sum(x['jobs'].values())}")
    if old:
        r.say(f"  older than {args.run_max_hours}h (job, age, samples done): {'; '.join(old)}")
        r.say("  (a long tail unless the clusters section flags its log)")
    return runs


# One read-only pass per cluster: queue state, pending waits, 24h start waits, and
# for each live run whose work dir is there, MARK_COMPLETE count + log idle minutes.
CLUSTER_SCRIPT = r"""
source ~/.nf_tel.env 2>/dev/null
now=$(date +%s)
echo "Q $(squeue -u $USER -h -t R | wc -l) $(squeue -u $USER -h -t PD | wc -l)"
squeue -u $USER -h -t PD -o "%r" | sort | uniq -c | awk '{print "R", $1, $2}'
squeue -u $USER -h -t PD -o "%V" | while read t; do echo "P $(( (now - $(date -d "$t" +%s)) / 60 ))"; done
sacct -u $USER -S now-24hours -X -n -P -o Submit,Start | grep -v Unknown |   while IFS="|" read s t; do echo "S $(( ($(date -d "$t" +%s) - $(date -d "$s" +%s)) / 60 ))"; done
for j in __JOBS__; do
  L=$NF_TEL_SCRATCH/$j/.nextflow.log
  [ -f "$L" ] && echo "L $j $(grep 'Task completed' "$L" | grep -c 'MARK_COMPLETE.*status: COMPLETED') $(( (now - $(stat -c %Y "$L")) / 60 ))"
done
"""


def _pct(v, q):
    return sorted(v)[int(q * (len(v) - 1))] if v else None


def clusters_section(r, args, runs):
    import subprocess
    jobs = " ".join(str(x["executor_job_id"]) for x in runs if x.get("executor_job_id"))
    by_job = {str(x.get("executor_job_id")): x for x in runs}
    r.say("")
    r.say("clusters (queue wait = submit to start, minutes):")
    for host in args.clusters.split(","):
        try:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR",
                 host, "bash -s"], input=CLUSTER_SCRIPT.replace("__JOBS__", jobs),
                capture_output=True, text=True, timeout=240).stdout
        except Exception as e:  # noqa: BLE001 — a dead cluster link is a note, not a crash
            r.note(f"{host}: ssh failed ({e})")
            continue
        q, reasons, pend, start = None, [], [], []
        for line in out.splitlines():
            f = line.split()
            if not f:
                continue
            if f[0] == "Q":
                q = (int(f[1]), int(f[2]))
            elif f[0] == "R":
                reasons.append(f"{f[2]}={f[1]}")
            elif f[0] == "P":
                pend.append(int(f[1]))
            elif f[0] == "S":
                start.append(int(f[1]))
            elif f[0] == "L":
                x = by_job.get(f[1])
                if not x:
                    continue
                mc, idle = int(f[2]), int(f[3])
                done = x["jobs"].get("completed", 0)
                if mc > done:
                    r.flag(f"{host} run {x['run_name']} ({f[1]}): log has {mc} MARK_COMPLETE, "
                           f"server {done} — weblog events lost (#350); replay, then scancel")
                elif idle > args.log_idle_min:
                    r.flag(f"{host} run {x['run_name']} ({f[1]}): .nextflow.log idle {idle}min "
                           f"— Nextflow may be hung (#350)")
        if q is None:
            r.note(f"{host}: no output from ssh")
            continue
        r.say(f"  {host:<9} running={q[0]} pending={q[1]} ({', '.join(reasons) or '-'}); "
              f"pending now p50={_pct(pend, .5)} max={max(pend, default=None)}; "
              f"started 24h n={len(start)} p50={_pct(start, .5)} p90={_pct(start, .9)}")
        if start and _pct(start, .5) > args.queue_wait_min:
            r.flag(f"{host}: median queue wait {_pct(start, .5)}min over 24h "
                   f"(>{args.queue_wait_min}) — runs are queue-bound there")


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
    ap.add_argument("--workflow-id", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--version", default=None, help="override workflow version (default: active row's)")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--daemon-stale-min", type=int, default=15)
    ap.add_argument("--dlq-pct", type=float, default=3.0, help="flag if batch DLQ exceeds this %%")
    ap.add_argument("--fail-pct", type=float, default=8.0, help="flag if recent hourly failure %% exceeds this")
    ap.add_argument("--min-unknown", type=int, default=10, help="flag an unlisted failing process only at/above this count")
    ap.add_argument("--run-max-hours", type=int, default=18, help="note runs older than this")
    ap.add_argument("--log-idle-min", type=int, default=60, help="flag a live run whose log is idle this long")
    ap.add_argument("--queue-wait-min", type=int, default=60, help="flag a cluster whose 24h median wait exceeds this")
    ap.add_argument("--clusters", default="alpine,anvil,bridges2", help="ssh aliases to check")
    ap.add_argument("--no-ssh", action="store_true", help="API checks only")
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
