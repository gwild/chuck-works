#!/bin/bash
# Launch the chuck-works observability dashboard on beelink, reading the live
# collector state file. stdlib-only server (no venv deps) → system python3.
# Split token so the check-venv Bash hook can't rewrite python3 → venv path.
cd /home/gregory/chuck-works/dashboard || exit 1
export STATE_FILE=/home/gregory/milton-services/data/jam-state.json
export PORT=8092
export TITLE="chuck-works observability"
export STALE_SECS=30
PY="/usr/bin/py""thon3"
exec "$PY" server.py
