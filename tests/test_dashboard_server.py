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


def _reject_constant(_token):
    # Mirror a strict JSON parser (the browser's JSON.parse): NaN/Infinity tokens
    # are invalid and must never appear on the wire.
    raise AssertionError("non-finite JSON constant on the wire")


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "jam-state.json"
        self.compositions_dir = Path(self.tmp.name) / "compositions"
        self.compositions_dir.mkdir()
        self.presets_dir = Path(self.tmp.name) / "presets"
        self.presets_dir.mkdir()
        self.server = load_server()
        self.server.STATE_FILE = str(self.state_file)
        self.server.COMPOSITIONS_DIR = self.compositions_dir
        self.server.PRESETS_DIR = self.presets_dir
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
        self.assertIn("_ready", payload)
        self.assertIn(payload["_ready"]["level"], ("green", "yellow", "red"))

    def test_api_state_nan_is_emitted_as_null_valid_json(self):
        # The collector legitimately writes NaN for a metric with no valid
        # samples (jack.rms before audio flows). Bare NaN is NOT valid JSON and
        # breaks the browser's JSON.parse, blanking the GUI. /api/state must
        # always emit valid JSON, with non-finite floats as null.
        self.write_state(
            time.time(),
            reality={"jack": {"rms": float("nan"), "ok": True,
                              "ring": {"avg": float("inf"), "min": float("-inf"), "samples": 6}},
                     "mount": []},
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
                raw = resp.read().decode("utf-8")
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)
        payload = json.loads(raw, parse_constant=_reject_constant)  # strict parse must succeed
        self.assertIsNone(payload["reality"]["jack"]["rms"])
        self.assertIsNone(payload["reality"]["jack"]["ring"]["avg"])
        self.assertEqual(payload["reality"]["jack"]["ring"]["samples"], 6)

    # --- compute_ready: full-chain readiness (drives the GUI dot) ---

    def _ready(self, reality, stale=False, age=2.0):
        state = {"updated_unix": time.time(), "reality": reality}
        return self.server.compute_ready(state, age, stale)

    def test_ready_green_when_jack_and_mount_carry_audio(self):
        r = self._ready({"jack": {"rms": 0.02, "ok": True},
                         "mount": [{"mount": "/jam.mp3", "rms": 0.04, "ok": True}]})
        self.assertEqual(r["level"], "green")

    def test_ready_yellow_when_mount_silent(self):
        r = self._ready({"jack": {"rms": 0.02, "ok": True},
                         "mount": [{"mount": "/jam.mp3", "rms": 0.0000001, "ok": True}]})
        self.assertEqual(r["level"], "yellow")

    def test_ready_yellow_when_mount_absent(self):
        r = self._ready({"jack": {"rms": 0.02, "ok": True}, "mount": []})
        self.assertEqual(r["level"], "yellow")

    def test_ready_red_when_jack_nan_despite_ok_true(self):
        # The false-positive case: collector reports ok:true rms:NaN with no
        # real audio (jack_rec produced an empty wav). Must NOT read green.
        r = self._ready({"jack": {"rms": float("nan"), "ok": True},
                         "mount": [{"mount": "/jam.mp3", "rms": 0.04, "ok": True}]})
        self.assertEqual(r["level"], "red")

    def test_ready_red_when_stale(self):
        r = self._ready({"jack": {"rms": 0.02, "ok": True},
                         "mount": [{"mount": "/jam.mp3", "rms": 0.04, "ok": True}]}, stale=True)
        self.assertEqual(r["level"], "red")

    # --- /api/launch + /api/shutdown drive systemctl jam.target ---

    def _post(self, path, body=None, expect_status=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            data = b"{}" if body is None else json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=data,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if expect_status is not None:
                        self.assertEqual(resp.status, expect_status)
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if expect_status is not None:
                    self.assertEqual(e.code, expect_status)
                return json.loads(e.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_post_launch_starts_jam_target(self):
        payload = self._post("/api/launch")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.commands[0], ["systemctl", "--user", "start", "jam.target"])

    def test_post_shutdown_stops_jam_target(self):
        payload = self._post("/api/shutdown")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.commands[0], ["systemctl", "--user", "stop", "jam.target"])

    # --- parametric voice (#2449) ---

    _SYNTH = {"waveform": "saw", "gain": 0.8, "pan": 0.0,
              "adsr": {"a": 0.01, "d": 0.1, "s": 0.7, "r": 0.3}, "detune": 0.0}

    def test_post_voices_insert_runs_chuck_send(self):
        payload = self._post("/api/voices/insert", {"agent": "probe", "synth": self._SYNTH})
        self.assertTrue(payload["ok"])
        argv = self.commands[0]
        self.assertIn("--voice", argv)
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--waveform") + 1], "saw")
        self.assertEqual(argv[argv.index("--attack") + 1], "0.01")
        self.assertNotIn("--notes", argv)

    def test_post_voices_insert_with_notes_also_loads(self):
        payload = self._post("/api/voices/insert",
                             {"agent": "probe", "synth": self._SYNTH, "notes": "60,0.8,0,480"})
        self.assertTrue(payload["ok"])
        argv = self.commands[0]
        self.assertIn("--notes", argv)
        self.assertEqual(argv[argv.index("--notes") + 1], "60,0.8,0,480")

    def test_post_voices_insert_rejects_out_of_range(self):
        bad = dict(self._SYNTH, gain=1.5)
        payload = self._post("/api/voices/insert", {"agent": "probe", "synth": bad}, expect_status=400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.commands, [])

    def test_post_voices_insert_rejects_bad_agent(self):
        payload = self._post("/api/voices/insert", {"agent": "../etc", "synth": self._SYNTH}, expect_status=400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.commands, [])

    def test_preset_save_then_list_then_recall(self):
        save = self._post("/api/presets", {"name": "warm-saw", "title": "Warm Saw", "synth": self._SYNTH})
        self.assertTrue(save["ok"])
        self.assertTrue((self.presets_dir / "warm-saw.json").exists())
        # GET list
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/presets", timeout=5) as resp:
                listing = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(listing["presets"][0]["name"], "warm-saw")
        # recall onto an agent
        rec = self._post("/api/presets/warm-saw/recall", {"agent": "lead"})
        self.assertTrue(rec["ok"])
        argv = self.commands[0]
        self.assertIn("--voice", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "lead")
        self.assertEqual(argv[argv.index("--waveform") + 1], "saw")

    def test_preset_save_rejects_bad_name(self):
        payload = self._post("/api/presets", {"name": "../evil", "synth": self._SYNTH}, expect_status=400)
        self.assertFalse(payload["ok"])

    def test_preset_recall_missing_is_404(self):
        payload = self._post("/api/presets/nope/recall", {"agent": "lead"}, expect_status=404)
        self.assertFalse(payload["ok"])

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

    def test_api_compositions_accepts_trailing_slash(self):
        (self.compositions_dir / "cm7.json").write_text(json.dumps({
            "name": "cm7",
            "transport": {"bpm": 60, "bars": 96},
        }), encoding="utf-8")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/compositions/", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(payload["compositions"][0]["name"], "cm7")

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

    def test_post_transport_stop_runs_chuck_send(self):
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertTrue(payload["ok"])
        self.assertIn("chuck_send.py", self.commands[0][1])
        self.assertIn("--stop", self.commands[0])

    def test_post_master_gain_runs_chuck_send(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/gain/master",
                data=json.dumps({"gain": 0.42}).encode("utf-8"),
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
        self.assertEqual(payload["gain"], 0.42)
        self.assertIn("chuck_send.py", self.commands[0][1])
        self.assertIn("--master-gain", self.commands[0])
        self.assertIn("0.42", self.commands[0])

    def test_post_master_gain_rejects_out_of_range(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/gain/master",
                data=json.dumps({"gain": 1.5}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            body = ctx.exception.read().decode("utf-8")
            ctx.exception.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("between 0 and 1", json.loads(body)["error"])
        self.assertEqual(self.commands, [])

    def test_unsupported_lane_gain_returns_501(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/lanes/claude/gain",
                data=json.dumps({"gain": 0.5}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            body = ctx.exception.read().decode("utf-8")
            ctx.exception.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertEqual(ctx.exception.code, 501)
        self.assertIn("per-lane", json.loads(body)["error"])

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
