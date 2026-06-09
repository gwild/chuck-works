#!/usr/bin/env python3
"""Byte-layout + CLI-guard tests for chuck_send.py — the OSC sender.

The receiver (chuck_receiver.ck handleVoice) parses /voice positionally, so the
wire layout is a contract: address, type tag ,ssfffffff, then agent + waveform
strings and 7 floats. These tests decode the datagram and assert each field, so
a sender/receiver drift is caught here rather than as silent mis-synthesis.
"""
import importlib.util
import struct
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SEND_PATH = REPO / "scripts" / "chuck_send.py"


def load_send():
    spec = importlib.util.spec_from_file_location("chuck_send", SEND_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_osc_string(buf, off):
    """Read a null-terminated, 4-byte-padded OSC string; return (value, next_off)."""
    end = buf.index(b"\x00", off)
    s = buf[off:end].decode("utf-8")
    nxt = off + len(s) + 1
    nxt += (4 - nxt % 4) % 4
    return s, nxt


class VoiceMessageLayoutTest(unittest.TestCase):
    def setUp(self):
        self.cs = load_send()

    def test_voice_message_decodes_field_by_field(self):
        m = self.cs.build_voice_message("probe", "saw", 0.8, -0.25, 0.01, 0.1, 0.7, 0.3, 12.0)
        self.assertEqual(len(m) % 4, 0, "OSC datagram must be 4-byte aligned")
        addr, off = _read_osc_string(m, 0)
        self.assertEqual(addr, "/voice")
        tag, off = _read_osc_string(m, off)
        self.assertEqual(tag, ",ssfffffff")
        agent, off = _read_osc_string(m, off)
        self.assertEqual(agent, "probe")
        wave, off = _read_osc_string(m, off)
        self.assertEqual(wave, "saw")
        floats = struct.unpack(">fffffff", m[off:off + 28])
        gain, pan, atk, dec, sus, rel, det = floats
        self.assertAlmostEqual(gain, 0.8, places=5)
        self.assertAlmostEqual(pan, -0.25, places=5)
        self.assertAlmostEqual(atk, 0.01, places=5)
        self.assertAlmostEqual(dec, 0.1, places=5)
        self.assertAlmostEqual(sus, 0.7, places=5)
        self.assertAlmostEqual(rel, 0.3, places=5)
        self.assertAlmostEqual(det, 12.0, places=5)
        self.assertEqual(off + 28, len(m), "no trailing bytes after the 7 floats")

    def test_detune_defaults_to_zero(self):
        m = self.cs.build_voice_message("a", "sine", 0.5, 0.0, 0.0, 0.0, 1.0, 0.0)
        # last 4 bytes = detune float
        self.assertAlmostEqual(struct.unpack(">f", m[-4:])[0], 0.0, places=6)

    def test_waveforms_match_receiver_set(self):
        self.assertEqual(self.cs.VOICE_WAVEFORMS, ("sine", "saw", "tri", "square"))


class VoiceCliGuardTest(unittest.TestCase):
    """The CLI must reject out-of-range params (dual guard with the receiver)."""

    def _run(self, *extra):
        # --host 127.0.0.1 --port 1 so a successful path would send to a dead
        # local port (no external effect); we only assert exit codes on guards.
        return subprocess.run(
            [sys.executable, str(SEND_PATH), "--voice", "--agent", "probe",
             "--host", "127.0.0.1", "--port", "1", *extra],
            capture_output=True, text=True,
        )

    def test_rejects_gain_over_one(self):
        r = self._run("--voice-gain", "1.5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("voice-gain", r.stderr)

    def test_rejects_bad_waveform(self):
        r = self._run("--waveform", "noise")
        self.assertEqual(r.returncode, 1)
        self.assertIn("waveform", r.stderr)

    def test_rejects_attack_over_cap(self):
        r = self._run("--attack", "9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("attack", r.stderr)

    def test_voice_requires_agent(self):
        r = subprocess.run(
            [sys.executable, str(SEND_PATH), "--voice", "--host", "127.0.0.1", "--port", "1"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("--voice requires --agent", r.stderr)


if __name__ == "__main__":
    unittest.main()
