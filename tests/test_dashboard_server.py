#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO / "dashboard" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("chuck_dashboard_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "jam-state.json"
        self.compositions_dir = Path(self.tmp.name) / "compositions"
        self.compositions_dir.mkdir()
        self.server = load_server()
        self.server.STATE_FILE = str(self.state_file)
        self.server.COMPOSITIONS_DIR = self.compositions_dir
        self.server.STALE_SECS = 30
        self.commands = []
        self.server.run_command = self.fake_run_command

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run_command(self, argv):
        self.commands.append(argv)

        class Result:
            returncode = 0
            stdout = "sent"
            stderr = ""

        return Result()

    def write_state(self, updated_unix, **extra):
        state = {
            "updated_unix": updated_unix,
            "schema_version": "0-provisional",
            "intent": {"transport_running": True, "bpm": 60, "bars": 96, "roster": ["claude"]},
            "reality": {"jack": {"rms": 0.02, "ok": True}, "gmixer": {"rms": 0.06, "ok": True}, "mount": []},
        }
        state.update(extra)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def test_read_state_fresh(self):
        self.write_state(time.time())
        state, age, stale = self.server.read_state()
        self.assertFalse(stale)
        self.assertLess(age, 5)
        self.assertEqual(state["schema_version"], "0-provisional")

    def test_read_state_missing_is_stale_red(self):
        state, age, stale = self.server.read_state()
        self.assertTrue(stale)
        self.assertIsNone(age)
        self.assertIn("_error", state)

    def test_read_state_old_clock_is_stale(self):
        self.write_state(time.time() - 60)
        _state, age, stale = self.server.read_state()
        self.assertTrue(stale)
        self.assertGreater(age, 30)

    def test_api_state_adds_staleness_envelope(self):
        self.write_state(time.time())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertIn("_age_secs", payload)
        self.assertFalse(payload["_stale"])
        self.assertEqual(payload["intent"]["roster"], ["claude"])

    def test_api_compositions_lists_manifest_summaries(self):
        (self.compositions_dir / "cm7.json").write_text(json.dumps({
            "name": "cm7",
            "title": "CM7",
            "transport": {"bpm": 60, "bars": 96},
            "voices": [],
        }), encoding="utf-8")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/compositions", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(payload["compositions"][0]["name"], "cm7")
        self.assertEqual(payload["compositions"][0]["transport"]["bpm"], 60)

    def test_post_transport_start_runs_chuck_send(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/transport/start",
                data=json.dumps({"bpm": 172, "bars": 8, "countin": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertTrue(payload["ok"])
        self.assertIn("chuck_send.py", self.commands[0][1])
        self.assertIn("--start", self.commands[0])
        self.assertIn("172.0", self.commands[0])

    def test_post_composition_recall_runs_player(self):
        (self.compositions_dir / "cm7.json").write_text(json.dumps({
            "name": "cm7",
            "transport": {"bpm": 60, "bars": 96},
            "voices": [{"agent": "a", "instrument": "sine", "notes": "60,0.2,0,480"}],
        }), encoding="utf-8")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/compositions/cm7/recall",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertTrue(payload["ok"])
        self.assertIn("play_composition.py", self.commands[0][1])
        self.assertEqual(Path(self.commands[0][2]).name, "cm7.json")

    def test_unsupported_stop_returns_501(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/transport/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            ctx.exception.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(ctx.exception.code, 501)

    def test_unknown_api_get_returns_json_404(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/nope", timeout=5)
            body = ctx.exception.read().decode("utf-8")
            ctype = ctx.exception.headers.get("Content-Type")
            ctx.exception.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body)["error"], "not found")


if __name__ == "__main__":
    unittest.main()
