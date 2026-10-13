"""The window's sidebar layout and Dot switching (src/ui/window.py), without a window:
the dragged width and its rail, Ctrl+Tab's steps and each Dot's latest conversation."""

from __future__ import annotations

import unittest

from tests.helpers import run_until  # noqa: F401  (isolates the run's config first)

from src.core.workspace import Workspace  # noqa: E402
from src.ui.window import (RAIL_WIDTH, dragged_width, latest_conversations,  # noqa: E402
                           step_through)


class DraggedWidthTest(unittest.TestCase):
    def test_within_its_range(self):
        self.assertEqual(dragged_width(300, 40), (340, False))
        self.assertEqual(dragged_width(300, 500), (400, False))     # at most 400 px
        self.assertEqual(dragged_width(300, -70), (240, False))     # at least 240 px

    def test_narrower_snaps_to_the_rail_and_back(self):
        self.assertEqual(dragged_width(300, -100), (RAIL_WIDTH, True))
        self.assertEqual(dragged_width(RAIL_WIDTH, 60), (RAIL_WIDTH, True))
        self.assertEqual(dragged_width(RAIL_WIDTH, 160), (248, False))

    def test_right_to_left_the_edge_is_on_the_left(self):
        self.assertEqual(dragged_width(300, -40, rtl=True), (340, False))


class StepThroughTest(unittest.TestCase):
    def test_round_both_ways(self):
        order = ["a", "b", "c"]
        self.assertEqual(step_through(order, "a"), "b")
        self.assertEqual(step_through(order, "c"), "a")
        self.assertEqual(step_through(order, "a", back=True), "c")

    def test_from_no_dot(self):
        """From Activity (no Dot open): the first one forward, the last one back."""
        self.assertEqual(step_through(["a", "b"], None), "a")
        self.assertEqual(step_through(["a", "b"], "gone", back=True), "b")


class LatestConversationsTest(unittest.TestCase):
    def test_the_newest_per_dot_in_seconds(self):
        workspace = Workspace.from_json({"conversations": [
            {"id": "1", "dotId": "a", "title": "Old", "createdAt": 1_700_000_000_000},
            {"id": "2", "dotId": "a", "title": "New", "createdAt": 1_700_000_600_000},
            {"id": "3", "dotId": "b", "title": "Plan", "createdAt": 1_700_000_100},
        ]})
        self.assertEqual(latest_conversations(workspace), {
            "a": (1_700_000_600.0, "New"), "b": (1_700_000_100.0, "Plan")})


if __name__ == "__main__":
    unittest.main()
