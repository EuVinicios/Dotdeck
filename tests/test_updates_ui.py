"""The "OpenDots Updates" group and the monitor's update notice, against a fake updater."""

from __future__ import annotations

import unittest

from tests.helpers import run_until, tmp_path

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.core import monitor, secrets, store, updates  # noqa: E402
from tests.fake_server import FakeUpdater  # noqa: E402

TOKEN = "owner-token-for-ui-tests-0123456789"


class FakeWindow:
    def __init__(self):
        self.opened = []

    def open_external(self, uri):
        self.opened.append(uri)


class FakeApp:
    def __init__(self, profile_store):
        self.profiles = profile_store
        self.tokens = secrets.TokenStore()
        self.monitors = {}
        self.window = FakeWindow()
        self.reloaded = []

    def reload_server(self, profile_id):
        self.reloaded.append(profile_id)


@unittest.skipUnless(Gtk.init_check(), "no display available")
class UpdatesUiTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.updater = FakeUpdater(TOKEN)
        self.addCleanup(self.updater.stop)
        profiles = store.ProfileStore(store.Path(tmp_path(f"profiles-{self.id()}.json")))
        self.profile = profiles.add("Homelab", "http://127.0.0.1:4310")
        self.profile.updater_url = self.updater.url
        profiles.update(self.profile)
        self.app = FakeApp(profiles)
        self.app.tokens.store(self.profile.id, self.profile.url, "t", TOKEN)
        mon = monitor.ServerMonitor(self.profile, lambda cb: cb(TOKEN))
        self.app.monitors[self.profile.id] = mon
        self.toasts = []

    def test_lists_news_and_updates_the_server(self):
        from src.ui.updates import UpdatesGroup
        group = UpdatesGroup(self.app, self.profile, self.toasts.append)
        window = Gtk.Window(child=group)
        window.present()
        self.addCleanup(window.destroy)
        self.assertTrue(run_until(lambda: group.current.get_visible()
                                  and group.current.get_subtitle(), 10))
        self.assertIn("083a6b4", group.current.get_subtitle())
        self.assertIn("2", group.news.get_subtitle())
        self.assertEqual(len(group._news_rows), 2)
        self.assertTrue(group.update_button.get_sensitive())
        self.assertFalse(group.missing.get_visible())

        group._update()   # what "Update" in the confirmation dialog does
        self.assertTrue(run_until(lambda: self.updater.updates_started == 1, 5))
        self.assertTrue(run_until(lambda: self.toasts, 15))
        self.assertEqual(self.toasts[-1], "OpenDots was updated")
        self.assertEqual(self.app.reloaded, [self.profile.id])
        self.assertTrue(run_until(lambda: not group.update_button.get_sensitive(), 5))
        self.assertIn("abc1234", group.job.get_subtitle())

    def test_a_blocked_update_says_why_and_cannot_start(self):
        from src.ui.updates import UpdatesGroup
        self.updater.extra = {"conflicts": ["src/client/App.tsx", "src/server/app.ts"],
                              "untracked": ["notes & ideas.md"]}
        group = UpdatesGroup(self.app, self.profile, self.toasts.append)
        window = Gtk.Window(child=group)
        window.present()
        self.addCleanup(window.destroy)
        self.assertTrue(run_until(lambda: group.blocked.get_visible(), 10))
        text = group.blocked.get_subtitle()
        self.assertIn("in 2 files: src/client/App.tsx, src/server/app.ts.", text)
        self.assertIn("would be overwritten: notes & ideas.md.", text)   # not markup
        self.assertFalse(group.update_button.get_sensitive())
        self.assertIn("blocked", group.update_button.get_title())

        self.updater.extra = {"branch": "experiment"}
        group.refresh()
        self.assertTrue(run_until(lambda: "experiment" in group.blocked.get_subtitle(), 10))
        self.assertNotIn("conflict", group.blocked.get_subtitle())
        self.updater.extra = {}
        group.refresh()
        self.assertTrue(run_until(lambda: not group.blocked.get_visible(), 10))
        self.assertTrue(group.update_button.get_sensitive())
        self.assertEqual(group.update_button.get_title(), "Update Server…")

    def test_failed_checks_show_their_output(self):
        from src.ui.updates import UpdatesGroup
        self.updater.job = {"state": "failed", "message": "npm test failed (exit 1).",
                            "output": ["FAIL tests/chat.test.ts > keeps <b>markup</b>"],
                            "finished_at": "2026-10-10T01:00:00+00:00"}
        group = UpdatesGroup(self.app, self.profile, self.toasts.append)
        window = Gtk.Window(child=group)
        window.present()
        self.addCleanup(window.destroy)
        self.assertTrue(run_until(lambda: group.job.get_visible(), 10))
        self.assertIn("npm test failed", group.job.get_subtitle())
        self.assertEqual(group.job.get_tooltip_text(),
                         "FAIL tests/chat.test.ts > keeps <b>markup</b>")

    def test_missing_updater_explains_how_to_install(self):
        from src.ui.updates import UpdatesGroup
        self.profile.updater_url = "http://127.0.0.1:9"
        group = UpdatesGroup(self.app, self.profile, self.toasts.append)
        window = Gtk.Window(child=group)
        window.present()
        self.addCleanup(window.destroy)
        self.assertTrue(run_until(lambda: group.missing.get_visible(), 10))
        self.assertFalse(group.update_button.get_visible())

    def test_monitor_announces_news_once(self):
        mon = self.app.monitors[self.profile.id]
        seen = []
        mon.connect("updates-available", lambda _m, behind, upstream: seen.append((behind, upstream)))
        mon.poll_updates()
        self.assertTrue(run_until(lambda: seen, 10))
        mon.poll_updates()
        run_until(lambda: False, 0.5)
        self.assertEqual(seen, [(2, "c0ffee1")])
        mon.stop()


class BlockersTest(unittest.TestCase):
    """What stops an update, read from the updater's /status (no display needed)."""

    def test_reads_every_reason_and_older_updaters(self):
        found = updates.blockers({"dirty": True, "branch": "", "expected_branch": "main",
                                  "untracked": ["a.txt"], "conflicts": None})
        self.assertEqual(found, {"dirty": True, "branch": ("", "main"), "untracked": ["a.txt"],
                                 "conflicts": [], "any": True})
        # An updater from before the conflict preview reports none of these.
        self.assertFalse(updates.blockers({"behind": 3, "dirty": False})["any"])
        self.assertFalse(updates.blockers({"branch": "main", "expected_branch": "main"})["any"])

    def test_knows_when_the_server_runs_here(self):
        for url, here in (("http://127.0.0.1:4310", True), ("http://localhost:4310", True),
                          ("http://[::1]:4310", True), ("http://aurora.local:4310", False),
                          ("https://aurora.tail1234.ts.net", False),
                          ("http://192.168.0.10:4310", False)):
            profile = store.Profile(id="p", name="Aurora", url=url)
            self.assertEqual(updates.on_this_machine(profile), here, url)


if __name__ == "__main__":
    unittest.main()
