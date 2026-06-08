#!/usr/bin/env python3
"""play_composition.py — play a saved ChucK composition through the receiver.

The jukebox foundation for #2449. A composition lives as a JSON MANIFEST
(see compositions/*.json) describing the transport plus every voice's
phrase — the machine-playable counterpart to the prose score in the
matching .md. This script reads a manifest and fires the exact OSC the
agents fired by hand during the live jams: one /start, then a /load and
optional /pan per voice. That makes a composition replayable from a file
instead of from a chat performance.

Manifest shape (validated below; v1):

    {
      "manifest_version": 1,
      "name": "cm7-drift",                 # matches compositions/<name>.md
      "title": "CM7 Drift - ambient ...",  # human label
      "transport": {"bpm": 60, "bars": 96, "countin": 0},
      "voices": [
        {"agent": "cm7-mid", "instrument": "pad", "revision": 1,
         "pan": -0.6,                       # optional; omit for pitch-curve default
         "notes": "52,0.35,0,7680;59,0.35,7680,7680"}  # chuck_send --notes form
      ]
    }

`countin` and per-voice `revision`/`pan` are OPTIONAL manifest fields with
documented behaviour when absent (countin 0; revision 1; pan = receiver's
pitch-curve default) — these are score semantics, not config values.

Design note (#2449 + #2442): this manifest IS a saved receiver state -
transport + per-agent {instrument, revision, notes, pan}. When #2442's
persistence lands, its snapshot and this manifest should converge on one
schema; the field names here mirror the receiver's /load + /pan + /start
inputs deliberately so that convergence is a rename, not a rewrite.

Fail-loud per RULES.md: a malformed manifest, an unknown field, or a
note string that doesn't parse aborts before sending anything - we never
half-play a composition.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Reuse the receiver's wire protocol + config (single source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chuck_send import (  # noqa: E402
    BEELINK_IP,
    OSC_PORT,
    TICKS_PER_BEAT,
    build_load_message,
    build_pan_message,
    build_start_message,
    parse_notes,
    send_osc,
)

_REQUIRED_TOP = ("name", "transport", "voices")
_REQUIRED_VOICE = ("agent", "instrument", "notes")
_ALLOWED_VOICE = {"agent", "instrument", "notes", "revision", "pan"}


def _die(msg: str) -> None:
    print(f"play_composition: {msg}", file=sys.stderr)
    sys.exit(1)


def load_manifest(path: Path) -> dict:
    """Read + validate a composition manifest. Fail loud on any defect."""
    if not path.exists():
        _die(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"manifest is not valid JSON: {e}")
    if not isinstance(data, dict):
        _die("manifest must be a JSON object")
    for k in _REQUIRED_TOP:
        if k not in data:
            _die(f"manifest missing required key: {k!r}")
    t = data["transport"]
    if not isinstance(t, dict) or "bpm" not in t or "bars" not in t:
        _die("transport must be an object with at least bpm + bars")
    voices = data["voices"]
    if not isinstance(voices, list) or not voices:
        _die("voices must be a non-empty list")
    seen = set()
    for i, v in enumerate(voices):
        if not isinstance(v, dict):
            _die(f"voice[{i}] must be an object")
        label = v["agent"] if "agent" in v else "?"
        for k in _REQUIRED_VOICE:
            if k not in v:
                _die(f"voice[{i}] ({label}) missing key: {k!r}")
        unknown = set(v) - _ALLOWED_VOICE
        if unknown:
            _die(f"voice[{i}] ({v['agent']}) has unknown keys: {sorted(unknown)}")
        if v["agent"] in seen:
            # One slot per agent (receiver constraint #1) - a dup would make
            # the second /load silently supersede the first.
            _die(f"duplicate agent in manifest: {v['agent']!r}")
        seen.add(v["agent"])
        # Parse-check the notes now so a bad string fails before any send.
        try:
            parse_notes(v["notes"])
        except Exception as e:  # parse_notes raises on malformed note tuples
            _die(f"voice[{i}] ({v['agent']}) notes failed to parse: {e}")
        if "pan" in v and not (-1.0 <= float(v["pan"]) <= 1.0):
            _die(f"voice[{i}] ({v['agent']}) pan out of range [-1,1]: {v['pan']}")
    return data


def play(manifest: dict, host: str, port: int, settle: float) -> None:
    """Fire /start, then /pan+/load per voice. Mirrors the live jam order:
    transport first, then each voice seats at the next cycle boundary."""
    t = manifest["transport"]
    bpm = float(t["bpm"])
    bars = int(t["bars"])
    countin = int(t["countin"]) if "countin" in t else 0
    send_osc(build_start_message(bpm, TICKS_PER_BEAT, bars, countin), host, port)
    print(f"/start bpm={bpm} bars={bars} countin={countin}")
    for v in manifest["voices"]:
        agent = v["agent"]
        if "pan" in v:
            send_osc(build_pan_message(agent, float(v["pan"])), host, port)
            print(f"/pan {agent} {v['pan']}")
        notes = parse_notes(v["notes"])
        rev = int(v["revision"]) if "revision" in v else 1
        send_osc(build_load_message(agent, v["instrument"], rev, notes), host, port)
        print(f"/load {agent} {v['instrument']} rev={rev} notes={len(notes)}")
        # Tiny inter-send spacing so a burst doesn't outrun the receiver's
        # OSC intake (the ~28-message ceiling under investigation, #2439).
        if settle > 0:
            time.sleep(settle)
    print(f"played '{manifest['name']}': {len(manifest['voices'])} voices")


def main() -> None:
    ap = argparse.ArgumentParser(description="Play a ChucK composition manifest.")
    ap.add_argument("manifest", help="path to compositions/<name>.json")
    ap.add_argument("--host", default=BEELINK_IP)
    ap.add_argument("--port", type=int, default=OSC_PORT)
    ap.add_argument("--settle", type=float, default=0.05,
                    help="seconds between per-voice sends (intake pacing)")
    ap.add_argument("--validate-only", action="store_true",
                    help="load + validate the manifest, send nothing")
    args = ap.parse_args()
    manifest = load_manifest(Path(args.manifest))
    if args.validate_only:
        print(f"OK: '{manifest['name']}' valid - "
              f"{len(manifest['voices'])} voices, "
              f"{manifest['transport']['bpm']} bpm / {manifest['transport']['bars']} bars")
        return
    play(manifest, args.host, args.port, args.settle)


if __name__ == "__main__":
    main()
