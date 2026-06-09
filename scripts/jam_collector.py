#!/usr/bin/env python3
"""jam_collector.py — chuck observability collector (#2464).

ONE writer, N readers. Joins the receiver's INTENT (what the jam is supposed
to be: transport/bpm/bars/roster) against decoded REALITY (RMS/peak at three
points down the pipeline) and writes a single ``data/jam-state.json`` that the
chuck dashboard (chuck-works PR #2), the gmixer-watchdog, and the #2442 units
all READ. The whole value is intent-vs-reality side by side: every bug in the
two-day blind debug lived in the gap between them.

Supervised daemon — meant to run under systemd --user with Restart=always,
NOT the oneshot + 3-min timer that went dark twice this week (chat-post
swallowed errors into exit 0, so a dead collector looked alive). Here a fatal
error exits non-zero so the supervisor restarts it and the failure is visible.

The 3 taps localize WHICH layer is dead:
  tap "jack"   — ChucK:outport in JACK, pre-encode (the source the receiver
                 is actually producing). Captured with jack_rec.
  tap "gmixer" — the gmixer output level (Icecast_Monitor gmixer's `outlevel`
                 GStreamer element), read from its debug log. Dual-purpose:
                 same measurement the held gmixer-loudness pass needs.
  tap "mount"  — the public icecast mount, decoded off the wire.
A source-thin jam reads low at jack; a broken mix reads jack-OK/gmixer-low;
a mount-up-but-silent reads gmixer-OK/mount-low.

Each tap keeps a rolling ~60s ring so a legit musical trough (a pad releasing)
reads as a dip, not a stall.

SCHEMA: v1 (Wendy's ruling, #2464) — additive-only from here; existing keys
never change meaning. Contract the collector upholds: every rms/peak is RAW
LINEAR [0,1] (the reader owns 20·log10→dB + colour); freshness is published as
RAW unix timestamps only (the reader owns staleness-as-RED, so a frozen file
reads RED not last-known-good). Per-lane RMS is deliberately absent — all voices
sum at master before the single JACK tap, so there is no per-voice level to
measure; the 3 reality taps (jack/gmixer/mount) are the only real measurement
points. The only coupling to Claude's dashboard is this file's path + the schema.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque

HOME = os.path.expanduser("~")


def _env(name, default):
    return os.environ.get(name, default)


# ── Config (env-overridable; mounts are a LIST so riddim can reuse this). ──
SERVICES_DIR = _env("JAM_SERVICES_DIR", os.path.join(HOME, "milton-services"))
STATE_FILE = _env("STATE_FILE", os.path.join(SERVICES_DIR, "data", "jam-state.json"))
RECEIVER_LOG = _env("JAM_RECEIVER_LOG", os.path.join(SERVICES_DIR, "logs", "chuck_receiver.log"))
GMIXER_LOG = _env("JAM_GMIXER_LOG", "/tmp/gmixer_debug.log")
ICECAST_HOST = _env("JAM_ICECAST_HOST", "127.0.0.1:8080")
JACK_PORTS = _env("JAM_JACK_PORTS", "ChucK:outport 0,ChucK:outport 1").split(",")
# Mount paths to decode for the "mount" tap. CSV so riddim/other rigs reuse it.
DECODE_MOUNTS = [m for m in _env("JAM_DECODE_MOUNTS", "/jam.mp3").split(",") if m]
INTERVAL = float(_env("JAM_INTERVAL", "10"))
RING_SECONDS = float(_env("JAM_RING_SECONDS", "60"))
CAPTURE_SECS = float(_env("JAM_CAPTURE_SECS", "2"))
# Floor for the transport-liveness window. The real window is cycle-aware (a
# slow jam logs a phrase only once per multi-minute cycle), but never tighter
# than this so a fast jam still flips to "stopped" reasonably quickly.
PHRASE_FRESH_SECS = float(_env("JAM_PHRASE_FRESH_SECS", "30"))
BEATS_PER_BAR = int(_env("JAM_BEATS_PER_BAR", "4"))

_START_RE = re.compile(r"START: bpm ([\d.]+) tpb (\d+) bars (\d+) countin (\d+)")
_STOP_RE = re.compile(r"STOP: transport gen")
_PHRASE_RE = re.compile(r"Playing phrase from ([\w.-]+)")
# "Loaded phrase from <name> : <instrument> rev <N> <notes> notes" — carries the
# per-lane instrument+rev that a name-only roster can't show. This is what makes
# merged≠live (kick still loaded as `sine` after a deploy) and a stale rev
# visible per lane (Wendy's v1 ruling, #2464).
_LOADED_RE = re.compile(r"Loaded phrase from ([\w.-]+) : (\S+) rev (\d+)")
_CYCLE_RE = re.compile(r"Cycle complete, looping")
_GMIX_RE = re.compile(r"outlevel: RMS dB: (-?[\d.]+)")


def _db_to_amp(db):
    try:
        return round(10.0 ** (float(db) / 20.0), 6)
    except (TypeError, ValueError):
        return None


def _sox_rms_peak(wav):
    """RMS + peak amplitude (0..1) from a wav via `sox -n stat`."""
    try:
        out = subprocess.run(
            ["sox", wav, "-n", "stat"],
            capture_output=True, text=True, timeout=15,
        ).stderr
    except (subprocess.SubprocessError, OSError):
        return None, None
    rms = peak = None
    for line in out.splitlines():
        if "RMS" in line and "amplitude" in line:
            rms = _last_float(line)
        elif "Maximum amplitude" in line:
            peak = _last_float(line)
    return rms, peak


def _last_float(line):
    try:
        return round(float(line.split()[-1]), 6)
    except (ValueError, IndexError):
        return None


# ── INTENT: parse the receiver log (until #2442 writes a status json). ──
def read_intent():
    """What the jam is *supposed* to be, from the receiver's own log.

    bpm/tpb/bars come from the most recent START line. Roster is scoped to the
    LAST COMPLETE transport cycle (between the final two `Cycle complete`
    markers), NOT an all-time distinct over the whole log — that was the false
    "32/32 FULL" reading. Transport is considered running only if a phrase
    played within PHRASE_FRESH_SECS, so a stopped jam doesn't look live.
    """
    intent = {
        "transport_running": False, "bpm": None, "tpb": None, "bars": None,
        "roster": [], "roster_count": 0, "lanes": [], "source": "receiver_log",
        "log_mtime_unix": None, "last_phrase_age_secs": None,
        "cycle_secs": None,
    }
    try:
        intent["log_mtime_unix"] = int(os.path.getmtime(RECEIVER_LOG))
        with open(RECEIVER_LOG, "r", errors="replace") as fh:
            lines = fh.readlines()[-4000:]
    except OSError:
        return intent

    last_start_idx = None
    last_stop_idx = None
    for idx, line in enumerate(lines):
        if _START_RE.search(line):
            last_start_idx = idx
        if _STOP_RE.search(line):
            last_stop_idx = idx

    for line in reversed(lines):
        m = _START_RE.search(line)
        if m:
            intent["bpm"] = float(m.group(1))
            intent["tpb"] = int(m.group(2))
            intent["bars"] = int(m.group(3))
            break

    stopped = (
        last_stop_idx is not None
        and (last_start_idx is None or last_stop_idx > last_start_idx)
    )

    active_lines = lines[last_start_idx:] if last_start_idx is not None else lines

    # Roster = distinct agents in the current cycle when it has started, falling
    # back to the last complete cycle. After a receiver restart on a long piece
    # (for example 96 bars at 60 bpm), waiting for two `Cycle complete` markers
    # would make intent read empty for the whole first multi-minute cycle even
    # while audio is flowing.
    cycle_idxs = [i for i, ln in enumerate(active_lines) if _CYCLE_RE.search(ln)]
    if stopped:
        window = []
    elif cycle_idxs and any(_PHRASE_RE.search(ln) for ln in active_lines[cycle_idxs[-1]:]):
        window = active_lines[cycle_idxs[-1]:]
    elif len(cycle_idxs) >= 2:
        lo, hi = cycle_idxs[-2], cycle_idxs[-1]
        window = active_lines[lo:hi]
    else:
        window = active_lines[cycle_idxs[-1]:] if cycle_idxs else active_lines
    roster = []
    for ln in window:
        m = _PHRASE_RE.search(ln)
        if m and m.group(1) not in roster:
            roster.append(m.group(1))
    intent["roster"] = roster
    intent["roster_count"] = len(roster)

    # lanes: enrich each roster member with the instrument+rev it most recently
    # LOADED (the whole tail, not just this cycle — a load persists across cycles
    # until superseded). roster stays the membership truth; lanes adds detail in
    # 1:1 correspondence, additive over the name-list (Wendy v1, #2464).
    # NOTE: last_phrase_age_secs is null — the receiver log has no per-line
    # timestamps, so a per-lane age isn't derivable here; only the whole-log
    # mtime exists (intent.last_phrase_age_secs). It lights up if/when the
    # receiver stamps per-line times (#2442). We publish the key, not a guess.
    latest_load = {}
    for ln in lines:
        m = _LOADED_RE.search(ln)
        if m:
            latest_load[m.group(1)] = {"instrument": m.group(2), "rev": int(m.group(3))}
    intent["lanes"] = [
        {
            "name": name,
            "instrument": latest_load.get(name, {}).get("instrument"),
            "rev": latest_load.get(name, {}).get("rev"),
            "last_phrase_age_secs": None,
        }
        for name in roster
    ]

    age = time.time() - intent["log_mtime_unix"]
    intent["last_phrase_age_secs"] = round(age, 1)
    # A slow jam logs only once per multi-minute cycle, so freshness must scale
    # with the cycle, not a fixed wall-clock window (60bpm/96bars ~= 384s/cycle;
    # a 30s window wrongly read that live jam as "stopped"). Allow 1.5 cycles.
    window = PHRASE_FRESH_SECS
    if intent["bpm"] and intent["bars"]:
        intent["cycle_secs"] = round(intent["bars"] * BEATS_PER_BAR * 60.0 / intent["bpm"], 1)
        window = max(PHRASE_FRESH_SECS, intent["cycle_secs"] * 1.5)
    intent["transport_running"] = (not stopped) and age <= window and bool(roster)
    return intent


# ── REALITY: the three decode taps. ──
def tap_jack():
    """RMS/peak of the receiver's JACK output (pre-encode source)."""
    wav = "/tmp/jam_collector_jack.wav"
    cmd = ["jack_rec", "-f", wav, "-d", str(CAPTURE_SECS)] + [p.strip() for p in JACK_PORTS]
    try:
        subprocess.run(cmd, capture_output=True, timeout=CAPTURE_SECS + 8)
    except (subprocess.SubprocessError, OSError):
        return {"rms": None, "peak": None, "ok": False}
    rms, peak = _sox_rms_peak(wav)
    return {"rms": rms, "peak": peak, "ok": rms is not None}


