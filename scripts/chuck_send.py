#!/usr/bin/env python3
"""chuck_send.py — Send note phrases to beelink ChucK receiver via OSC.

Usage:
    python3 chuck_send.py --agent claude --instrument sine --notes "60,0.8,0,480;64,0.8,480,480;67,0.8,960,480"
    python3 chuck_send.py --start --bpm 120 --bars 8
    python3 chuck_send.py --stop
    python3 chuck_send.py --master-gain 0.85

Notes format: pitch,velocity,start_tick,dur_ticks separated by semicolons.
480 ticks = 1 beat at any BPM.
"""

import argparse
import socket
import struct
import sys
import os

import yaml

# ── Config (all from this repo's config.yaml, no fallbacks) ─────────
# Fail-loud on purpose, same pattern as chuck_relay.py (chuck-works #1).
# The old form swallowed ANY load failure into a hardcoded IP/port — after
# the #2450 migration there is no config.yaml at the resolved path unless
# this repo owns it, so every invocation would silently run on constants
# that merely happened to match reality. "Sent" prints whether or not the
# datagram goes anywhere useful (engine constraint 7), so a wrong default
# here is undetectable from the sender. A missing/broken config must STOP
# the send, not improvise one.
_cfg_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
try:
    with open(_cfg_file, encoding='utf-8') as _f:
        _cfg = yaml.safe_load(_f)
    BEELINK_IP = str(_cfg["ssh_access"]["hosts"]["beelink"]["ip"])
    OSC_PORT = int(_cfg["timers"]["chuck_relay"]["osc_port"])
except (OSError, KeyError, TypeError, yaml.YAMLError) as _e:
    sys.exit(f"FATAL chuck_send: cannot load receiver host/port from {_cfg_file}: "
             f"{_e!r} — fix the config; there are no fallback defaults.")
TICKS_PER_BEAT = 480


def osc_string(s):
    """Encode string for OSC (null-terminated, padded to 4-byte boundary)."""
    encoded = s.encode("utf-8") + b"\x00"
    padding = (4 - len(encoded) % 4) % 4
    return encoded + b"\x00" * padding


def osc_int(i):
    return struct.pack(">i", i)


def osc_float(f):
    return struct.pack(">f", f)


def build_load_message(agent, instrument, revision, notes):
    """Build OSC /load message."""
    address = osc_string("/load")

    # Type tag: s s i i (agent, instrument, revision, num_notes) + per note: i f i i
    num_notes = len(notes)
    type_tag = ",ssii" + "ifii" * num_notes
    type_tag_enc = osc_string(type_tag)

    data = osc_string(agent) + osc_string(instrument)
    data += osc_int(revision) + osc_int(num_notes)

    for note in notes:
        data += osc_int(note["pitch"])
        data += osc_float(note["velocity"])
        data += osc_int(note["start_tick"])
        data += osc_int(note["dur_ticks"])

    return address + type_tag_enc + data


def build_start_message(bpm, ticks_per_beat, bars, countin_ticks):
    """Build OSC /start message."""
    address = osc_string("/start")
    type_tag = osc_string(",fiii")
    data = osc_float(bpm) + osc_int(ticks_per_beat) + osc_int(bars) + osc_int(countin_ticks)
    return address + type_tag + data


def build_stop_message():
    """Build OSC /stop message."""
    return osc_string("/stop") + osc_string(",")


def build_clear_message():
    """Build OSC /clear message — empty the receiver roster (#2456) so the next
    composition REPLACES rather than stacks on the current one."""
    return osc_string("/clear") + osc_string(",")


def build_master_gain_message(gain):
    """Build OSC /master_gain message."""
    return osc_string("/master_gain") + osc_string(",f") + osc_float(gain)


def parse_notes(notes_str):
    """Parse 'pitch,vel,start,dur;pitch,vel,start,dur;...' into list of dicts."""
    notes = []
    for part in notes_str.split(";"):
        fields = part.strip().split(",")
        if len(fields) != 4:
            print(f"Skipping malformed note: {part}", file=sys.stderr)
            continue
        notes.append({
            "pitch": int(fields[0]),
            "velocity": float(fields[1]),
            "start_tick": int(fields[2]),
            "dur_ticks": int(fields[3]),
        })
    return notes


