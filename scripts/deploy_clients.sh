#!/usr/bin/env bash
# Deploy the nf-client daemon to the HPC login nodes at one pushed commit (#254).
#
#   scripts/deploy_clients.sh <alpine|anvil|all> [ref]      (or: just deploy-clients ...)
#
# Per cluster, on the login node the daemon runs on (from /api/daemons; NODE=
# overrides, e.g. for a first deploy):
#   1. refuse if the daemon has active runs (FORCE=1 overrides) or the checkout is dirty
#   2. check out the commit (detached), install ~/.nf_tel.env from config/nf_tel.env.<cluster>
#   3. reinstall nf-client from git+file://<checkout>@<sha>, so the heartbeat reports the sha
#   4. restart the daemon, then poll /api/daemons until it reports <version>+<sha7>
# The client yaml is read in place from the checkout ($NF_TEL_CONFIG), so step 2
# deploys it too. ssh aliases `alpine` and `anvil` must work non-interactively.
set -euo pipefail

API=${NF_TEL_API:-https://nf-telemetry.seandavi.workers.dev}
target=${1:?usage: deploy_clients.sh <alpine|anvil|all> [ref]}
ref=${2:-origin/main}
case $target in
    all) clusters=(alpine anvil) ;;
    alpine|anvil) clusters=("$target") ;;
    *) echo "unknown cluster: $target" >&2; exit 2 ;;
esac

git fetch -q origin
sha=$(git rev-parse --verify "$ref^{commit}")
git branch -r --contains "$sha" | grep -q . || { echo "$sha is not on origin; push it first" >&2; exit 2; }
echo "deploying ${sha:0:7} ($(git log -1 --format=%s "$sha"))"

# -> "<agent_id> <active_runs>" for the cluster's daemon, empty if it never registered
daemon_row() {
    curl -fsS --max-time 30 "$API/api/daemons/" | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if (d.get("profile") or "").split(",")[0] == sys.argv[1]:
        print(d["agent_id"], d.get("active_runs") or 0)
        break' "$1"
}

deploy() {
    local cluster=$1 row node runs
    row=$(daemon_row "$cluster")
    node=${NODE:-${row%% *}}
    runs=${row##* }
    [ -n "$node" ] || { echo "[$cluster] no daemon registered; set NODE=<login node>" >&2; return 1; }
    if [ -n "$row" ] && [ "$runs" != 0 ] && [ "${FORCE:-0}" != 1 ]; then
        echo "[$cluster] $node has $runs active run(s); wait for them or set FORCE=1" >&2
        return 1
    fi
    echo "[$cluster] $node: deploying"

    # Land on the daemon's node: the watchdog cron and tmux session live there.
    ssh -o BatchMode=yes "$cluster" \
        "if [ \"\$(hostname -s)\" = \"${node%%.*}\" ]; then exec bash -s; else exec ssh -o BatchMode=yes $node bash -s; fi" \
        <<REMOTE | { grep -v '^#' || true; }   # Alpine prints a '#' banner on stdout
set -euo pipefail
source ~/.nf_tel.env
cd "\$NF_TEL_REPO"
if [ -n "\$(git status --porcelain --untracked-files=no)" ]; then
    echo "dirty checkout in \$NF_TEL_REPO:"; git status --short --untracked-files=no; exit 3
fi
git fetch -q origin
git checkout -q --detach $sha
if ! cmp -s config/nf_tel.env.$cluster ~/.nf_tel.env; then
    diff ~/.nf_tel.env config/nf_tel.env.$cluster || true
    cp ~/.nf_tel.env ~/.nf_tel.env.bak-\$(date +%Y%m%d%H%M%S)
    cp config/nf_tel.env.$cluster ~/.nf_tel.env
    source ~/.nf_tel.env
fi
# Alpine's home is nearly full: keep uv's cache on scratch.
UV_CACHE_DIR=\$NF_TEL_SCRATCH/uv_cache uv tool install -q --reinstall --python 3.13 \
    "nf-client @ git+file://\$NF_TEL_REPO@$sha#subdirectory=packages/nf_client"
tmux kill-session -t nf 2>/dev/null || true
sleep 3   # killing the last session stops the tmux server; an immediate new-session dies with it
tmux new-session -d -s nf "\$NF_TEL_REPO/config/nf_tel_daemon.sh"
REMOTE
    [ "${PIPESTATUS[0]}" = 0 ] || { echo "[$cluster] $node: remote deploy failed" >&2; return 1; }

    local want="+${sha:0:7}" got=""
    for _ in $(seq 24); do
        sleep 5
        got=$(curl -fsS --max-time 30 "$API/api/daemons/" | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["agent_id"] == sys.argv[1]:
        print(d.get("nf_client_version") or "")' "$node")
        if [[ $got == *"$want" ]]; then echo "[$cluster] $node: running $got"; return 0; fi
    done
    echo "[$cluster] $node: heartbeat still reports '$got', expected *$want; see \$NF_TEL_DAEMON/daemon.log" >&2
    return 1
}

rc=0
for c in "${clusters[@]}"; do deploy "$c" || rc=1; done
exit $rc