def tap_gmixer():
    """gmixer output level from its debug log (`outlevel` element, dB→amp)."""
    out = {"rms": None, "peak": None, "ok": False, "log_mtime_unix": None}
    try:
        out["log_mtime_unix"] = int(os.path.getmtime(GMIXER_LOG))
        with open(GMIXER_LOG, "r", errors="replace") as fh:
            tail = fh.readlines()[-200:]
    except OSError:
        return out
    for line in reversed(tail):
        m = _GMIX_RE.search(line)
        if m:
            out["rms"] = _db_to_amp(m.group(1))
            out["ok"] = out["rms"] is not None
            break
    return out


def tap_mount(mount):
    """RMS/peak decoded off a live icecast mount."""
    wav = "/tmp/jam_collector_mount.wav"
    pipeline = (
        f"souphttpsrc location=http://{ICECAST_HOST}{mount} "
        "! icydemux ! mpegaudioparse ! avdec_mp3 ! audioconvert ! wavenc "
        f"! filesink location={wav}"
    )
    # Stop with the `timeout` binary (graceful SIGTERM) so gst finalizes the
    # wav header — Python's own timeout sends SIGKILL, leaving a truncated file
    # that sox can't read (the mount tap silently read null until this fix).
    cap = max(1, int(round(CAPTURE_SECS)))
    try:
        subprocess.run(
            ["timeout", "--signal=TERM", str(cap), "gst-launch-1.0", "-q"] + pipeline.split(),
            capture_output=True, timeout=cap + 8,
        )
    except (subprocess.SubprocessError, OSError):
        return {"mount": mount, "rms": None, "peak": None, "ok": False}
    rms, peak = _sox_rms_peak(wav)
    return {"mount": mount, "rms": rms, "peak": peak, "ok": rms is not None}


