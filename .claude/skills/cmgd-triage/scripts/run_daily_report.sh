#!/usr/bin/env bash
# Daily cmgd health-report runner (for cron). Read-only; mutates nothing.
# Writes a dated report + latest.txt to $CMGD_HEALTH_DIR (default ~/.cmgd-health),
# keeps 30 days, and drops an attention marker when the report is non-green so a
# follow-on step (notification, or an LLM deep-dive) can pick it up.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${CMGD_HEALTH_DIR:-$HOME/.cmgd-health}"
PY="${PYTHON:-python3}"
mkdir -p "$LOGDIR"

STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
OUT="$LOGDIR/report-$STAMP.txt"

"$PY" "$SCRIPT_DIR/health_report.py" "$@" >"$OUT" 2>&1
rc=$?

cp -f "$OUT" "$LOGDIR/latest.txt"
if [ "$rc" -ne 0 ]; then
    cp -f "$OUT" "$LOGDIR/attention-latest.txt"
    # Surface on stdout too so cron MAILTO (if configured) delivers it.
    echo "cmgd health report: NON-GREEN (rc=$rc) — see $OUT"
    tail -n 20 "$OUT"
fi

# Publish to the per-version GitHub tracking issue (best-effort — never masks the
# health verdict). Disable with CMGD_GITHUB=0.
if [ "${CMGD_GITHUB:-1}" = "1" ]; then
    "$PY" "$SCRIPT_DIR/post_github.py" \
        --report-file "$OUT" --status "$rc" \
        --repo "${CMGD_REPO:-seandavi/nextflow_telemetry}" \
        --state-file "$LOGDIR/issue.json" \
        --mention "${CMGD_MENTION:-@seandavi}" \
        --project "${CMGD_PROJECT:-}" --project-owner "${CMGD_PROJECT_OWNER:-seandavi}" \
        || echo "WARN: github publish failed (rc=$?); local report at $OUT"
fi

# retention: keep 30 days of dated reports
find "$LOGDIR" -name 'report-*.txt' -mtime +30 -delete 2>/dev/null

exit "$rc"
