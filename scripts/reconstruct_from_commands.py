#!/usr/bin/env python3
"""reconstruct_from_commands.py — rebuild a composition manifest from the
chuck_send.py command lines posted during a live jam (#chuck-commands, #2496).

A live jam IS a sequence of `chuck_send.py --agent … --notes …` invocations
posted to #chuck-commands. This tool reads that log (a file, or stdin), parses
every chuck_send invocation, and reconstructs the composition the receiver would
have ended up holding:

  - per AGENT, keep the phrase with the HIGHEST --revision seen (the receiver's
    rev-guard: a higher rev supersedes, a lower one is ignored — so the last
    *winning* phrase per agent is the live state, NOT merely the last posted);
  - take the MOST RECENT --start for transport bpm/bars/countin;
  - carry the last --pan and the full --voice synth spec per agent if present.

Output is a compositions/<name>.json manifest replayable via play_composition.py
(Recall on the dashboard) — the jam authors itself, no re-performance.

LIMITS (Jan #2496): chat text alone is not exact replay. Only phrases sent AS
chuck_send command lines are captured; live OSC sent another way is invisible.
Pick the session boundary with --since/--until or by feeding only the relevant
log slice. Render fidelity also depends on the receiver build (instrument
definitions) — the manifest pins notes + instrument NAME, not the timbre code.

Input line format: anything containing a `chuck_send.py` invocation; flags are
parsed positionally-agnostic (… --agent claude --instrument sine --notes "…").
Chat prefixes/markdown around the command are ignored.

Fail-loud per RULES.md: a note string that doesn't parse aborts; an empty
result (no commands found) is an error, not an empty manifest.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chuck_send import VOICE_WAVEFORMS, parse_notes  # noqa: E402

_CMD_RE = re.compile(r"chuck_send\.py\b(.*)$")


def _die(msg: str) -> None:
    print(f"reconstruct: {msg}", file=sys.stderr)
    sys.exit(1)


def _extract_args(line: str):
    """Return the token list AFTER 'chuck_send.py' on a line, or None if the
    line has no chuck_send invocation. Uses shlex so quoted --notes survive."""
    m = _CMD_RE.search(line)
    if not m:
        return None
    tail = m.group(1).strip()
    if not tail:
        return []
    try:
        return shlex.split(tail)
    except ValueError:
        # Unbalanced quotes (chat truncation) — skip this line, don't crash.
        return None


def _flag_map(tokens):
    """Parse `--flag value` / bare `--flag` tokens into a dict. Bare flags
    (--start/--stop/--voice) map to True."""
    out = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                out[key] = tokens[i + 1]
                i += 2
            else:
                out[key] = True
                i += 1
        else:
            i += 1
    return out


def reconstruct(lines, name):
    """Build a manifest dict from an iterable of log lines. Pure (no I/O)."""
    transport = None              # latest --start
    voices: dict[str, dict] = {}  # agent -> winning phrase dict
    for line in lines:
        tokens = _extract_args(line)
        if tokens is None:
            continue
        f = _flag_map(tokens)
        if f.get("start"):
            transport = {
                "bpm": float(f["bpm"]) if "bpm" in f else 120.0,
                "bars": int(f["bars"]) if "bars" in f else 8,
                "countin": int(f["countin"]) if "countin" in f else 0,
            }
            continue
        if f.get("stop") or "master-gain" in f:
            continue  # global controls aren't part of a voice's phrase
        agent = f.get("agent")
        notes = f.get("notes")
        if not agent or not notes or notes is True:
            # /pan-only or /voice-only line: fold into an existing voice if any.
            if agent and agent in voices:
                if "pan" in f:
                    voices[agent]["pan"] = float(f["pan"])
            continue
        rev = int(f["revision"]) if "revision" in f else 1
        prev = voices.get(agent)
        if prev is not None and rev <= prev["revision"]:
            continue  # rev-guard: a lower/equal revision does not supersede
        try:
            parse_notes(notes)
        except Exception as e:
            _die(f"agent {agent!r} rev {rev}: notes failed to parse: {e}")
        voice = {
            "agent": agent,
            "instrument": f.get("instrument") if isinstance(f.get("instrument"), str) else "sine",
            "revision": rev,
            "notes": notes,
        }
        if "pan" in f and f["pan"] is not True:
            voice["pan"] = float(f["pan"])
        if f.get("voice") and isinstance(f.get("waveform"), str) and f["waveform"] in VOICE_WAVEFORMS:
            voice["synth"] = {
                "waveform": f["waveform"],
                "gain": float(f["voice-gain"]) if "voice-gain" in f else 0.8,
                "pan": float(f["voice-pan"]) if "voice-pan" in f else 0.0,
                "adsr": {
                    "a": float(f["attack"]) if "attack" in f else 0.01,
                    "d": float(f["decay"]) if "decay" in f else 0.1,
                    "s": float(f["sustain"]) if "sustain" in f else 0.7,
                    "r": float(f["release"]) if "release" in f else 0.3,
                },
                "detune": float(f["detune"]) if "detune" in f else 0.0,
            }
        voices[agent] = voice
    if not voices:
        _die("no chuck_send phrases found in input — nothing to reconstruct")
    if transport is None:
        _die("no --start found in input — cannot determine bpm/bars; "
             "include the transport command or the log slice that has it")
    return {
        "manifest_version": 1,
        "name": name,
        "title": f"{name} (reconstructed from #chuck-commands)",
        "reconstructed": True,
        "transport": transport,
        "voices": [voices[a] for a in sorted(voices)],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconstruct a composition manifest from chuck_send command lines.")
    ap.add_argument("logfile", nargs="?", help="file of chuck_send command lines (default: stdin)")
    ap.add_argument("--name", required=True, help="composition name (file stem); [A-Za-z0-9._-]")
    ap.add_argument("--out", help="write manifest to this path (default: stdout)")
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.name):
        _die("--name must use only letters, numbers, dot, underscore, or hyphen")
    if args.logfile:
        text = Path(args.logfile).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    manifest = reconstruct(text.splitlines(), args.name)
    out_json = json.dumps(manifest, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(out_json, encoding="utf-8")
        print(f"reconstructed '{args.name}': {len(manifest['voices'])} voices "
              f"@ {manifest['transport']['bpm']} bpm -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out_json)


if __name__ == "__main__":
    main()
