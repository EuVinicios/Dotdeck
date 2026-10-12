"""What things cost, in words (src/core/costs.py), and where the numbers come from:
the voice gateway's running total during a call and the header's line."""

from __future__ import annotations

import json
import unittest

from tests.helpers import run_until  # noqa: F401  (isolates the run's config first)

from src.core import costs, voice  # noqa: E402
from src.ui.window import call_subtitle  # noqa: E402
from tests.fake_server import SPEND  # noqa: E402


class SpendTextTest(unittest.TestCase):
    def test_month_and_what_is_left(self):
        self.assertEqual(costs.spend_text(SPEND), "US$ 0.29 this month · US$ 6.11 left")

    def test_a_key_limit_wins_over_the_account_credits(self):
        spend = {**SPEND, "key": {**SPEND["key"], "limit": 10, "limitRemaining": 9.71}}
        self.assertEqual(costs.spend_text(spend), "US$ 0.29 this month · US$ 9.71 left")
        self.assertIn("Key limit: US$ 10.00 (US$ 9.71 left)", costs.spend_tooltip(spend))

    def test_without_credits_only_the_month(self):
        self.assertEqual(costs.spend_text({**SPEND, "credits": None}), "US$ 0.29 this month")

    def test_nothing_to_show(self):
        for spend in (None, {}, {"available": False, "provider": None},
                      {"available": True, "provider": "openrouter",
                       "error": "OpenRouter returned HTTP 401."}):
            self.assertEqual(costs.spend_text(spend), "")
            self.assertEqual(costs.spend_tooltip(spend), "")

    def test_the_tooltip_has_every_period(self):
        lines = costs.spend_tooltip(SPEND).splitlines()
        self.assertEqual(lines[:5], ["OpenRouter spend (this server's key)", "Today: US$ 0.18",
                                     "This week: US$ 0.20", "This month: US$ 0.29",
                                     "All time: US$ 4.51"])
        self.assertIn("Account credits: US$ 6.11 of US$ 115.00 left", lines)


class ConversationCostTest(unittest.TestCase):
    COST = {"threadId": "t", "turns": 3, "inputTokens": 64511, "outputTokens": 1829,
            "costUsd": 0.0734, "calls": 1, "complete": True}

    def test_the_header_line(self):
        self.assertEqual(costs.conversation_text(self.COST), "≈ US$ 0.07 in this conversation")
        cheap = {**self.COST, "costUsd": 0.0046}
        self.assertEqual(costs.conversation_text(cheap), "≈ US$ 0.0046 in this conversation")

    def test_nothing_before_the_first_reply(self):
        self.assertEqual(costs.conversation_text({**self.COST, "costUsd": 0, "turns": 0,
                                                  "calls": 0}), "")
        self.assertEqual(costs.conversation_text(None), "")
        self.assertEqual(costs.conversation_text({"costUsd": "free"}), "")

    def test_the_tooltip(self):
        tip = costs.conversation_tooltip(self.COST)
        self.assertIn("3 replies · 64511 tokens in · 1829 tokens out", tip.replace(",", ""))
        self.assertIn("Includes the voice of 1 call", tip)
        self.assertNotIn("without a known price", tip)
        self.assertIn("without a known price",
                      costs.conversation_tooltip({**self.COST, "complete": False}))


class CallCostTest(unittest.TestCase):
    def test_voice_plus_research(self):
        self.assertIsNone(costs.call_cost(None, None))
        self.assertAlmostEqual(costs.call_cost(0.004, None), 0.004)
        self.assertAlmostEqual(costs.call_cost(0.004, 0.02), 0.024)
        self.assertAlmostEqual(costs.call_cost(None, -0.01), 0.0)   # a stale base
        tip = costs.call_tooltip(0.004, 0.02)
        self.assertIn("Voice and transcription: US$ 0.0040", tip)
        self.assertIn("Research during the call: US$ 0.02", tip)

    def test_the_header_line_during_a_call(self):
        self.assertEqual(call_subtitle("speaking", "01:23", connected=True, compact=False,
                                       cost=0.0046), "In a call · 01:23 · US$ 0.0046")
        self.assertEqual(call_subtitle("speaking", "01:23", connected=True, compact=True,
                                       cost=0.0046), "01:23")
        self.assertEqual(call_subtitle("listening", "01:23", connected=True, compact=False),
                         "In a call · 01:23")


class GatewayCostEventTest(unittest.TestCase):
    """The voice gateway's ``gateway.cost`` (not a Realtime event) feeds the counter."""

    def event(self, **data) -> str:
        return json.dumps({"type": "gateway.cost", **data})

    def test_parsed(self):
        self.assertEqual(voice.parse_call_event(self.event(cost=0.0012)).cost, 0.0012)
        for bad in ("a lot", -1, True, None, 1e9):
            self.assertIsNone(voice.parse_call_event(self.event(cost=bad)), bad)

    def test_the_tracker_only_reports_a_growing_total(self):
        tracker = voice.CallTracker()
        tracker.start()
        self.assertEqual(tracker.feed(self.event(cost=0.001)), [("call-cost", 0.001)])
        self.assertEqual(tracker.feed(self.event(cost=0.001)), [])
        self.assertEqual(tracker.feed(self.event(cost=0.0035)), [("call-cost", 0.0035)])
        self.assertEqual(tracker.phase, "connecting")        # the phase does not move
        tracker.end()
        self.assertEqual(tracker.feed(self.event(cost=0.01)), [])


if __name__ == "__main__":
    unittest.main()
