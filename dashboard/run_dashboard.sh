#!/bin/bash
set -eu

# Launch the chuck-works observability dashboard on beelink. The server is
# stdlib-only, so system Python is intentional here.
cd "$(dirname "$0")"

: "${STATE_FILE:=$HOME/milton-services/data/jam-state.json}"
: "${PORT:=8092}"
: "${TITLE:=chuck-works observability}"
: "${STALE_SECS:=30}"
: "${PYTHON:=/usr/bin/python3}"

export STATE_FILE PORT TITLE STALE_SECS
exec "$PYTHON" server.py
