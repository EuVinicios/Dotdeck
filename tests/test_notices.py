"""When a Dot's own news reach the desktop (src/core/notices.py, ADR-029)."""

from __future__ import annotations

import unittest

from tests.helpers import run_until  # noqa: F401  (isolates the run's config first)

from src.core.notices import DotNotices, done_body  # noqa: E402


class DotNoticesTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.notices = DotNotices(clock=lambda: self.now)

    def test_not_while_looking_at_that_dot_or_when_off(self):
        key = ("p", "a", "done")
        self.assertFalse(self.notices.allow(key, wanted=True, looking=True))
        self.assertFalse(self.notices.allow(key, wanted=False, looking=False))
        self.assertTrue(self.notices.allow(key, wanted=True, looking=False))

    def test_at_most_one_every_five_seconds_per_dot(self):
        a, b = ("p", "a", "done"), ("p", "b", "done")
        self.assertTrue(self.notices.allow(a, wanted=True, looking=False))
        self.now += 2
        self.assertFalse(self.notices.allow(a, wanted=True, looking=False))
        self.assertTrue(self.notices.allow(b, wanted=True, looking=False))   # another Dot
        self.now += 3.5
        self.assertTrue(self.notices.allow(a, wanted=True, looking=False))

    def test_the_body_names_the_conversation(self):
        self.assertEqual(done_body("Weekly plan"), "Finished replying in “Weekly plan”.")
        self.assertEqual(done_body(""), "Finished replying.")


if __name__ == "__main__":
    unittest.main()
