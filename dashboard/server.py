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
  POST /api/gain/master
                    -> call scripts/chuck_send.py --master-gain
  POST /api/compositions/<name>/recall
                    -> call scripts/play_composition.py for that manifest

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
PORT = int(os.environ.get("PORT", "8092"))
TITLE = os.environ.get("TITLE", "chuck-works")
STALE_SECS = float(os.environ.get("STALE_SECS", "30"))
PYTHON = os.environ.get("PYTHON", sys.executable)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
    result = run_command([
        PYTHON,
        str(REPO_ROOT / "scripts" / "play_composition.py"),
        str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"play_composition exited {result.returncode}")
    return {"ok": True, "name": name, "stdout": result.stdout.strip()}


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
            self._send(200, json.dumps(envelope), "application/json")
            return
        if path in ("/api/compositions", "/api/compositions/"):
            try:
                self._send(200, json.dumps(list_compositions()), "application/json")
            except ValueError as exc:
                self._send(500, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        if path == "/" or path == "/index.html":
            try:
                with open(HERE / "index.html", "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, f"index.html missing: {exc}", "text/plain")
            return
        if path.startswith("/api/"):
            self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/transport/start":
                self._send(200, json.dumps(start_transport(read_json_body(self))), "application/json")
                return
            if path == "/api/transport/stop":
                self._send(200, json.dumps(stop_transport()), "application/json")
                return
            if path == "/api/gain/master":
                self._send(200, json.dumps(set_master_gain(read_json_body(self))), "application/json")
                return
            if path.startswith("/api/lanes/") and (path.endswith("/mute") or path.endswith("/gain")):
                self._send(501, json.dumps({"ok": False, "error": "receiver has no per-lane mute/gain OSC handlers yet"}), "application/json")
                return
            prefix = "/api/compositions/"
            suffix = "/recall"
            if path.startswith(prefix) and path.endswith(suffix):
                name = unquote(path[len(prefix):-len(suffix)])
                self._send(200, json.dumps(recall_composition(name)), "application/json")
                return
        except FileNotFoundError as exc:
            self._send(404, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        except ValueError as exc:
            self._send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        except RuntimeError as exc:
            self._send(500, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        if path.startswith("/api/"):
            self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json")
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