def send_osc(message, host=BEELINK_IP, port=OSC_PORT):
    """Send raw OSC message via UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(message, (host, port))
    sock.close()


def build_pan_message(agent, pos):
    """Build /pan agent(s) pos(f) — per-agent stereo override, -1..+1.
    The receiver clamps; agents never /pan'd keep the pitch-formula
    default (which only spans ±0.3 — /pan is how you reach the sides)."""
    return osc_string("/pan") + osc_string(",sf") + osc_string(agent) + struct.pack(">f", float(pos))


# Waveforms the parametric voice supports — must match chuck_receiver.ck handleVoice.
VOICE_WAVEFORMS = ("sine", "saw", "tri", "square")


def build_voice_message(agent, waveform, gain, pan, attack, decay, sustain, release, detune=0.0):
    """Build /voice — per-agent PARAMETRIC voice spec (the modular signal-insert
    template, #2449). Mirrors build_pan_message but carries a whole oscillator+
    envelope spec the receiver builds the chain from, instead of a hardcoded
    instrument preset. Fixed-width tag (detune always sent, default 0.0) so the
    receiver parses positionally with no optional-arg logic. The receiver clamps
    every field; an agent never /voice'd keeps its instrument-name behavior."""
    data = osc_string(agent) + osc_string(waveform)
    data += osc_float(gain) + osc_float(pan)
    data += osc_float(attack) + osc_float(decay) + osc_float(sustain) + osc_float(release)
    data += osc_float(detune)
    return osc_string("/voice") + osc_string(",ssfffffff") + data


def main():
    parser = argparse.ArgumentParser(description="Send ChucK OSC messages to beelink")
    parser.add_argument("--agent", help="Agent name (e.g. claude)")
    parser.add_argument("--instrument", default="sine", help="Instrument name")
    parser.add_argument("--revision", type=int, default=1, help="Phrase revision")
    parser.add_argument("--notes", help="Notes: pitch,vel,start_tick,dur_ticks;...")
    parser.add_argument("--start", action="store_true", help="Send /start message")
    parser.add_argument("--stop", action="store_true", help="Send /stop message")
    parser.add_argument("--clear", action="store_true",
                        help="Send /clear: empty the receiver roster so the next song replaces it")
    parser.add_argument("--bpm", type=float, default=120.0, help="BPM for /start")
    parser.add_argument("--bars", type=int, default=8, help="Number of bars")
    parser.add_argument("--countin", type=int, default=0, help="Count-in ticks")
    parser.add_argument("--host", default=BEELINK_IP, help="Beelink IP")
    parser.add_argument("--port", type=int, default=OSC_PORT, help="OSC port")
    parser.add_argument("--pan", type=float, default=None,
                        help="Send /pan for --agent: stereo position -1..+1 "
                             "(hand-rolled OSC was the only path before)")
    parser.add_argument("--master-gain", type=float, default=None,
                        help="Send /master_gain: shared output gain 0..1")
    parser.add_argument("--voice", action="store_true",
                        help="Send /voice for --agent: parametric oscillator voice "
                             "(waveform + gain/pan + ADSR + detune). Pairs with --notes.")
    parser.add_argument("--waveform", default="sine", help="/voice waveform: " + "|".join(VOICE_WAVEFORMS))
    parser.add_argument("--voice-gain", type=float, default=0.8, help="/voice gain 0..1")
    parser.add_argument("--voice-pan", type=float, default=0.0, help="/voice pan -1..+1")
    parser.add_argument("--attack", type=float, default=0.01, help="/voice ADSR attack seconds (>=0, <=5)")
    parser.add_argument("--decay", type=float, default=0.1, help="/voice ADSR decay seconds (>=0, <=5)")
    parser.add_argument("--sustain", type=float, default=0.7, help="/voice ADSR sustain level 0..1")
    parser.add_argument("--release", type=float, default=0.3, help="/voice ADSR release seconds (>=0, <=5)")
    parser.add_argument("--detune", type=float, default=0.0, help="/voice detune cents (-1200..+1200)")
    args = parser.parse_args()

    global_commands = int(args.start) + int(args.stop) + int(args.clear) + int(args.master_gain is not None)
    if global_commands > 1:
        print("--start, --stop, --clear, and --master-gain are global controls; send one per invocation", file=sys.stderr)
        sys.exit(1)
    if global_commands and (args.pan is not None or args.notes or args.voice):
        print("global controls cannot combine with --pan, --voice, or --notes; send them separately", file=sys.stderr)
        sys.exit(1)

    if args.voice:
        if not args.agent:
            print("--voice requires --agent", file=sys.stderr)
            sys.exit(1)
        if args.waveform not in VOICE_WAVEFORMS:
            print(f"--waveform must be one of {VOICE_WAVEFORMS}", file=sys.stderr)
            sys.exit(1)
        if not (0.0 <= args.voice_gain <= 1.0):
            print("--voice-gain must be between 0 and 1", file=sys.stderr)
            sys.exit(1)
        if not (-1.0 <= args.voice_pan <= 1.0):
            print("--voice-pan must be between -1 and 1", file=sys.stderr)
            sys.exit(1)
        for _name, _v in (("--attack", args.attack), ("--decay", args.decay), ("--release", args.release)):
            if not (0.0 <= _v <= 5.0):
                print(f"{_name} must be between 0 and 5 seconds", file=sys.stderr)
                sys.exit(1)
        if not (0.0 <= args.sustain <= 1.0):
            print("--sustain must be between 0 and 1", file=sys.stderr)
            sys.exit(1)
        if not (-1200.0 <= args.detune <= 1200.0):
            print("--detune must be between -1200 and 1200 cents", file=sys.stderr)
            sys.exit(1)
        send_osc(build_voice_message(args.agent, args.waveform, args.voice_gain, args.voice_pan,
                                     args.attack, args.decay, args.sustain, args.release, args.detune),
                 args.host, args.port)
        print(f"Sent /voice: agent={args.agent} wave={args.waveform} gain={args.voice_gain} "
              f"pan={args.voice_pan} adsr={args.attack},{args.decay},{args.sustain},{args.release} "
              f"detune={args.detune}")
        # Deliberately NO return: --voice combined with --notes sends BOTH
        # (/voice then /load), same no-silent-drop contract as --pan below.

    if args.pan is not None:
        if not args.agent:
            print("--pan requires --agent", file=sys.stderr)
            sys.exit(1)
        send_osc(build_pan_message(args.agent, args.pan), args.host, args.port)
        print(f"Sent /pan: agent={args.agent} pos={args.pan}")
        # Deliberately NO return: --pan combined with --notes sends BOTH
        # (/pan then /load) — the early-return here silently discarded the
        # /load, a silent drop in the very PR documenting silent drops
        # (Robby's #2444 review).

    if args.start:
        msg = build_start_message(args.bpm, TICKS_PER_BEAT, args.bars, args.countin)
        send_osc(msg, args.host, args.port)
        print(f"Sent /start: bpm={args.bpm} bars={args.bars} countin={args.countin}")
    elif args.stop:
        send_osc(build_stop_message(), args.host, args.port)
        print("Sent /stop")
    elif args.clear:
        send_osc(build_clear_message(), args.host, args.port)
        print("Sent /clear")
    elif args.master_gain is not None:
        if args.master_gain < 0.0 or args.master_gain > 1.0:
            print("--master-gain must be between 0 and 1", file=sys.stderr)
            sys.exit(1)
        send_osc(build_master_gain_message(args.master_gain), args.host, args.port)
        print(f"Sent /master_gain: gain={args.master_gain}")
    elif args.agent and args.notes:
        notes = parse_notes(args.notes)
        msg = build_load_message(args.agent, args.instrument, args.revision, notes)
        send_osc(msg, args.host, args.port)
        print(f"Sent /load: agent={args.agent} instrument={args.instrument} rev={args.revision} notes={len(notes)}")
    elif args.pan is not None:
        pass  # --pan alone already sent above
    elif args.voice:
        pass  # --voice alone already sent above
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