# ── Rolling ring so a musical trough reads as a dip, not a stall. ──
_rings = defaultdict(lambda: deque())


def _ring_push(key, rms):
    if rms is None:
        return None
    dq = _rings[key]
    now = time.time()
    dq.append((now, rms))
    while dq and now - dq[0][0] > RING_SECONDS:
        dq.popleft()
    vals = [v for _, v in dq]
    return {
        "now": rms,
        "min": round(min(vals), 6),
        "max": round(max(vals), 6),
        "avg": round(sum(vals) / len(vals), 6),
        "samples": len(vals),
    }


def collect_once():
    intent = read_intent()
    jack = tap_jack()
    gmix = tap_gmixer()
    mounts = [tap_mount(m) for m in DECODE_MOUNTS]

    taps = {
        "jack": {**jack, "ring": _ring_push("jack", jack["rms"])},
        "gmixer": {**gmix, "ring": _ring_push("gmixer", gmix["rms"])},
        "mount": [
            {**mt, "ring": _ring_push("mount:" + mt["mount"], mt["rms"])}
            for mt in mounts
        ],
    }
    return {
        "updated_unix": int(time.time()),
        "collector": {"pid": os.getpid(), "interval_secs": INTERVAL,
                      "ring_secs": RING_SECONDS},
        "intent": intent,
        "reality": taps,
        "schema_version": "1",
    }


def write_state(state):
    """Atomic publish so a reader never sees a half-written file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def main():
    once = "--once" in sys.argv
    while True:
        try:
            state = collect_once()
            write_state(state)
            if once:
                json.dump(state, sys.stdout, indent=2)
                sys.stdout.write("\n")
                return 0
        except Exception as exc:  # surface, don't swallow into exit 0
            sys.stderr.write(f"[jam_collector] cycle error: {exc!r}\n")
            sys.stderr.flush()
            if once:
                return 1
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
