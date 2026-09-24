"""Krita-free unit tests for the pure-Python modules (run with any Python 3.9+).

    python3 tests/test_logic.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "samselect"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "samselect" / "server"))

import modes  # noqa: E402
import protocol  # noqa: E402

S, C, A = modes.SHIFT, modes.CONTROL, modes.ALT


class ModifierMapping(unittest.TestCase):
    """Matches KisSelectionModifierMapper (plugins/tools/selectiontools)."""

    def test_default(self):
        f = lambda m: modes.mode_for_modifiers(m, modes.REPLACE)  # noqa: E731
        self.assertEqual(f(0), modes.REPLACE)
        self.assertEqual(f(S), modes.ADD)
        self.assertEqual(f(A), modes.SUBTRACT)
        self.assertEqual(f(S | A), modes.INTERSECT)
        self.assertEqual(f(C), modes.REPLACE)
        self.assertEqual(f(C | A), modes.SYMMETRIC_DIFFERENCE)

    def test_tool_option_mode_is_default(self):
        self.assertEqual(modes.mode_for_modifiers(0, modes.ADD), modes.ADD)
        self.assertEqual(modes.mode_for_modifiers(C, modes.ADD), modes.REPLACE)
        # Unmapped combinations fall back to the tool option, like SELECTION_DEFAULT.
        self.assertEqual(modes.mode_for_modifiers(S | C, modes.SUBTRACT), modes.SUBTRACT)
        self.assertEqual(modes.mode_for_modifiers(S | C | A, modes.ADD), modes.ADD)

    def test_swapped_ctrl_alt(self):
        f = lambda m: modes.mode_for_modifiers(m, modes.REPLACE, swap_ctrl_alt=True)  # noqa: E731
        self.assertEqual(f(A), modes.REPLACE)
        self.assertEqual(f(C), modes.SUBTRACT)
        self.assertEqual(f(S | C), modes.INTERSECT)
        self.assertEqual(f(C | A), modes.SYMMETRIC_DIFFERENCE)
        self.assertEqual(f(S), modes.ADD)

    def test_ignores_macos_control_key(self):
        self.assertEqual(modes.mode_for_modifiers(modes.META | S, modes.REPLACE), modes.ADD)


class Frames(unittest.TestCase):
    def test_roundtrip_in_chunks(self):
        frames = [({"id": 1, "cmd": "ping"}, b""), ({"id": 2, "cmd": "x"}, bytes(range(256)) * 1000)]
        stream = b"".join(protocol.encode(h, p) for h, p in frames)
        reader = protocol.FrameReader()
        got = []
        for i in range(0, len(stream), 997):  # arbitrary chunking, like a pipe
            got += reader.feed(stream[i : i + 997])
        self.assertEqual(got, frames)

    def test_bad_magic(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.FrameReader().feed(b"XXXX" + b"\0" * 12)


if __name__ == "__main__":
    unittest.main()
