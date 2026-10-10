from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path

from tests.helpers import run_until, tmp_path

from src.core import secrets, store  # noqa: E402


class ProfileStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tmp_path(f"profiles-{self.id()}.json"))
        self.path.unlink(missing_ok=True)

    def test_add_switch_remove_and_persist(self):
        profiles = store.ProfileStore(self.path)
        self.assertIsNone(profiles.active)
        home = profiles.add("Homelab", "http://127.0.0.1:4310")
        work = profiles.add("", "https://dots.example.com")
        self.assertEqual(work.name, "https://dots.example.com")
        profiles.set_active(home.id)
        profiles.set_active(work.id)

        reloaded = store.ProfileStore(self.path)
        self.assertEqual([p.name for p in reloaded.list()], ["Homelab", "https://dots.example.com"])
        self.assertEqual(reloaded.active.id, work.id)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertNotIn("token", self.path.read_text())

        changed = []
        reloaded.connect("active-changed", lambda *_: changed.append(True))
        reloaded.remove(work.id)
        self.assertEqual(reloaded.active.id, home.id)
        self.assertEqual(changed, [True])

    def test_ignores_corrupt_entries(self):
        self.path.write_text(json.dumps({"active": "zzz", "profiles": [
            {"id": "a", "name": "A", "url": "http://a", "microphone": "always", "extra": 1},
            {"name": "no id"}, "junk"]}))
        profiles = store.ProfileStore(self.path)
        self.assertEqual([p.id for p in profiles.list()], ["a"])
        self.assertEqual(profiles.active.id, "a")
        self.assertEqual(profiles.get("a").microphone, store.MIC_ASK)
        self.path.write_text("{not json")
        self.assertEqual(store.ProfileStore(self.path).list(), [])


class SettingsTest(unittest.TestCase):
    def test_defaults_types_and_change_signal(self):
        path = Path(tmp_path("settings.json"))
        path.write_text(json.dumps({"notifications": "yes", "zoom": 1, "color-scheme": "dark"}))
        settings = store.Settings(path)
        self.assertTrue(settings.get("notifications"))      # wrong type falls back to default
        self.assertEqual(settings.get("zoom"), 1)
        self.assertEqual(settings.get("color-scheme"), "dark")
        seen = []
        settings.connect("changed", lambda _s, key: seen.append(key))
        settings.set("run-in-background", False)
        settings.set("run-in-background", False)
        self.assertEqual(seen, ["run-in-background"])
        self.assertFalse(store.Settings(path).get("run-in-background"))
        with self.assertRaises(KeyError):
            settings.set("nope", 1)


class TokenStoreTest(unittest.TestCase):
    def test_memory_backend_round_trip(self):
        self.assertFalse(secrets.keyring_available())  # tests never touch the real keyring
        tokens = secrets.TokenStore()
        saved = []
        tokens.store("p1", "http://h", "label", "tok", saved.append)
        self.assertEqual(saved, [True])
        found = []
        tokens.lookup("p1", "http://h", found.append)
        self.assertTrue(run_until(lambda: found))
        self.assertEqual(found, ["tok"])
        tokens.clear("p1", "http://h")
        tokens.lookup("p1", "http://h", found.append)
        self.assertEqual(found, ["tok", None])


if __name__ == "__main__":
    unittest.main()
