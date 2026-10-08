#!/bin/bash
# Start the nf-client daemon if it is not running on this login node (#240).
# Run from the user's crontab on the node the daemon lives on:
#
#   @reboot      sleep 120; $NF_TEL_REPO/config/nf_tel_watchdog.sh
#   */10 * * * * $NF_TEL_REPO/config/nf_tel_watchdog.sh
#
# Cron's environment is minimal, so everything comes from ~/.nf_tel.env. The
# daemon itself is started exactly like the documented restart: tmux session
# `nf` running config/nf_tel_daemon.sh. Restarts are logged to watchdog.log.
set -uo pipefail
source ~/.nf_tel.env

# One watchdog at a time on this node (a slow restart must not overlap the next tick).
exec 9>"/tmp/nf_tel_watchdog.$USER.lock"
flock -n 9 || exit 0

pgrep -u "$USER" -f 'nf-client daemon' >/dev/null && exit 0

# A leftover session (daemon exited, shell gone) would block new-session.
tmux kill-session -t nf 2>/dev/null
sleep 3  # killing the last session stops the tmux server; an immediate new-session dies with it
tmux new-session -d -s nf "$NF_TEL_REPO/config/nf_tel_daemon.sh"
sleep 10
if pgrep -u "$USER" -f 'nf-client daemon' >/dev/null; then
    msg="started nf-client daemon"
else
    msg="FAILED to start nf-client daemon (see daemon.log)"
fi
echo "$(date -u +%FT%TZ) $(hostname -s) watchdog: $msg" >> "$NF_TEL_DAEMON/watchdog.log"
