#!/usr/bin/env python3
"""Tests for reconstruct_from_commands.py — rebuilding a composition from the
chuck_send command lines of a live jam (#2496). The core contract is the
rev-guard (highest revision per agent wins, mirroring the receiver) and the
transport pickup."""
import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RECON_PATH = REPO / "scripts" / "reconstruct_from_commands.py"


def load_recon():
    spec = importlib.util.spec_from_file_location("reconstruct_from_commands", RECON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReconstructTest(unittest.TestCase):
    def setUp(self):
        self.r = load_recon()

    def _build(self, lines, name="t"):
        return self.r.reconstruct(lines, name)

    def test_rev_guard_keeps_highest_revision(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 95 --bars 8",
            'scripts/chuck_send.py --agent claude --instrument sine --revision 76 --notes "36,0.8,0,480"',
            'scripts/chuck_send.py --agent claude --instrument sine --revision 82 --notes "33,0.7,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual(len(m["voices"]), 1)
        self.assertEqual(m["voices"][0]["revision"], 82)
        self.assertEqual(m["voices"][0]["notes"], "33,0.7,0,480")

    def test_lower_revision_after_higher_does_not_supersede(self):
        # Out-of-order: a stale rev posted late must NOT win (receiver drops it).
        lines = [
            "scripts/chuck_send.py --start --bpm 120 --bars 8",
            'scripts/chuck_send.py --agent a --instrument saw --revision 5 --notes "60,0.8,0,480"',
            'scripts/chuck_send.py --agent a --instrument saw --revision 2 --notes "48,0.8,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual(m["voices"][0]["revision"], 5)

    def test_latest_start_wins_transport(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 60 --bars 4",
            'scripts/chuck_send.py --agent a --instrument sine --revision 1 --notes "60,0.8,0,480"',
            "scripts/chuck_send.py --start --bpm 172 --bars 8",
        ]
        m = self._build(lines)
        self.assertEqual(m["transport"]["bpm"], 172.0)
        self.assertEqual(m["transport"]["bars"], 8)

    def test_multiple_agents_each_kept(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 95 --bars 8",
            'scripts/chuck_send.py --agent claude --instrument sine --revision 1 --notes "36,0.8,0,480"',
            'scripts/chuck_send.py --agent windy --instrument saw --revision 1 --notes "72,0.4,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual({v["agent"] for v in m["voices"]}, {"claude", "windy"})

    def test_windows_interpreter_prefix_parsed(self):
        # Windows agents post `venv-win\Scripts\python.exe scripts/chuck_send.py …`.
        lines = [
            r"venv-win\Scripts\python.exe scripts/chuck_send.py --start --bpm 95 --bars 8",
            r'venv-win\Scripts\python.exe scripts/chuck_send.py --agent windy --instrument saw --revision 1 --notes "72,0.4,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual(m["transport"]["bpm"], 95.0)
        self.assertEqual(m["voices"][0]["agent"], "windy")

    def test_pan_folded_into_voice(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 95 --bars 8",
            'scripts/chuck_send.py --agent a --instrument sine --revision 1 --notes "36,0.8,0,480" --pan -0.5',
        ]
        m = self._build(lines)
        self.assertEqual(m["voices"][0]["pan"], -0.5)

    def test_voice_synth_captured(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 172 --bars 4",
            'scripts/chuck_send.py --voice --agent lead --waveform saw --voice-gain 0.7 --voice-pan 0.1 '
            '--attack 0.02 --decay 0.1 --sustain 0.6 --release 0.4 --instrument saw --revision 1 --notes "60,0.8,0,480"',
        ]
        m = self._build(lines)
        synth = m["voices"][0]["synth"]
        self.assertEqual(synth["waveform"], "saw")
        self.assertEqual(synth["adsr"]["a"], 0.02)

    def test_global_controls_ignored(self):
        lines = [
            "scripts/chuck_send.py --start --bpm 95 --bars 8",
            "scripts/chuck_send.py --stop",
            "scripts/chuck_send.py --master-gain 0.6",
            'scripts/chuck_send.py --agent a --instrument sine --revision 1 --notes "36,0.8,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual(len(m["voices"]), 1)

    def test_no_commands_aborts(self):
        with self.assertRaises(SystemExit):
            self._build(["just some chat with no command", "[Gregory] nice jam"])

    def test_no_start_aborts(self):
        with self.assertRaises(SystemExit):
            self._build(['scripts/chuck_send.py --agent a --instrument sine --revision 1 --notes "36,0.8,0,480"'])

    def test_truncated_quote_line_skipped_not_crash(self):
        # A chat-truncated line with an unbalanced quote must be skipped, not crash.
        lines = [
            "scripts/chuck_send.py --start --bpm 95 --bars 8",
            'scripts/chuck_send.py --agent a --instrument sine --revision 1 --notes "36,0.8,0,4',  # truncated
            'scripts/chuck_send.py --agent b --instrument saw --revision 1 --notes "60,0.8,0,480"',
        ]
        m = self._build(lines)
        self.assertEqual([v["agent"] for v in m["voices"]], ["b"])


if __name__ == "__main__":
    unittest.main()
