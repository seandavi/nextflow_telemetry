#!/usr/bin/env python3
"""Post a cmgd health report to a per-version GitHub tracking issue.

One issue per (workflow_id, version) — e.g. `cmgd_nextflow 2.2.1 — fleet health`.
It rotates automatically when a new version goes active (i.e. after a release):
the report's active version changes, a fresh issue is opened, and the prior
version's issue is closed with a pointer. The issue is that version's health
record for its whole rollout.

Notification model (deliberate — see cmgd-triage skill):
  * Every run EDITS the issue body (current status + rolling table + latest
    report). Body edits do NOT notify — a silent daily heartbeat.
  * A COMMENT is posted only when the run is non-green (ATTENTION/ERROR) or on
    recovery (non-green -> green). Comments notify; non-green ones @-mention to
    hard-ping. So notification volume == number of bad days.

Consumes the text report + exit status from health_report.py (via
run_daily_report.sh); does not re-query telemetry. GitHub I/O via `gh` (token in
~/.config/gh/hosts.yml, works headless). Best-effort: a `gh` failure must not mask
the health verdict, so the caller keeps its own exit code.

Usage:
  post_github.py --report-file OUT.txt --status {0|1|2} \
                 --repo seandavi/nextflow_telemetry \
                 --state-file ~/.cmgd-health/issue.json [--mention @seandavi] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VERDICT = {0: ("GREEN", "🟢"), 1: ("ATTENTION", "🔴"), 2: ("ERROR", "⚠️")}
EMOJI = {"GREEN": "🟢", "ATTENTION": "🔴", "ERROR": "⚠️"}
# Flipping status label — makes the issue list / project board scannable at a
# glance. Label changes do NOT notify, so no added noise. (name, color, desc)
STATUS_LABELS = {
    "GREEN":     ("status:green",     "0e8a16", "fleet health: all green"),
    "ATTENTION": ("status:attention", "d93f0b", "fleet health: regression signal"),
    "ERROR":     ("status:error",     "fbca04", "fleet health: report could not run"),
}


def gh(args, dry_run=False):
    cmd = ["gh"] + args
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return ""
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()}")
    return (r.stdout or "").strip()


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def parse_report(text):
    """-> (workflow_id, version, date). version None if the report couldn't
    determine an active workflow (e.g. API-unreachable run)."""
    head = text.splitlines()[0] if text else ""
    wf = head.split()[0] if head else "cmgd_nextflow"
    vm = re.search(r"active workflow:.*version=(\S+)", text)
    dm = re.search(r"(\d{4}-\d{2}-\d{2})", head)
    date = dm.group(1) if dm else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return wf, (vm.group(1) if vm else None), date


def attention_lines(text):
    return [ln.strip()[2:].strip() for ln in text.splitlines()
            if ln.lstrip().startswith("! ")]


def sync_status_label(repo, num, verdict, dry_run):
    """Set status:<verdict>, remove the other two. Ensures the labels exist.
    Label changes don't notify subscribers."""
    want = STATUS_LABELS[verdict][0]
    for v, (name, color, desc) in STATUS_LABELS.items():
        try:  # best-effort ensure the label exists in the repo
            gh(["label", "create", name, "--repo", repo, "--color", color,
                "--description", desc], dry_run=dry_run)
        except RuntimeError:
            pass
    drop = [name for v, (name, _, _) in STATUS_LABELS.items() if name != want]
    args = ["issue", "edit", str(num), "--repo", repo, "--add-label", want]
    for d in drop:
        args += ["--remove-label", d]
    try:
        gh(args, dry_run=dry_run)
    except RuntimeError as e:
        print(f"WARN: status label sync failed: {e}", file=sys.stderr)


def issue_open(repo, num):
    try:
        info = json.loads(gh(["issue", "view", str(num), "--repo", repo,
                              "--json", "state"]))
        return info.get("state") == "OPEN"
    except (RuntimeError, ValueError):
        return None  # missing/deleted


