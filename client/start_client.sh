#!/bin/sh
# RealTran client for macOS / Linux, through an SSH tunnel. First run creates a Python environment here.
# Change SERVER below if you log in to the RealTran server with a different SSH name / alias.
# macOS: system audio needs the BlackHole virtual device (see README.md); it is picked up automatically.
SERVER="__SSH_USER__@__SERVER_IP__"
cd "$(dirname "$0")" || exit 1
. ./setup_env.sh || exit 1
exec .venv/bin/python realtran_client.py --ssh "$SERVER" "$@"
