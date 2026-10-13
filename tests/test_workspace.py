"""The workspace model (jobs from instructions, lookups) and the per-Dot choices."""

from __future__ import annotations

import unittest

from pathlib import Path

from tests.helpers import tmp_path

from src.core import dotprefs
from src.core.workspace import Dot, ServerState, Workspace, job_of, with_job

WORKSPACE = {
    "spaces": [{"id": "s1", "name": "Everyday", "description": "", "createdAt": 1}],
    "dots": [
        {"id": "d1", "spaceId": "s1", "spaceIds": ["s1"], "name": "Dot",
         "instructions": "Be thoughtful, practical, and concise. Help the user think clearly.",
         "researchAllowed": True, "memoryAllowed": True, "createdAt": 1,
         "learningContainerId": None, "skillDeliveryEnabled": False},
        {"id": "d2", "spaceId": "s1", "spaceIds": ["s1"], "name": "Manutenção",
         "instructions": "# Manutenção — backups e o servidor de casa\n\n"
                         "Cuida dos backups.",
         "researchAllowed": False, "memoryAllowed": True, "createdAt": 2},
    ],
    "conversations": [
        {"id": "c1", "dotId": "d2", "title": "A new thought", "createdAt": 10},
        {"id": "c2", "dotId": "d2", "title": "Backups", "createdAt": 20},
        {"id": "c3", "dotId": "d1", "title": "Oi", "createdAt": 5},
    ],
    "calls": [{"id": "k1", "threadId": "c2", "status": "active", "startedAt": 30}],
    "setup": {"voice": True, "missing": []},
    "activeModel": "deepseek/x",
}


class JobTests(unittest.TestCase):
    def test_header_and_prose(self):
        self.assertEqual(job_of("Manutenção", "# Manutenção — backups e o servidor de casa\nmore"),
                         "backups e o servidor de casa")
        self.assertEqual(job_of("Dot", "Be thoughtful. Then more."), "Be thoughtful.")
        self.assertEqual(job_of("X", ""), "")
        self.assertTrue(job_of("X", "word " * 40).endswith("…"))

    def test_with_job_rewrites_only_the_header(self):
        text = "# Manutenção — backups e o servidor de casa\n\nCuida dos backups."
        self.assertEqual(with_job("Manutenção", text, "Guardião"),
                         "# Manutenção — Guardião\n\nCuida dos backups.")
        self.assertEqual(with_job("Manutenção", text, ""), "Cuida dos backups.")
        self.assertEqual(with_job("Dot", "Plain prose.", "Helper"),
                         "# Dot — Helper\n\nPlain prose.")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace.from_json(WORKSPACE)

    def test_lookups(self):
        self.assertEqual([d.name for d in self.ws.dots], ["Dot", "Manutenção"])
        self.assertEqual(self.ws.dot("d2").job, "backups e o servidor de casa")
        self.assertEqual(self.ws.dot_index("d2"), 1)
        self.assertEqual(self.ws.dot_named("manutenção").id, "d2")
        self.assertEqual(self.ws.latest_conversation("d2").id, "c2")
        self.assertEqual(self.ws.dot_in_call().id, "d2")
        self.assertTrue(self.ws.voice_ready)
        self.assertEqual(self.ws.active_model, "deepseek/x")

    def test_update_body_is_complete_and_strict(self):
        body = self.ws.dot("d1").update_body(name="Dottie")
        self.assertEqual(body["name"], "Dottie")
        self.assertEqual(set(body), {"name", "instructions", "researchAllowed", "memoryAllowed",
                                     "spaceId", "spaceIds", "learningContainerId",
                                     "skillDeliveryEnabled"})

    def test_garbage_is_ignored(self):
        ws = Workspace.from_json({"dots": [None, {"id": 3}, {"id": "a", "name": "A"}],
                                  "calls": "x"})
        self.assertEqual([d.id for d in ws.dots], ["a"])
        self.assertIsNone(ws.active_call())
        self.assertEqual(Workspace.from_json(None), Workspace())

    def test_signature_changes_with_calls(self):
        quiet = dict(WORKSPACE, calls=[])
        self.assertNotEqual(Workspace.from_json(quiet).signature(), self.ws.signature())

    def test_server_state(self):
        state = ServerState.from_json({"settings": {"name": "Home", "paused": True},
                                       "memories": [{"id": "m", "text": "t"}]})
        self.assertTrue(state.paused)
        self.assertEqual(len(state.memories), 1)


class DotPrefsTests(unittest.TestCase):
    def test_pets_persist_and_swap(self):
        path = Path(tmp_path(f"dots-{self.id()}.json"))
        prefs = dotprefs.DotPrefs("p", path)
        dots = [Dot("a", "Assistente"), Dot("b", "Manutenção")]
        first = prefs.pets_for(dots)
        self.assertEqual(first, {"a": "sky", "b": "bolt"})
        again = dotprefs.DotPrefs("p", path).pets_for(dots)
        self.assertEqual(again, first)
        taken_from = prefs.set_pet("b", "sky")
        self.assertEqual(taken_from, "a")
        self.assertEqual(prefs.pet("b"), "sky")
        self.assertEqual(prefs.pet("a"), "bolt")

    def test_pinned_first_hidden_out(self):
        prefs = dotprefs.DotPrefs("p", Path(tmp_path(f"dots-{self.id()}.json")))
        dots = [Dot("a", "A"), Dot("b", "B"), Dot("c", "C")]
        prefs.set_pinned("c", True)
        prefs.set_hidden("b", True)
        self.assertEqual([d.id for d in prefs.ordered(dots)], ["c", "a"])
        self.assertEqual([d.id for d in prefs.hidden(dots)], ["b"])

    def test_seen_seeds_once_and_quietly(self):
        path = Path(tmp_path(f"dots-{self.id()}.json"))
        prefs = dotprefs.DotPrefs("p", path)
        changes = []
        prefs.connect("changed", lambda *_: changes.append(1))
        prefs.seed_seen({"a": 100.0, "b": 200.0})
        prefs.seed_seen({"a": 999.0})                 # a Dot keeps its first "seen"
        self.assertEqual((prefs.last_seen("a"), prefs.last_seen("b")), (100.0, 200.0))
        prefs.mark_seen("a", 300.0, notify=False)
        self.assertEqual(changes, [])                 # nothing redraws for these
        prefs.mark_seen("b", 400.0)
        self.assertEqual(changes, [1])
        again = dotprefs.DotPrefs("p", path)          # and they are saved
        self.assertEqual((again.last_seen("a"), again.last_seen("b")), (300.0, 400.0))


if __name__ == "__main__":
    unittest.main()
