#!/usr/bin/env python3
"""chuck-works dashboard — standalone observability/debug web GUI.

A view over ONE state file (the collector's jam-state.json), NOT a second
collector. Reads ground truth that someone else writes; never derives signal
itself. stdlib-only (no flask/fastapi) so it runs anywhere chuck-works does.

GENERIC BY DESIGN (so riddim reuses this core): every instance-specific thing
is an env var — point STATE_FILE at riddim's state file, set PORT/TITLE, and
the same server+page serve a riddim dashboard with zero code change.

  STATE_FILE   path to the collector's JSON state    (default: ../data/jam-state.json)
  PORT         http port                              (default: 8092)
  TITLE        page title                             (default: "chuck-works")
  STALE_SECS   age (s) past which state is RED        (default: 30)

Run:  STATE_FILE=/home/gregory/milton-services/data/jam-state.json ./server.py

Endpoints:
  GET /              -> index.html (the page)
  GET /api/state     -> the raw state JSON + a server-computed {_age_secs,_stale}
                        envelope (staleness is first-class: a frozen last-good
                        state must read RED, never green — #2464 design rule).
  GET /api/compositions
                    -> saved composition summaries from compositions/*.json
  POST /api/transport/start
                    -> call scripts/chuck_send.py --start
  POST /api/transport/stop
                    -> call scripts/chuck_send.py --stop
  POST /api/launch  -> systemctl --user start jam.target (bring up the whole
                       audio chain: jackd -> ChucK -> GStreamer -> Icecast)
  POST /api/shutdown -> systemctl --user stop jam.target (tear the chain down)
  POST /api/gain/master
                    -> call scripts/chuck_send.py --master-gain
  POST /api/compositions/<name>/recall
                    -> call scripts/play_composition.py for that manifest
  POST /api/voices/insert
                    -> chuck_send.py --voice (parametric voice, #2449); optional
                       notes also loads a test phrase
  GET  /api/presets / POST /api/presets / POST /api/presets/<name>/recall
                    -> list / save (presets/<name>.json) / send a saved voice

Unsupported receiver controls return 501 until OSC support exists. The GUI must
never fake per-lane gain/mute success.

The server is SCHEMA-AGNOSTIC: it serves whatever the collector wrote, plus
the staleness envelope. The renderer (index.html) binds to the agreed schema
(Wendy owns the final shape, #2464); update the renderer, not this server,
when the schema lands.
"""
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
STATE_FILE = os.environ.get(
    "STATE_FILE", str(REPO_ROOT / "data" / "jam-state.json")
)
COMPOSITIONS_DIR = Path(os.environ.get("COMPOSITIONS_DIR", str(REPO_ROOT / "compositions")))
PRESETS_DIR = Path(os.environ.get("PRESETS_DIR", str(REPO_ROOT / "presets")))
PORT = int(os.environ.get("PORT", "8092"))
TITLE = os.environ.get("TITLE", "chuck-works")
STALE_SECS = float(os.environ.get("STALE_SECS", "30"))
PYTHON = os.environ.get("PYTHON", sys.executable)
# Linear-RMS floor below which a tap counts as silent (~ -60 dB, just under the
# renderer's -55 dB "dead" floor). Green readiness requires audio above this on
# both the ChucK/JACK tap and the /jam.mp3 mount, so a connected-but-silent
# stream reads YELLOW, never green.
JAM_SILENCE_RMS = float(os.environ.get("JAM_SILENCE_RMS", "0.001"))
JAM_MOUNT = os.environ.get("JAM_MOUNT", "/jam.mp3")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _finite(value):
    """Recursively replace non-finite floats (NaN, inf, -inf) with None.

    The collector legitimately emits NaN for a metric with no valid samples
    (e.g. jack.rms before audio flows). json.dumps' default allow_nan=True
    then serializes a bare `NaN`, which is NOT valid JSON: the browser's
    JSON.parse rejects the whole document and the GUI goes blank. allow_nan
    is therefore False below, but that RAISES on a non-finite float — and
    observability must never crash on bad input — so we sanitize first.
    null is the right wire value: "no reading", which the renderer already
    handles (gmixer.peak is already null when absent).
    """
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def dump_json(obj):
    """json.dumps that always produces valid JSON (no bare NaN/Infinity)."""
    return json.dumps(_finite(obj), allow_nan=False)