def create_issue(repo, title, dry_run):
    if dry_run:
        gh(["issue", "create", "--repo", repo, "--title", title,
            "--label", "health", "--body", "(init)"], dry_run=True)
        return 999999
    try:  # best-effort: ensure the label exists so --label sticks
        gh(["label", "create", "health", "--repo", repo, "--color", "0e8a16",
            "--description", "automated fleet-health heartbeat"])
    except RuntimeError:
        pass  # already exists (or no perms) — create below falls back if needed
    try:
        url = gh(["issue", "create", "--repo", repo, "--title", title,
                  "--label", "health", "--body", "_initializing…_"])
    except RuntimeError:
        url = gh(["issue", "create", "--repo", repo, "--title", title,
                  "--body", "_initializing…_"])  # retry w/o label
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def build_body(entry, wf, version, verdict, date, report_text):
    hist = entry.get("history", [])[-30:]
    rows = "\n".join(f"| {d} | {EMOJI.get(v, '')} {v} |"
                     for d, v in reversed(hist)) or "| — | — |"
    return (
        f"**{wf} `{version}` — Status: {EMOJI[verdict]} {verdict}** · "
        f"last run {date} ({datetime.now(timezone.utc).strftime('%H:%MZ')})\n\n"
        f"Automated per-version fleet-health heartbeat (read-only). The body is "
        f"rewritten each run — **no notification**. A **comment** is posted only on "
        f"non-green days and on recovery. Rotates to a new issue when a new version "
        f"goes active. See the `cmgd-triage` skill.\n\n"
        f"| date | verdict |\n|---|---|\n{rows}\n\n"
        f"<details><summary>latest report ({date})</summary>\n\n"
        f"```\n{report_text.strip()}\n```\n</details>\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-file", required=True)
    ap.add_argument("--status", type=int, required=True, choices=[0, 1, 2])
    ap.add_argument("--repo", default="seandavi/nextflow_telemetry")
    ap.add_argument("--state-file",
                    default=os.path.expanduser("~/.cmgd-health/issue.json"))
    ap.add_argument("--mention", default="")
    ap.add_argument("--project", default="", help="project number to add new version-issues to")
    ap.add_argument("--project-owner", default="seandavi")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.report_file) as f:
        report_text = f.read()
    verdict, emoji = VERDICT[args.status]
    wf, version, date = parse_report(report_text)

    state = load_state(args.state_file)
    state.setdefault("repo", args.repo)
    issues = state.setdefault("issues", {})  # key "wf@ver" -> {number,last_verdict,history}

    # No version (API-unreachable run): fall back to the current issue so the
    # ERROR still pings; never create a versionless issue.
    if version is None:
        key = state.get("current_key")
        if not key or key not in issues:
            print("WARN: no active version and no known issue — skipping github",
                  file=sys.stderr)
            return 3
    else:
        key = f"{wf}@{version}"

    try:
        entry = issues.get(key)
        created = False

        if entry is None:
            # new version -> new issue; close the prior current issue
            number = create_issue(args.repo, f"{wf} {version} — fleet health", args.dry_run)
            prior_key = state.get("current_key")
            prior = issues.get(prior_key) if prior_key and prior_key != key else None
            if prior and issue_open(args.repo, prior["number"]):
                try:
                    gh(["issue", "close", str(prior["number"]), "--repo", args.repo,
                        "--comment", f"Superseded by {version} — see #{number}."],
                       dry_run=args.dry_run)
                except RuntimeError:
                    pass
            entry = {"number": number, "last_verdict": None, "history": []}
            issues[key] = entry
            created = True
        else:
            number = entry["number"]
            st = issue_open(args.repo, number)
            if st is None:                     # deleted -> recreate
                number = create_issue(args.repo, f"{wf} {version} — fleet health", args.dry_run)
                entry["number"] = number
                created = True
            elif st is False and args.status != 0:   # closed but now non-green -> reopen
                try:
                    gh(["issue", "reopen", str(number), "--repo", args.repo], dry_run=args.dry_run)
                except RuntimeError:
                    pass

        state["current_key"] = key
        prev_verdict = entry.get("last_verdict")

        # rolling history (dedupe same-day re-runs)
        hist = [hv for hv in entry.get("history", []) if hv[0] != date]
        hist.append([date, verdict])
        entry["history"] = hist[-60:]

        body = build_body(entry, wf, version or "?", verdict, date, report_text)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write(body)
            body_path = tf.name
        gh(["issue", "edit", str(number), "--repo", args.repo,
            "--body-file", body_path], dry_run=args.dry_run)
        os.unlink(body_path)

        sync_status_label(args.repo, number, verdict, args.dry_run)

        # add a freshly-created version issue to the project board (best-effort)
        if created and args.project:
            try:
                url = f"https://github.com/{args.repo}/issues/{number}"
                gh(["project", "item-add", args.project, "--owner", args.project_owner,
                    "--url", url], dry_run=args.dry_run)
            except RuntimeError as e:
                print(f"WARN: project add failed: {e}", file=sys.stderr)

        comment = None
        if args.status != 0:
            bullets = "\n".join(f"- {b}" for b in attention_lines(report_text)) or "- (see report)"
            m = (args.mention + " ") if args.mention else ""
            comment = (f"{m}{emoji} **{verdict}** — {wf} `{version}` — {date}\n\n{bullets}\n\n"
                       f"<details><summary>full report</summary>\n\n"
                       f"```\n{report_text.strip()}\n```\n</details>")
        elif prev_verdict and prev_verdict != "GREEN":
            comment = f"🟢 **Recovered** — GREEN as of {date} (was {prev_verdict})."
        if comment:
            gh(["issue", "comment", str(number), "--repo", args.repo,
                "--body", comment], dry_run=args.dry_run)

        entry["last_verdict"] = verdict
        if not args.dry_run:
            save_state(args.state_file, state)
        print(f"github: {key} -> issue #{number} ({'created' if created else 'updated'}); "
              f"verdict={verdict}; comment={'yes' if comment else 'no'}")
        return 0
    except (RuntimeError, OSError, ValueError) as e:
        print(f"WARN: github post failed (report still logged locally): {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
