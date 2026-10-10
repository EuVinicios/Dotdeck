"""The header's line under the Dot's name during a call (src/ui/window.py)."""

from __future__ import annotations

import unittest

from tests.helpers import run_until  # noqa: F401  (isolates the run's config first)

from src.ui.window import PHASE_WORDS, call_subtitle  # noqa: E402


class CallSubtitleTest(unittest.TestCase):
    def test_one_width_for_the_whole_call(self):
        """The phase changes only the pet: the centred title does not move with it."""
        lines = {call_subtitle(phase, "01:23", connected=True, compact=False)
                 for phase in ("", "listening", "thinking", "speaking")}
        self.assertEqual(lines, {"In a call · 01:23"})
        self.assertEqual(set(PHASE_WORDS), {"connecting", "listening", "thinking", "speaking"})

    def test_no_time_until_the_call_connects(self):
        self.assertEqual(call_subtitle("connecting", "00:00", connected=False, compact=False),
                         "Calling…")
        self.assertEqual(call_subtitle("connecting", "00:42", connected=True, compact=False),
                         "Reconnecting…")

    def test_saving_and_phone_width(self):
        self.assertEqual(call_subtitle("ended", "01:23", connected=True, compact=False),
                         "Saving the call…")
        # A very narrow window keeps the time: the call button there is only an icon.
        self.assertEqual(call_subtitle("speaking", "01:23", connected=True, compact=True),
                         "01:23")


if __name__ == "__main__":
    unittest.main()