def run_command(argv):
    return subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True)


def read_state():
    """Return (state_dict, age_secs, stale_bool). Missing/corrupt file is a
    RED state, not an exception — observability must not crash on bad input."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError) as exc:
        return {"_error": f"no readable state file at {STATE_FILE}: {exc!r}"}, None, True
    # updated_unix is the staleness clock (collector stamps it each write).
    updated = state.get("updated_unix")
    if not isinstance(updated, (int, float)):
        return state, None, True
    age = time.time() - updated
    return state, age, age > STALE_SECS


def read_json_body(handler, *, max_bytes=4096):
    length = int(handler.headers.get("Content-Length") or "0")
    if length > max_bytes:
        raise ValueError(f"request body too large: {length} bytes")
    if length == 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def composition_path(name):
    if not NAME_RE.match(name):
        raise ValueError("composition name must use only letters, numbers, dot, underscore, or hyphen")
    path = COMPOSITIONS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"composition not found: {name}")
    return path


def load_composition_summary(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: unreadable manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: manifest must be an object")
    name = data.get("name") or path.stem
    title = data.get("title") or name
    transport = data.get("transport") or {}
    if not isinstance(transport, dict):
        raise ValueError(f"{path.name}: transport must be an object")
    return {"name": name, "title": title, "transport": transport}


def list_compositions():
    items = []
    for path in sorted(COMPOSITIONS_DIR.glob("*.json")):
        items.append(load_composition_summary(path))
    return {"compositions": items}


def start_transport(body):
    try:
        bpm = float(body["bpm"])
        bars = int(body["bars"])
        countin = int(body.get("countin", 0))
    except KeyError as exc:
        raise ValueError(f"missing required field: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("bpm must be numeric; bars/countin must be integers") from exc
    if bpm <= 0 or bars <= 0 or countin < 0:
        raise ValueError("bpm and bars must be > 0; countin must be >= 0")
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "chuck_send.py"),
        "--start",
        "--bpm", str(bpm),
        "--bars", str(bars),
        "--countin", str(countin),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "transport": {"bpm": bpm, "bars": bars, "countin": countin}, "stdout": result.stdout.strip()}


def stop_transport():
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "chuck_send.py"),
        "--stop",
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "stdout": result.stdout.strip()}


def clear_roster():
    """Empty the receiver roster (#2456) — wipe all loaded voices to silence so
    the next composition REPLACES rather than stacks."""
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "chuck_send.py"),
        "--clear",
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "stdout": result.stdout.strip()}


def set_master_gain(body):
    try:
        gain = float(body["gain"])
    except KeyError as exc:
        raise ValueError(f"missing required field: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("gain must be numeric") from exc
    if gain < 0.0 or gain > 1.0:
        raise ValueError("gain must be between 0 and 1")
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "chuck_send.py"),
        "--master-gain", str(gain),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "gain": gain, "stdout": result.stdout.strip()}


def recall_composition(name):
    path = composition_path(name)
    # Clear the roster FIRST so recalling a song REPLACES the current one rather
    # than stacking its voices on top (#2456 — different songs use different
    # agent names, which never overwrite each other's slots).
    clear_roster()
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "play_composition.py"),
        str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"play_composition exited {result.returncode}")
    return {"ok": True, "name": name, "stdout": result.stdout.strip()}


# ── Parametric voice (#2449) ────────────────────────────────────────
# Waveforms must match chuck_send.VOICE_WAVEFORMS / chuck_receiver.ck handleVoice.
VOICE_WAVEFORMS = ("sine", "saw", "tri", "square")


def validate_synth(s):
    """Validate a synth spec dict {waveform, gain, pan, adsr:{a,d,s,r}, detune?}.
    Raises ValueError (→ HTTP 400) on any defect. Same range contract as the
    sender CLI + receiver clamps; we reject rather than clamp so the GUI gets a
    clear error instead of a silently-altered voice."""
    if not isinstance(s, dict):
        raise ValueError("synth must be an object")
    if s.get("waveform") not in VOICE_WAVEFORMS:
        raise ValueError(f"synth.waveform must be one of {list(VOICE_WAVEFORMS)}")
    try:
        gain = float(s["gain"])
        pan = float(s["pan"])
        adsr = s["adsr"]
        a = float(adsr["a"])
        d = float(adsr["d"])
        sus = float(adsr["s"])
        r = float(adsr["r"])
        detune = float(s["detune"]) if "detune" in s else 0.0
    except KeyError as exc:
        raise ValueError(f"synth missing field: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(f"synth fields must be numeric: {exc}") from exc
    if not (0.0 <= gain <= 1.0):
        raise ValueError("synth.gain must be between 0 and 1")
    if not (-1.0 <= pan <= 1.0):
        raise ValueError("synth.pan must be between -1 and 1")
    for nm, val in (("attack", a), ("decay", d), ("release", r)):
        if not (0.0 <= val <= 5.0):
            raise ValueError(f"synth.adsr.{nm} must be between 0 and 5 seconds")
    if not (0.0 <= sus <= 1.0):
        raise ValueError("synth.adsr.s (sustain) must be between 0 and 1")
    if not (-1200.0 <= detune <= 1200.0):
        raise ValueError("synth.detune must be between -1200 and 1200 cents")
    return {"waveform": s["waveform"], "gain": gain, "pan": pan,
            "adsr": {"a": a, "d": d, "s": sus, "r": r}, "detune": detune}


def _voice_argv(agent, s):
    """chuck_send.py --voice argv for a validated synth spec onto `agent`."""
    a = s["adsr"]
    return [
        PYTHON, str(REPO_ROOT / "scripts" / "chuck_send.py"),
        "--voice", "--agent", agent,
        "--waveform", s["waveform"],
        "--voice-gain", str(s["gain"]), "--voice-pan", str(s["pan"]),
        "--attack", str(a["a"]), "--decay", str(a["d"]),
        "--sustain", str(a["s"]), "--release", str(a["r"]),
        "--detune", str(s["detune"]),
    ]


def insert_voice(body):
    """POST /api/voices/insert — configure an agent's parametric voice live, and
    optionally load a test phrase in the same call (notes)."""
    agent = body.get("agent")
    if not agent or not isinstance(agent, str) or not NAME_RE.match(agent):
        raise ValueError("agent must be a name of letters, numbers, dot, underscore, or hyphen")
    if "synth" not in body:
        raise ValueError("missing required field: synth")
    s = validate_synth(body["synth"])
    argv = _voice_argv(agent, s)
    if body.get("notes"):
        argv += ["--instrument", str(body.get("instrument") or s["waveform"]), "--notes", str(body["notes"])]
    result = run_command(argv)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "agent": agent, "synth": s, "stdout": result.stdout.strip()}


def preset_path(name):
    if not NAME_RE.match(name):
        raise ValueError("preset name must use only letters, numbers, dot, underscore, or hyphen")
    return PRESETS_DIR / f"{name}.json"


def load_preset(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: unreadable preset: {exc}") from exc
    if not isinstance(data, dict) or "synth" not in data:
        raise ValueError(f"{path.name}: preset must be an object with a synth")
    name = data.get("name") or path.stem
    title = data.get("title") or name
    return {"name": name, "title": title, "synth": validate_synth(data["synth"])}


def list_presets():
    items = []
    if PRESETS_DIR.exists():
        for path in sorted(PRESETS_DIR.glob("*.json")):
            items.append(load_preset(path))
    return {"presets": items}


def save_preset(body):
    """POST /api/presets — write presets/<name>.json (the server's only file
    write). Strict NAME_RE guards path traversal; written atomically via a temp
    file + os.replace so a crash mid-write never leaves a half preset."""
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("missing required field: name")
    path = preset_path(name)
    if "synth" not in body:
        raise ValueError("missing required field: synth")
    s = validate_synth(body["synth"])
    doc = {"preset_version": 1, "name": name, "title": body.get("title") or name, "synth": s}
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "name": name, "synth": s}


def recall_preset(name, body):
    """POST /api/presets/<name>/recall — send the saved voice onto an agent."""
    path = preset_path(name)
    if not path.exists():
        raise FileNotFoundError(f"preset not found: {name}")
    preset = load_preset(path)
    agent = body.get("agent")
    if not agent or not isinstance(agent, str) or not NAME_RE.match(agent):
        raise ValueError("agent must be a name of letters, numbers, dot, underscore, or hyphen")
    result = run_command(_voice_argv(agent, preset["synth"]))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"chuck_send exited {result.returncode}")
    return {"ok": True, "name": name, "agent": agent, "stdout": result.stdout.strip()}


def launch_chain():
    """Bring up the whole audio chain (jackd -> ChucK -> GStreamer -> Icecast)
    via systemd. jam.target orders jackd-dummy.service, chuck-receiver.service
    and gststream-cw.service; systemd owns supervision and restart."""
    result = run_command(["systemctl", "--user", "start", "jam.target"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"systemctl start exited {result.returncode}")
    return {"ok": True, "stdout": result.stdout.strip()}


def shutdown_chain():
    """Tear the whole audio chain down. PartOf= on the member units makes
    stopping jam.target cascade to all three."""
    result = run_command(["systemctl", "--user", "stop", "jam.target"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"systemctl stop exited {result.returncode}")
    return {"ok": True, "stdout": result.stdout.strip()}


def _signal_rms(node):
    """Linear RMS from a reality tap if it is a real, finite, positive number,
    else None. A tap can report ok:true with rms:NaN (jack_rec produced an
    empty wav) — that is NOT a live signal, so non-finite reads as 'no signal'."""
    if not isinstance(node, dict):
        return None
    rms = node.get("rms")
    if not isinstance(rms, (int, float)) or isinstance(rms, bool):
        return None
    if rms != rms or rms in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return float(rms)


def compute_ready(state, age_secs, stale):
    """Map collector state -> {"level": "green"|"yellow"|"red", "reasons": [...]}.

    Pure (no I/O) so it is unit-testable. Drives the GUI readiness dot. Green
    means the FULL chain is carrying real audio end-to-end; a connected-but-
    silent stream reads YELLOW, never green (the failure mode we keep hitting).
    """
    reasons = []
    if stale:
        reasons.append("state is stale (collector down or frozen)")
        return {"level": "red", "reasons": reasons}
    if not isinstance(state, dict) or state.get("_error"):
        reasons.append(state.get("_error") if isinstance(state, dict) else "no state")
        return {"level": "red", "reasons": reasons}

    reality = state.get("reality") or {}
    jack_rms = _signal_rms(reality.get("jack"))
    if jack_rms is None:
        reasons.append("ChucK/JACK has no live signal (jackd or receiver down)")
        return {"level": "red", "reasons": reasons}
    if jack_rms < JAM_SILENCE_RMS:
        reasons.append(f"ChucK is silent (jack rms {jack_rms:.5f} < {JAM_SILENCE_RMS})")

    mounts = reality.get("mount") or []
    jam = next((m for m in mounts if isinstance(m, dict) and m.get("mount") == JAM_MOUNT), None)
    mount_rms = _signal_rms(jam)
    if mount_rms is None:
        reasons.append(f"{JAM_MOUNT} not streaming (GStreamer/Icecast not carrying audio)")
    elif mount_rms < JAM_SILENCE_RMS:
        reasons.append(f"{JAM_MOUNT} is silent (mount rms {mount_rms:.5f} < {JAM_SILENCE_RMS})")

    if jack_rms >= JAM_SILENCE_RMS and mount_rms is not None and mount_rms >= JAM_SILENCE_RMS:
        return {"level": "green", "reasons": ["full chain carrying audio"]}
    return {"level": "yellow", "reasons": reasons or ["chain partially up"]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            state, age, stale = read_state()
            envelope = dict(state)
            envelope["_age_secs"] = round(age, 1) if age is not None else None
            envelope["_stale"] = stale
            envelope["_ready"] = compute_ready(state, age, stale)
            self._send(200, dump_json(envelope), "application/json")
            return
        if path in ("/api/compositions", "/api/compositions/"):
            try:
                self._send(200, dump_json(list_compositions()), "application/json")
            except ValueError as exc:
                self._send(500, dump_json({"ok": False, "error": str(exc)}), "application/json")
            return
        if path in ("/api/presets", "/api/presets/"):
            try:
                self._send(200, dump_json(list_presets()), "application/json")
            except ValueError as exc:
                self._send(500, dump_json({"ok": False, "error": str(exc)}), "application/json")
            return
        if path == "/" or path == "/index.html":
            try:
                with open(HERE / "index.html", "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, f"index.html missing: {exc}", "text/plain")
            return
        if path.startswith("/api/"):
            self._send(404, dump_json({"ok": False, "error": "not found"}), "application/json")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/transport/start":
                self._send(200, dump_json(start_transport(read_json_body(self))), "application/json")
                return
            if path == "/api/transport/stop":
                self._send(200, dump_json(stop_transport()), "application/json")
                return
            if path == "/api/clear":
                self._send(200, dump_json(clear_roster()), "application/json")
                return
            if path == "/api/launch":
                self._send(200, dump_json(launch_chain()), "application/json")
                return
            if path == "/api/shutdown":
                self._send(200, dump_json(shutdown_chain()), "application/json")
                return
            if path == "/api/gain/master":
                self._send(200, dump_json(set_master_gain(read_json_body(self))), "application/json")
                return
            if path == "/api/voices/insert":
                self._send(200, dump_json(insert_voice(read_json_body(self))), "application/json")
                return
            preset_prefix = "/api/presets/"
            preset_suffix = "/recall"
            if path.startswith(preset_prefix) and path.endswith(preset_suffix):
                name = unquote(path[len(preset_prefix):-len(preset_suffix)])
                self._send(200, dump_json(recall_preset(name, read_json_body(self))), "application/json")
                return
            if path == "/api/presets":
                self._send(200, dump_json(save_preset(read_json_body(self))), "application/json")
                return
            if path.startswith("/api/lanes/") and (path.endswith("/mute") or path.endswith("/gain")):
                self._send(501, dump_json({"ok": False, "error": "receiver has no per-lane mute/gain OSC handlers yet"}), "application/json")
                return
            prefix = "/api/compositions/"
            suffix = "/recall"
            if path.startswith(prefix) and path.endswith(suffix):
                name = unquote(path[len(prefix):-len(suffix)])
                self._send(200, dump_json(recall_composition(name)), "application/json")
                return
        except FileNotFoundError as exc:
            self._send(404, dump_json({"ok": False, "error": str(exc)}), "application/json")
            return
        except ValueError as exc:
            self._send(400, dump_json({"ok": False, "error": str(exc)}), "application/json")
            return
        except RuntimeError as exc:
            self._send(500, dump_json({"ok": False, "error": str(exc)}), "application/json")
            return
        if path.startswith("/api/"):
            self._send(404, dump_json({"ok": False, "error": "not found"}), "application/json")
            return
        self._send(404, "not found", "text/plain")

    def log_message(self, *_args):
        pass  # quiet; this is an observability tool, not a noise source


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[chuck-dashboard] {TITLE} on :{PORT} reading {STATE_FILE} "
          f"(stale>{STALE_SECS}s)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
