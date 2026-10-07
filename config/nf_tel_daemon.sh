#!/bin/bash
# Run the nf-client daemon in the foreground; tmux owns the process. Same
# script on every cluster — values come from ~/.nf_tel.env, the bearer token
# from ~/.nf_tel.secrets (mode 600, `export NF_OPERATOR_TOKEN=...`).
#
#   source ~/.nf_tel.env && tmux new-session -d -s nf "$NF_TEL_REPO/config/nf_tel_daemon.sh"
#
# No `&` or send-keys: Anvil's slow login rc races send-keys, and background
# jobs under `ssh host '...'` hang the ssh session.
set -euo pipefail
source ~/.nf_tel.env
source ~/.nf_tel.secrets
cd "$NF_TEL_DAEMON"
exec nf-client daemon --config "$NF_TEL_CONFIG" >> "$NF_TEL_DAEMON/daemon.log" 2>&1
