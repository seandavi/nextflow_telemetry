#!/bin/bash
# Login-node watchdog, run from the user's crontab on the daemon's node (#240):
#
#   @reboot      sleep 120; $NF_TEL_REPO/config/nf_tel_watchdog.sh
#   */10 * * * * $NF_TEL_REPO/config/nf_tel_watchdog.sh
#
# 1. Starts the nf-client daemon if it is not running (tmux session `nf`).
# 2. Sweeps run directories ($NF_TEL_SCRATCH/<jobid>) whose SLURM job is gone:
#    the batch script removes its own directory on every exit it sees; this
#    catches node loss and kill -9. Directories marked .keep_failed are kept for
#    NF_TEL_KEEP_FAILED_HOURS (default 48). Non-numeric entries are never touched.
# Cron's environment is minimal, so everything comes from ~/.nf_tel.env.
set -uo pipefail
source ~/.nf_tel.env

log() { echo "$(date -u +%FT%TZ) $(hostname -s) watchdog: $*" >> "$NF_TEL_DAEMON/watchdog.log"; }

# One watchdog at a time on this node (a slow sweep must not overlap the next tick).
exec 9>"/tmp/nf_tel_watchdog.$USER.lock"
flock -n 9 || exit 0

start_daemon() {
    pgrep -u "$USER" -f 'nf-client daemon' >/dev/null && return
    # A leftover session (daemon exited, shell gone) would block new-session.
    tmux kill-session -t nf 2>/dev/null
    sleep 3  # killing the last session stops the tmux server; an immediate new-session dies with it
    tmux new-session -d -s nf "$NF_TEL_REPO/config/nf_tel_daemon.sh"
    sleep 10
    if pgrep -u "$USER" -f 'nf-client daemon' >/dev/null; then
        log "started nf-client daemon"
    else
        log "FAILED to start nf-client daemon (see daemon.log)"
    fi
}

sweep_run_dirs() {
    [ -d "${NF_TEL_SCRATCH:-}" ] || return
    local live keep_min=$(( ${NF_TEL_KEEP_FAILED_HOURS:-48} * 60 ))
    # If squeue fails (controller down) we know nothing about live jobs: sweep nothing.
    live=$(squeue -h -u "$USER" -o '%i') || { log "squeue failed; sweep skipped"; return; }
    for d in "$NF_TEL_SCRATCH"/*/; do
        local id
        id=$(basename "$d")
        [[ $id =~ ^[0-9]+$ ]] || continue          # only <jobid> run directories
        grep -qx "$id" <<<"$live" && continue      # job still queued or running
        [ -n "$(find "$d" -maxdepth 0 -mmin -60)" ] && continue   # just created: give squeue time
        if [ -e "$d/.keep_failed" ] && [ -z "$(find "$d/.keep_failed" -mmin +$keep_min)" ]; then
            continue                               # kept for debugging, not expired yet
        fi
        rm -rf "$d" && log "swept run directory $d (job $id not in squeue)"
    done
}

start_daemon
sweep_run_dirs
