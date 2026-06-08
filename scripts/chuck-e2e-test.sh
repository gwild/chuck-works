#!/usr/bin/env bash
# chuck-e2e-test.sh — end-to-end regression test for chuck_receiver.ck.
#
# Pins the three behaviors fixed in PR #2392 (receiver deaf to /load after
# /start; envelopes erasing dur_ticks; phrase loops off the bar grid — the
# first is the one this harness can assert from logs alone):
#
#   1. /load AFTER /start must still load (pre-#2392 receivers processed
#      /start inside the OSC dispatch loop and never returned — every
#      post-start /load was silently dropped; the 2026-06-05 jam loaded
#      ZERO phrases). THE regression assertion.
#   2. Loaded phrases must actually play each cycle.
#   3. A second /start must supersede the running transport (generation
#      guard), not stack a second clock.
#
# Runs the receiver under `chuck --silent` (virtual time, no audio device
# needed) and drives it with real OSC over loopback via chuck_send.py.
# Expected: PASS on a #2392+ receiver; FAIL (assertion 1) on pre-#2392.
# If a future receiver changes its log strings, update the assertions here
# in the same diff — the strings ARE the contract this harness reads.
#
# Exit codes: 0 = pass, 1 = assertion failure (named), 77 = skipped
# (chuck not installed — the receiver only deploys on beelink/linux, so
# other platforms attest the skip path; 77 is the conventional skip code).
#
# Provenance: Rebecca's independent review repro of #2392 (sd-3,
# 2026-06-06), converted to a committed harness per Jan's ask — this file
# replaces two ad-hoc proofs that lived only in chat.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
RECEIVER="$REPO_ROOT/scripts/chuck_receiver.ck"
SENDER="$REPO_ROOT/scripts/chuck_send.py"

fail() { echo "FAIL: $1" >&2; exit 1; }

# ── Preconditions ────────────────────────────────────────────────────
if ! command -v chuck >/dev/null 2>&1; then
    echo "SKIP: chuck not installed on this host — receiver deploys on beelink/linux only" >&2
    exit 77
fi
[ -f "$RECEIVER" ] || fail "receiver not found: $RECEIVER"
[ -f "$SENDER" ] || fail "sender not found: $SENDER"
PYTHON_BIN="python3"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python3 not on PATH"

# Settle windows from config.yaml (timers.chuck_e2e) — fail loud if the
# block is absent, per the no-hardcoded-timers rule. These are loopback
# settle windows, not musical durations (receiver runs in virtual time).
# NOTE: honors an inherited CONFIG_FILE (repo convention) — in agent
# sessions that env var may point at a checkout whose config predates
# timers.chuck_e2e, and the "missing" fail-loud below is about THAT file,
# not this worktree's. Pin CONFIG_FILE to this checkout when in doubt
# (bit Jan during review at 666469e3).
CONFIG_FILE="${CONFIG_FILE:-$REPO_ROOT/config.yaml}"
TIMER_LINE="$("$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
t = cfg["timers"]["chuck_e2e"]  # KeyError = fail loud
print(t["startup_wait_sec"], t["poll_interval_sec"],
      t["load_settle_sec"], t["supersede_settle_sec"])
PY
)" || fail "timers.chuck_e2e missing/invalid in $CONFIG_FILE"
read -r STARTUP_WAIT POLL_INTERVAL LOAD_SETTLE SUPERSEDE_SETTLE <<EOF_T
$TIMER_LINE
EOF_T
[ -n "${SUPERSEDE_SETTLE:-}" ] || fail "timers.chuck_e2e parsed empty from $CONFIG_FILE"

# Refuse to run if OSC port 9000 is already bound (a live receiver or a
# previous test run) — failing loud beats sending test phrases into a
# production jam.
if "$PYTHON_BIN" - <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(("127.0.0.1", 9000))
except OSError:
    sys.exit(0)   # bound elsewhere -> port busy
finally:
    s.close()
sys.exit(1)       # bind ok -> port free
PY
then
    fail "OSC port 9000 already bound — is a receiver already running on this host?"
fi

# ── Launch receiver (virtual time, no audio) ─────────────────────────
LOG="$(mktemp "${TMPDIR:-/tmp}/chuck-e2e.XXXXXX.log")"
chuck --silent "$RECEIVER" > "$LOG" 2>&1 &
CHUCK_PID=$!
cleanup() { kill "$CHUCK_PID" 2>/dev/null; wait "$CHUCK_PID" 2>/dev/null; }
trap cleanup EXIT

# Wait (bounded by timers.chuck_e2e.startup_wait_sec) for the receiver.
WAITED=0
while [ "$WAITED" -lt "$STARTUP_WAIT" ]; do
    grep -q "Listening on OSC port" "$LOG" && break
    sleep "$POLL_INTERVAL"
    WAITED=$((WAITED + POLL_INTERVAL))
done
grep -q "Listening on OSC port" "$LOG" || fail "receiver never reached 'Listening on OSC port' within ${STARTUP_WAIT}s (see $LOG)"

# ── Drive the regression sequence: /start FIRST, /loads AFTER ────────
"$PYTHON_BIN" "$SENDER" --host 127.0.0.1 --start --bpm 120 --bars 1 \
    || fail "chuck_send.py --start failed"
sleep "$POLL_INTERVAL"
"$PYTHON_BIN" "$SENDER" --host 127.0.0.1 --agent e2e_alpha --instrument saw --revision 1 \
    --notes "60,0.5,0,480;67,0.5,1920,480" || fail "first /load send failed"
"$PYTHON_BIN" "$SENDER" --host 127.0.0.1 --agent e2e_beta --instrument square --revision 1 \
    --notes "48,0.6,960,480" || fail "second /load send failed"
sleep "$LOAD_SETTLE"

# Supersession probe: a second /start must replace, not stack.
"$PYTHON_BIN" "$SENDER" --host 127.0.0.1 --start --bpm 140 --bars 1 \
    || fail "second /start send failed"
sleep "$SUPERSEDE_SETTLE"

# ── Assertions ───────────────────────────────────────────────────────
# 1. THE regression: post-start /loads must be accepted.
grep -q "Loaded phrase from e2e_alpha" "$LOG" \
    || fail "post-/start /load from e2e_alpha was never loaded — receiver is deaf after /start (pre-#2392 bug). Log: $LOG"
grep -q "Loaded phrase from e2e_beta" "$LOG" \
    || fail "post-/start /load from e2e_beta was never loaded — receiver is deaf after /start (pre-#2392 bug). Log: $LOG"

# 2. Loaded phrases actually play.
grep -q "Playing phrase from e2e_alpha" "$LOG" \
    || fail "e2e_alpha loaded but never played. Log: $LOG"
grep -q "Playing phrase from e2e_beta" "$LOG" \
    || fail "e2e_beta loaded but never played. Log: $LOG"

# 3. Transport supersession (generation guard), not clock-stacking.
grep -q "superseded" "$LOG" \
    || fail "second /start did not supersede the running transport (no generation guard?). Log: $LOG"

ALPHA_PLAYS=$(grep -c "Playing phrase from e2e_alpha" "$LOG")
BETA_PLAYS=$(grep -c "Playing phrase from e2e_beta" "$LOG")
CYCLES=$(grep -c "Cycle complete" "$LOG")
echo "PASS: post-start loads accepted + played (alpha=$ALPHA_PLAYS beta=$BETA_PLAYS across $CYCLES cycles), transport supersession verified"
rm -f "$LOG"
exit 0
