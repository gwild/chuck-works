#!/usr/bin/env python3
"""chuck-works dashboard — standalone observability/debug web GUI.

A view over ONE state file (the collector's jam-state.json), NOT a second
collector. Reads ground truth that someone else writes; never derives signal
itself. stdlib-only (no flask/fastapi) so it runs anywhere chuck-works does.

GENERIC BY DESIGN (so riddim reuses this core): every instance-specific thing
is an env var — point STATE_FILE at riddim's state file, set PORT/TITLE, and
the same server+page serve a riddim dashboard with zero code change.

  STATE_FILE   path to the collector's JSON state    (default: ../data/jam-state.json)
  PORT         http port                              (default: 8090)
  TITLE        page title                             (default: "chuck-works")
  STALE_SECS   age (s) past which state is RED        (default: 30)

Run:  STATE_FILE=/home/gregory/milton-services/data/jam-state.json ./server.py

Endpoints:
  GET /              -> index.html (the page)
  GET /api/state     -> the raw state JSON + a server-computed {_age_secs,_stale}
                        envelope (staleness is first-class: a frozen last-good
                        state must read RED, never green — #2464 design rule).

The server is SCHEMA-AGNOSTIC: it serves whatever the collector wrote, plus
the staleness envelope. The renderer (index.html) binds to the agreed schema
(Wendy owns the final shape, #2464); update the renderer, not this server,
when the schema lands.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(HERE, os.pardir, "data", "jam-state.json")
)
PORT = int(os.environ.get("PORT", "8090"))
TITLE = os.environ.get("TITLE", "chuck-works")
STALE_SECS = float(os.environ.get("STALE_SECS", "30"))


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
        if self.path.split("?")[0] == "/api/state":
            state, age, stale = read_state()
            envelope = dict(state)
            envelope["_age_secs"] = round(age, 1) if age is not None else None
            envelope["_stale"] = stale
            self._send(200, json.dumps(envelope), "application/json")
            return
        if self.path == "/" or self.path.split("?")[0] == "/index.html":
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, f"index.html missing: {exc}", "text/plain")
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
