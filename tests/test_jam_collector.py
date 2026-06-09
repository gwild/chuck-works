#!/usr/bin/env python3
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
COLLECTOR_PATH = REPO / "scripts" / "jam_collector.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("jam_collector", COLLECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JamCollectorIntentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "chuck_receiver.log"
        self.collector = load_collector()
        self.collector.RECEIVER_LOG = str(self.log)
        self.collector.RECEIVER_SOURCE = "file"

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        now = time.time()
        self.log.touch()
        return now

    def test_stop_after_start_clears_transport_running(self):
        self.write_log([
            "[chuck_receiver] START: bpm 172.000000 tpb 480 bars 8 countin 0",
            "[chuck_receiver] Playing phrase from locke",
            "[chuck_receiver] STOP: transport gen 2",
        ])

        intent = self.collector.read_intent()

        self.assertFalse(intent["transport_running"])
        self.assertEqual(intent["roster"], [])
        self.assertEqual(intent["bpm"], 172.0)
        self.assertEqual(intent["bars"], 8)

    def test_start_after_stop_restores_normal_running_detection(self):
        self.write_log([
            "[chuck_receiver] START: bpm 60.000000 tpb 480 bars 4 countin 0",
            "[chuck_receiver] Playing phrase from locke",
            "[chuck_receiver] STOP: transport gen 2",
            "[chuck_receiver] START: bpm 172.000000 tpb 480 bars 8 countin 0",
            "[chuck_receiver] Playing phrase from claude",
        ])

        intent = self.collector.read_intent()

        self.assertTrue(intent["transport_running"])
        self.assertEqual(intent["roster"], ["claude"])
        self.assertEqual(intent["bpm"], 172.0)
        self.assertEqual(intent["bars"], 8)

    def test_journal_source_reads_live_receiver_events(self):
        self.collector.RECEIVER_SOURCE = "journal"

        def fake_journal():
            return int(time.time()), [
                "[chuck_receiver] START: bpm 172.000000 tpb 480 bars 8 countin 0",
                "[chuck_receiver] Playing phrase from acid",
                "[chuck_receiver] Playing phrase from sub",
            ]

        self.collector._receiver_journal_lines = fake_journal

        intent = self.collector.read_intent()

        self.assertTrue(intent["transport_running"])
        self.assertEqual(intent["source"], "receiver_journal")
        self.assertEqual(intent["roster"], ["acid", "sub"])
        self.assertEqual(intent["bpm"], 172.0)
        self.assertEqual(intent["bars"], 8)

    def test_journal_source_failure_is_visible(self):
        self.collector.RECEIVER_SOURCE = "journal"

        def broken_journal():
            raise RuntimeError("journalctl failed for chuck-receiver.service: denied")

        self.collector._receiver_journal_lines = broken_journal

        with self.assertRaisesRegex(RuntimeError, "journalctl failed"):
            self.collector.read_intent()


if __name__ == "__main__":
    unittest.main()
