#!/bin/sh
# RealTran client for macOS / Linux without SSH: connects straight to the server over the LAN
# (https, port 8765; the server must be running with --lan, which start.bat does).
# macOS: system audio needs the BlackHole virtual device (see README.md); it is picked up automatically.
SERVER="wss://__SERVER_IP__:8765"
cd "$(dirname "$0")" || exit 1
. ./setup_env.sh || exit 1
exec .venv/bin/python realtran_client.py --server "$SERVER" "$@"
