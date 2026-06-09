#!/usr/bin/env python3
"""Validation tests for play_composition.py — manifest contract incl. the
peak_target headroom contract (Jan's #17 review). The live --check-peak capture
is beelink-only (jack_rec+sox); here we test the manifest-validation half, which
is what gates a bad headroom target before anything plays."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PLAY_PATH = REPO / "scripts" / "play_composition.py"


def load_play():
    spec = importlib.util.spec_from_file_location("play_composition", PLAY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(**extra):
    m = {
        "manifest_version": 1,
        "name": "t",
        "transport": {"bpm": 120, "bars": 4},
        "voices": [{"agent": "a", "instrument": "sine", "notes": "60,0.8,0,480"}],
    }
    m.update(extra)
    return m


class PeakTargetValidationTest(unittest.TestCase):
    def setUp(self):
        self.play = load_play()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, manifest):
        p = Path(self.tmp.name) / "m.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        return self.play.load_manifest(p)

    def test_no_peak_target_is_allowed(self):
        data = self._load(_manifest())
        self.assertNotIn("peak_target", data)

    def test_valid_peak_target_passes(self):
        data = self._load(_manifest(peak_target={"max": 0.4, "min": 0.03}))
        self.assertEqual(data["peak_target"]["max"], 0.4)

    def test_peak_target_requires_max(self):
        with self.assertRaises(SystemExit):
            self._load(_manifest(peak_target={"min": 0.1}))

    def test_peak_target_max_must_be_in_range(self):
        with self.assertRaises(SystemExit):
            self._load(_manifest(peak_target={"max": 1.5}))

    def test_peak_target_min_must_be_below_max(self):
        with self.assertRaises(SystemExit):
            self._load(_manifest(peak_target={"max": 0.4, "min": 0.5}))


if __name__ == "__main__":
    unittest.main()
