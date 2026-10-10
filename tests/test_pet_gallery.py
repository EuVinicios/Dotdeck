"""The pet gallery and the New Dot dialog: tiles, swaps, automatic picks, creating a Dot."""

from __future__ import annotations

import json
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from src.core import dotprefs, monitor, pets  # noqa: E402
from src.core.store import Profile  # noqa: E402
from src.core.workspace import ServerState, Workspace  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402

TOKEN = "owner-token-for-pet-tests-0123456789"


class FakeTokens:
    def lookup(self, _profile_id, _url, callback):
        callback(TOKEN)


class FakeApp:
    def __init__(self, profile, mon):
        self.monitors = {profile.id: mon}
        self.tokens = FakeTokens()
        self.window = None


class ToastWindow(Adw.Window):
    """Stands in for MainWindow: dialogs present on it and toasts land in a list."""

    def __init__(self):
        super().__init__(default_width=900, default_height=860)
        self.set_content(Gtk.Box())
        self.toasts: list[tuple[str, str | None, object]] = []

    def toast(self, text, button=None, action=None):
        self.toasts.append((text, button, action))


class SettingsLikeWindow(Adw.Window):
    """Stands in for the Settings window: a ToastOverlay and a ``toast(text)`` without buttons."""

    def __init__(self):
        super().__init__(default_width=900, default_height=860)
        self.overlay = Adw.ToastOverlay(child=Gtk.Box())
        self.set_content(self.overlay)
        self.toasts: list[str] = []
        self.overlay_toasts: list[Adw.Toast] = []
        # AdwToastOverlay is final: record what is added through the Python attribute.
        self.overlay.add_toast = self.overlay_toasts.append

    def toast(self, text):
        self.toasts.append(text)


class DeferredTokens:
    """A keyring that answers only when the test says so."""

    def __init__(self):
        self.pending: list = []

    def lookup(self, _profile_id, _url, callback):
        self.pending.append(callback)


class StaticMonitor(GObject.Object):
    """A monitor that never polls: for states FakeOpenDots does not serve."""

    __gsignals__ = {"workspace-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
                    "changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, data, status="online"):
        super().__init__()
        self.workspace = Workspace.from_json(data)
        self.state = ServerState()
        self.status = status
        self.polls = 0

    def poll_now(self):
        self.polls += 1


class FakeDotsServer:
    """``/api/state`` and ``/api/workspace`` with Spaces, and ``POST /api/dots``.

    tests/fake_server.py has no Spaces and no writes, so this one records every POST.
    """

    def __init__(self, spaces, dots):
        self.spaces = spaces
        self.dots = dots
        self.posts: list[dict] = []
        self.fail: str | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._json(401, {"error": "Enter your owner access token."})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return
                if self.path == "/api/state":
                    self._json(200, {"settings": {"name": "Test", "paused": False,
                                                  "researchAllowed": True,
                                                  "memoryAllowed": True},
                                     "tasks": [], "memories": [], "configured": True})
                elif self.path == "/api/workspace":
                    self._json(200, {"spaces": fake.spaces, "dots": fake.dots,
                                     "conversations": [], "calls": [],
                                     "setup": {"missing": []}})
                else:
                    self._json(404, {"error": "Not found."})

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._authorized():
                    return
                body = json.loads(raw.decode() or "null")
                fake.posts.append({"path": self.path, "body": body,
                                   "type": self.headers.get("Content-Type")})
                if self.path != "/api/dots":
                    self._json(404, {"error": "Not found."})
                elif fake.fail:
                    self._json(400, {"error": fake.fail})
                else:
                    dot = {"id": f"new-{len(fake.dots)}", "name": body["name"],
                           "instructions": body["instructions"], "spaceId": body["spaceId"],
                           "spaceIds": [body["spaceId"]],
                           "researchAllowed": body["researchAllowed"],
                           "memoryAllowed": body["memoryAllowed"], "createdAt": 99}
                    fake.dots.append(dot)
                    self._json(201, dot)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def _closed(dialog) -> list[bool]:
    box: list[bool] = []
    dialog.connect("closed", lambda _d: box.append(True))
    return box


def _close_at_cleanup(test: unittest.TestCase, dialog) -> None:
    """Close a dialog a test left open (and only then: closing twice is a critical)."""
    closed = _closed(dialog)
    test.addCleanup(lambda: None if closed else dialog.force_close())


# -- pure parts ----------------------------------------------------------------------------

class NewDotHelpersTest(unittest.TestCase):
    def test_templates_start_with_the_job_header_and_stay_short(self):
        from src.ui import new_dot
        self.assertEqual([t.id for t in new_dot.TEMPLATES],
                         ["chief", "researcher", "writer", "finance", "home", "study", "blank"])
        for template in new_dot.TEMPLATES:
            text = new_dot.template_instructions(template)
            if template is new_dot.BLANK:
                self.assertEqual(text, "")
                continue
            name, job, body = new_dot.template_fields(template)
            self.assertTrue(text.startswith(f"# {name} — {job}\n\n"), text[:60])
            self.assertLess(new_dot.js_length(text), 1200, template.id)
            self.assertLessEqual(len(name), new_dot.NAME_LIMIT)
            self.assertTrue(body.endswith("."))

    def test_templates_suggest_fitting_pets(self):
        from src.ui import new_dot
        expected = {"chief": "sky", "researcher": "sage", "writer": "rosy", "finance": "sunny",
                    "home": "bolt", "study": "lilac"}
        for template_id, pet_id in expected.items():
            name, job, _body = new_dot.template_fields(new_dot.BY_ID[template_id])
            self.assertEqual(new_dot.free_pet(name, job, set()), pet_id, template_id)

    def test_compose_keeps_one_header(self):
        from src.ui.new_dot import compose
        self.assertEqual(compose("Ana", "analyst", "Reads reports."),
                         "# Ana — analyst\n\nReads reports.")
        self.assertEqual(compose("Ana", "analyst", "# Ana — old job\n\nReads reports."),
                         "# Ana — analyst\n\nReads reports.")
        self.assertEqual(compose("Ana", "", "Reads reports."), "Reads reports.")
        self.assertEqual(compose("Ana", "analyst", ""), "# Ana — analyst")
        self.assertEqual(compose("  Ana ", " analyst ", "  Body  "), "# Ana — analyst\n\nBody")
        # Only a header for this Dot is replaced; the owner's own headings stay.
        self.assertEqual(compose("Ana", "analyst", "# Tone - be kind\nReads reports."),
                         "# Ana — analyst\n\n# Tone - be kind\nReads reports.")
        self.assertEqual(compose("Ana", "analyst", "# ana – old\n\nReads."),
                         "# Ana — analyst\n\nReads.")
        self.assertEqual(compose("Ana - B", "analyst", "# Ana - B — old\nReads."),
                         "# Ana - B — analyst\n\nReads.")
        self.assertEqual(compose("Ana", "analyst", "# Ana — old"), "# Ana — analyst")

    def test_names_dot(self):
        from src.ui.new_dot import names_dot
        self.assertTrue(names_dot("# Chefe — chefe de gabinete", "Chefe"))
        self.assertTrue(names_dot("## chefe - x", "Chefe"))
        self.assertFalse(names_dot("# Chefe", "Chefe"))
        self.assertFalse(names_dot("Chefe — chefe", "Chefe"))
        self.assertFalse(names_dot("# Chefão — x", "Chefe"))
        self.assertFalse(names_dot("# Tone - be kind", "Ana"))

    def test_lengths_are_counted_like_the_server(self):
        from src.ui.new_dot import js_length
        self.assertEqual(js_length("abc"), 3)
        self.assertEqual(js_length("ção"), 3)
        self.assertEqual(js_length("🐶"), 2)  # one emoji is two UTF-16 units

    def test_problems(self):
        from src.ui.new_dot import problems
        self.assertEqual(problems("Ana", "Does things.", "s1", {"Chefe"}), {})
        self.assertIn("name", problems("  ", "Does things.", "s1"))
        self.assertIn("name", problems("chefe", "Does things.", "s1", {"Chefe"}))
        self.assertIn("name", problems("🐶" * 21, "Does things.", "s1"))
        self.assertIn("instructions", problems("Ana", " a ", "s1"))
        self.assertIn("instructions", problems("Ana", "x" * 2001, "s1"))
        self.assertNotIn("instructions", problems("Ana", "x" * 2000, "s1"))
        self.assertIn("space", problems("Ana", "Does things.", None))

    def test_tile_labels_and_automatic_pick(self):
        from src.core.workspace import Dot
        from src.ui import pet_gallery
        chief, other = Dot("a", "Chefe"), Dot("b", "Zelador")
        self.assertEqual(pet_gallery.tile_label(pets.get("bolt"), other), "Bolt, pet of Zelador")
        self.assertEqual(pet_gallery.tile_label(pets.get("sky"), None), "Sky, available")
        # The Dot's own current pick is ignored; other Dots' pets are not offered.
        self.assertEqual(pet_gallery.automatic_pet(chief, {"cocoa": chief, "bolt": other}),
                         "sky")
        self.assertNotEqual(pet_gallery.automatic_pet(chief, {"sky": other}), "sky")


# -- the gallery ---------------------------------------------------------------------------

@unittest.skipUnless(Gtk.init_check(), "no display available")
class PetGalleryTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.server = FakeOpenDots(token=TOKEN, dots=["Chief", "Researcher", "Zelador"]).start()
        self.addCleanup(self.server.stop)
        self.profile = Profile(f"gallery-{uuid.uuid4().hex[:8]}", "Home", self.server.url)
        self.monitor = monitor.ServerMonitor(self.profile, lambda cb: cb(TOKEN))
        self.addCleanup(self.monitor.stop)
        self.monitor.poll_now()
        self.assertTrue(run_until(lambda: len(self.monitor.workspace.dots) == 3))
        self.app = FakeApp(self.profile, self.monitor)
        self.window = ToastWindow()
        self.window.present()
        self.addCleanup(self.window.destroy)
        self.prefs = dotprefs.for_profile(self.profile.id)
        self.addCleanup(dotprefs.forget, self.profile.id)
        self.chief = self.monitor.workspace.dot("d0")
        self.done: list = []

    def open(self, dot=None):
        from src.ui.pet_gallery import choose_pet
        gallery = choose_pet(self.app, self.profile, dot or self.chief, self.window,
                             self.done.append)
        _close_at_cleanup(self, gallery)
        return gallery

    def test_shows_all_twelve_pets_and_who_has_them(self):
        gallery = self.open()
        children = []
        index = 0
        while (child := gallery.grid.get_child_at_index(index)) is not None:
            children.append(child)
            index += 1
        self.assertEqual(len(children), 12)
        self.assertEqual([c.pet.id for c in children], [p.id for p in pets.PETS])
        self.assertEqual(self.prefs.pet("d0"), "sky")
        self.assertEqual(gallery.current, "sky")
        self.assertEqual(gallery.selected, "sky")
        self.assertEqual([c.pet.id for c in gallery.grid.get_selected_children()], ["sky"])
        self.assertEqual(gallery.tiles["sky"].caption.get_label(), "Current")
        researcher_pet = self.prefs.pet("d1")
        self.assertEqual(researcher_pet, "sage")
        self.assertEqual(gallery.tiles["sage"].caption.get_label(), "With Researcher")
        self.assertEqual(gallery.tiles["bolt"].caption.get_label(), "With Zelador")
        self.assertEqual(gallery.tiles["cocoa"].owner, None)
        self.assertEqual(gallery.preview.get_property("pet"), "sky")
        self.assertEqual(gallery.get_title(), "Chief")
        # The keyboard starts on the current pet, and Enter means Choose.
        self.assertTrue(run_until(lambda: gallery.get_focus() is gallery.tiles["sky"], 3))
        self.assertIs(gallery.get_default_widget(), gallery.choose_button)

    def test_choosing_a_free_pet(self):
        gallery = self.open()
        closed = _closed(gallery)
        gallery.select("cocoa")
        self.assertEqual(gallery.preview.get_property("pet"), "cocoa")
        self.assertEqual(gallery.preview_note.get_label(), "Available")
        gallery.choose()
        self.assertIsNone(gallery.alert)
        self.assertEqual(self.prefs.pet("d0"), "cocoa")
        self.assertEqual(self.done, ["cocoa"])
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, ["cocoa"])  # once, not again with None on close
        self.assertEqual(self.window.toasts, [])

    def test_taken_pet_asks_then_swaps(self):
        gallery = self.open()
        closed = _closed(gallery)
        gallery.select("sage")
        self.assertIn("Researcher", gallery.preview_note.get_label())
        gallery.choose()
        self.assertIsNotNone(gallery.alert)
        self.assertEqual(gallery.alert.get_heading(), "Swap Pets with Researcher?")
        self.assertIn("Sky", gallery.alert.get_body())
        self.assertEqual(self.prefs.pet("d0"), "sky")  # nothing changes before the answer
        gallery.alert.emit("response", "swap")
        self.assertEqual(self.prefs.pet("d0"), "sage")
        self.assertEqual(self.prefs.pet("d1"), "sky")
        self.assertEqual(self.done, ["sage"])
        gallery.alert.force_close()
        self.assertTrue(run_until(lambda: closed))
        text, button, undo = self.window.toasts[-1]
        self.assertEqual(text, "Chief and Researcher swapped pets")
        self.assertEqual(button, "Undo")
        undo()
        self.assertEqual(self.prefs.pet("d0"), "sky")
        self.assertEqual(self.prefs.pet("d1"), "sage")

    def test_declining_the_swap_changes_nothing(self):
        gallery = self.open()
        closed = _closed(gallery)
        gallery.select("bolt")
        gallery.choose()
        gallery.alert.emit("response", "cancel")
        gallery.alert.force_close()
        run_until(lambda: False, 0.2)
        self.assertEqual(self.prefs.pet("d0"), "sky")
        self.assertEqual(self.prefs.pet("d2"), "bolt")
        self.assertEqual(self.done, [])
        self.assertFalse(closed)
        gallery.close()
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, [None])

    def test_automatic_picks_the_suggested_pet(self):
        self.prefs.pets_for(self.monitor.workspace.dots)
        self.prefs.set_pet("d0", "cocoa")
        gallery = self.open()
        self.assertEqual(gallery.selected, "cocoa")
        expected = pets.suggest("Chief", "", {"sage", "bolt"}).id
        self.assertEqual(expected, "sky")
        self.assertEqual(gallery.automatic(), expected)
        self.assertEqual(gallery.selected, expected)
        self.assertEqual(gallery.preview_note.get_label(), "Best match for Chief")
        gallery.choose()
        self.assertEqual(self.prefs.pet("d0"), "sky")
        self.assertEqual(self.done, ["sky"])

    def test_keeping_the_same_pet_and_preview_states(self):
        gallery = self.open()
        closed = _closed(gallery)
        gallery.states.set_active_name("speaking")
        self.assertEqual(gallery.preview.get_property("state"), "speaking")
        gallery.states.set_active_name("paused")
        self.assertEqual(gallery.preview.get_property("state"), "paused")
        gallery.choose()
        self.assertEqual(self.done, ["sky"])
        self.assertTrue(run_until(lambda: closed))

    def test_accepts_a_dot_id_and_refuses_unknown_ones(self):
        from src.ui.pet_gallery import choose_pet
        gallery = self.open("d1")
        self.assertEqual(gallery.dot.name, "Researcher")
        self.assertEqual(gallery.current, "sage")
        self.assertIsNone(choose_pet(self.app, self.profile, "missing", self.window,
                                     self.done.append))
        self.assertEqual(self.done, [None])

    def test_follows_changes_made_elsewhere(self):
        gallery = self.open()
        self.prefs.set_pet("d1", "cocoa")  # e.g. from the sidebar of another window
        self.assertEqual(gallery.tiles["cocoa"].caption.get_label(), "With Researcher")
        self.assertEqual(gallery.tiles["sage"].owner, None)

    def test_swap_from_the_settings_window(self):
        """Settings has toast(text) only: the Undo toast goes to its overlay, no crash."""
        from src.ui.pet_gallery import choose_pet
        window = SettingsLikeWindow()
        window.present()
        self.addCleanup(window.destroy)
        gallery = choose_pet(self.app, self.profile, self.chief, window, self.done.append)
        _close_at_cleanup(self, gallery)
        closed = _closed(gallery)
        gallery.select("bolt")
        gallery.choose()
        gallery.alert.emit("response", "swap")
        gallery.alert.force_close()
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, ["bolt"])
        self.assertEqual(self.prefs.pet("d2"), "sky")
        self.assertEqual(window.toasts, [])
        toast, = window.overlay_toasts
        self.assertEqual(toast.get_title(), "Chief and Zelador swapped pets")
        self.assertEqual(toast.get_button_label(), "Undo")
        toast.emit("button-clicked")
        self.assertEqual(self.prefs.pet("d0"), "sky")
        self.assertEqual(self.prefs.pet("d2"), "bolt")

    def test_closes_when_the_dot_is_deleted(self):
        from src.ui.pet_gallery import choose_pet
        static = StaticMonitor({"dots": [{"id": "a", "name": "Chefe"},
                                         {"id": "b", "name": "Zelador"}]})
        app = FakeApp(self.profile, static)
        gallery = choose_pet(app, self.profile, "b", self.window, self.done.append)
        _close_at_cleanup(self, gallery)
        closed = _closed(gallery)
        gallery.select("cocoa")
        static.workspace = Workspace.from_json({"dots": [{"id": "a", "name": "Chefe"}]})
        static.emit("workspace-changed")
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, [None])
        self.assertNotEqual(self.prefs.pet("b"), "cocoa")  # the selection was never applied
        self.assertEqual(self.window.toasts[-1][0], "Zelador was deleted on the server")
        gallery.choose()  # a late Enter does nothing
        self.assertEqual(self.done, [None])

    def test_more_dots_than_pets(self):
        """Past 12 Dots pets repeat, but the Dot being edited owns its own pet."""
        from src.ui.pet_gallery import choose_pet
        names = [f"Dot {i}" for i in range(14)]
        static = StaticMonitor({"dots": [{"id": f"x{i}", "name": n}
                                         for i, n in enumerate(names)]})
        app = FakeApp(self.profile, static)
        mapping = self.prefs.pets_for(static.workspace.dots)
        last = static.workspace.dot("x13")
        gallery = choose_pet(app, self.profile, last, self.window, self.done.append)
        _close_at_cleanup(self, gallery)
        mine = mapping["x13"]
        self.assertEqual(gallery.current, mine)
        self.assertIs(gallery.owners[mine], gallery.dot)
        self.assertEqual(gallery.tiles[mine].caption.get_label(), "Current")


# -- New Dot -------------------------------------------------------------------------------

@unittest.skipUnless(Gtk.init_check(), "no display available")
class NewDotTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.server = FakeDotsServer(
            spaces=[{"id": "s1", "name": "Everyday", "description": "A little space."},
                    {"id": "s2", "name": "Money", "description": "Bills and budgets."}],
            dots=[{"id": "d0", "name": "Chefe", "spaceId": "s1",
                   "instructions": "# Chefe — chefe de gabinete\n\nCoordena."}])
        self.addCleanup(self.server.stop)
        self.profile = Profile(f"newdot-{uuid.uuid4().hex[:8]}", "Home", self.server.url)
        self.monitor = monitor.ServerMonitor(self.profile, lambda cb: cb(TOKEN))
        self.addCleanup(self.monitor.stop)
        self.monitor.poll_now()
        self.assertTrue(run_until(lambda: len(self.monitor.workspace.spaces) == 2))
        self.app = FakeApp(self.profile, self.monitor)
        self.window = ToastWindow()
        self.window.present()
        self.addCleanup(self.window.destroy)
        self.prefs = dotprefs.for_profile(self.profile.id)
        self.addCleanup(dotprefs.forget, self.profile.id)
        self.done: list = []

    def open(self):
        from src.ui.new_dot import create_dot
        dialog = create_dot(self.app, self.profile, self.window, self.done.append)
        _close_at_cleanup(self, dialog)
        return dialog

    def test_starts_blank_with_create_disabled(self):
        dialog = self.open()
        self.assertTrue(dialog.chips["blank"].get_active())
        self.assertEqual(dialog.name_row.get_text(), "")
        self.assertFalse(dialog.create_button.get_sensitive())
        self.assertEqual([s.id for s in dialog.spaces], ["s1", "s2"])
        self.assertEqual(dialog.space_id, "s1")
        self.assertEqual(dialog.counter.get_label(), "0/2000")
        self.assertFalse(dialog.name_hint.get_visible())
        self.assertTrue(run_until(lambda: dialog.get_focus() is not None and (
            dialog.get_focus() is dialog.name_row or
            dialog.get_focus().is_ancestor(dialog.name_row)), 3))

    def test_template_prefills_name_job_and_instructions(self):
        from src.ui import new_dot
        dialog = self.open()
        dialog.chips["researcher"].set_active(True)
        self.assertEqual(dialog.name_row.get_text(), "Researcher")
        self.assertEqual(dialog.job_row.get_text(), "finds and checks sources for you")
        self.assertEqual(dialog.instructions(),
                         new_dot.template_instructions(new_dot.BY_ID["researcher"]))
        self.assertEqual(dialog.header_line.get_label(),
                         "# Researcher — finds and checks sources for you")
        self.assertTrue(dialog.create_button.get_sensitive())
        self.assertEqual(dialog.hero_name.get_label(), "Researcher")

    def test_validation(self):
        dialog = self.open()
        dialog.name_row.set_text("Ana")
        dialog.buffer.set_text("ab")
        self.assertFalse(dialog.create_button.get_sensitive())
        self.assertIn("instructions", dialog.issues)
        dialog.buffer.set_text("Follows the regional market.")
        self.assertTrue(dialog.create_button.get_sensitive())
        dialog.name_row.set_text("chefe")
        self.assertFalse(dialog.create_button.get_sensitive())
        self.assertTrue(dialog.name_hint.get_visible())
        self.assertEqual(dialog.name_hint.get_label(), "Another Dot is already called “chefe”.")
        self.assertTrue(dialog.name_row.has_css_class("error"))
        dialog.name_row.set_text("Ana")
        self.assertFalse(dialog.name_row.has_css_class("error"))
        dialog.buffer.set_text("x" * 2001)
        self.assertFalse(dialog.create_button.get_sensitive())
        self.assertTrue(dialog.counter.has_css_class("error"))
        self.assertEqual(dialog.counter.get_label(), "2001/2000")
        dialog.job_row.set_text("analyst")
        self.assertEqual(dialog.counter.get_label(), f"{2001 + len('# Ana — analyst') + 2}/2000")
        self.assertEqual(dialog.name_row.get_max_length(), 40)

    def test_creates_the_dot_with_the_right_body(self):
        from src.ui import new_dot
        dialog = self.open()
        closed = _closed(dialog)
        dialog.use_template("finance")
        self.assertEqual(dialog.pet_id, "sunny")
        dialog.space_row.set_selected(1)
        dialog.research_row.set_active(False)
        dialog.create()
        self.assertTrue(run_until(lambda: self.done))
        self.assertEqual(len(self.server.posts), 1)
        post = self.server.posts[0]
        self.assertEqual(post["path"], "/api/dots")
        self.assertEqual(post["type"], "application/json")
        self.assertEqual(post["body"], {
            "name": "Finance",
            "instructions": new_dot.template_instructions(new_dot.BY_ID["finance"]),
            "spaceId": "s2", "researchAllowed": False, "memoryAllowed": True})
        self.assertEqual(self.done, ["new-1"])
        self.assertEqual(self.prefs.pet("new-1"), "sunny")
        self.assertIn(("Finance is ready", None, None), self.window.toasts)
        self.assertTrue(run_until(lambda: self.monitor.workspace.dot("new-1") is not None))
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, ["new-1"])

    def test_new_dot_never_gets_a_pet_in_use(self):
        dialog = self.open()
        self.assertEqual(self.prefs.pet("d0"), "sky")  # assigned when the dialog opened
        dialog.use_template("chief")
        self.assertNotEqual(dialog.pet_id, "sky")
        self.assertEqual(dialog.pet_id, pets.suggest("Chief of Staff", "keeps your week on track",
                                                     {"sky"}).id)
        self.assertEqual(dialog.hero_avatar.get_property("pet"), dialog.pet_id)

    def test_server_error_is_shown_inline(self):
        self.server.fail = "Provide a name, role instructions, and explicit tool permissions."
        dialog = self.open()
        closed = _closed(dialog)
        dialog.use_template("writer")
        dialog.create()
        self.assertTrue(run_until(lambda: dialog.banner.get_revealed()))
        self.assertIn(self.server.fail, dialog.banner.get_title())
        self.assertTrue(dialog.create_button.get_sensitive())
        self.assertTrue(dialog.get_can_close())
        self.assertEqual(self.done, [])
        self.assertFalse(closed)
        self.server.fail = None
        dialog.create()
        self.assertTrue(run_until(lambda: self.done))
        self.assertEqual(self.done, ["new-1"])

    def test_a_silent_server_does_not_trap_the_dialog(self):
        from src.ui import new_dot

        class SilentTokens:
            def lookup(self, *_args):
                pass  # e.g. a locked keyring that never answers

        self.app.tokens = SilentTokens()
        old = new_dot.WATCHDOG_SECONDS
        new_dot.WATCHDOG_SECONDS = 1
        self.addCleanup(setattr, new_dot, "WATCHDOG_SECONDS", old)
        dialog = self.open()
        dialog.use_template("writer")
        dialog.create()
        self.assertFalse(dialog.get_can_close())
        self.assertTrue(run_until(lambda: dialog.banner.get_revealed(), 5))
        self.assertTrue(dialog.get_can_close())
        self.assertTrue(dialog.cancel_button.get_sensitive())
        self.assertEqual(self.done, [])

    def test_choosing_a_template_over_own_text_asks_first(self):
        dialog = self.open()
        dialog.use_template("researcher")
        dialog.buffer.set_text("My own words.")
        dialog.chips["writer"].set_active(True)
        self.assertIsNotNone(dialog.alert)
        dialog.alert.emit("response", "keep")
        dialog.alert.force_close()
        self.assertTrue(dialog.chips["researcher"].get_active())
        self.assertFalse(dialog.chips["writer"].get_active())
        self.assertEqual(dialog.body(), "My own words.")
        dialog.chips["writer"].set_active(True)
        dialog.alert.emit("response", "replace")
        dialog.alert.force_close()
        self.assertEqual(dialog.name_row.get_text(), "Writer")
        self.assertTrue(dialog.chips["writer"].get_active())

    def test_a_typed_name_survives_templates(self):
        dialog = self.open()
        dialog.name_row.set_text("Ana")
        dialog.chips["researcher"].set_active(True)
        self.assertIsNone(dialog.alert)
        self.assertEqual(dialog.name_row.get_text(), "Ana")
        self.assertTrue(dialog.instructions().startswith(
            "# Ana — finds and checks sources for you\n\nYou are my research assistant."))
        dialog.chips["writer"].set_active(True)
        self.assertIsNone(dialog.alert)
        self.assertEqual(dialog.name_row.get_text(), "Ana")
        self.assertEqual(dialog.job_row.get_text(), "drafts and polishes your writing")

    def test_untouched_template_switches_without_asking(self):
        dialog = self.open()
        dialog.use_template("researcher")
        dialog.chips["study"].set_active(True)
        self.assertIsNone(dialog.alert)
        self.assertEqual(dialog.name_row.get_text(), "Study Partner")

    def test_no_spaces_means_no_create(self):
        from src.ui.new_dot import create_dot
        empty = StaticMonitor({"spaces": [], "dots": []})
        app = FakeApp(self.profile, empty)
        dialog = create_dot(app, self.profile, self.window, self.done.append)
        _close_at_cleanup(self, dialog)
        dialog.use_template("writer")
        self.assertIn("space", dialog.issues)
        self.assertFalse(dialog.create_button.get_sensitive())
        self.assertIn("no Spaces", dialog.space_row.get_subtitle())
        # Spaces that arrive later are picked up.
        empty.workspace = Workspace.from_json({"spaces": [{"id": "s9", "name": "Late"}]})
        empty.emit("workspace-changed")
        self.assertEqual(dialog.space_id, "s9")
        self.assertTrue(dialog.create_button.get_sensitive())

    def test_a_late_token_never_creates_the_dot(self):
        """After the watchdog gave the controls back, a late keyring answer sends nothing."""
        from src.ui import new_dot
        tokens = DeferredTokens()
        self.app.tokens = tokens
        old = new_dot.WATCHDOG_SECONDS
        new_dot.WATCHDOG_SECONDS = 1
        self.addCleanup(setattr, new_dot, "WATCHDOG_SECONDS", old)
        dialog = self.open()
        dialog.use_template("writer")
        dialog.create()
        dialog.create()  # Ctrl+Enter again while waiting: ignored
        self.assertEqual(len(tokens.pending), 1)
        self.assertTrue(dialog.create_button.get_sensitive())  # keeps its colour, with a spinner
        self.assertTrue(run_until(lambda: dialog.banner.get_revealed(), 5))
        self.assertIn("keyring", dialog.banner.get_title())
        tokens.pending[0](TOKEN)  # the keyring finally answers
        run_until(lambda: False, 0.5)
        self.assertEqual(self.server.posts, [])
        self.assertEqual(self.done, [])
        dialog.create()  # trying again creates exactly one Dot
        self.assertEqual(len(tokens.pending), 2)
        tokens.pending[1](TOKEN)
        self.assertTrue(run_until(lambda: self.done))
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(self.done, ["new-1"])

    def test_cancel_before_the_token_sends_nothing(self):
        tokens = DeferredTokens()
        self.app.tokens = tokens
        dialog = self.open()
        dialog.use_template("writer")
        dialog.create()
        self.assertFalse(dialog.get_can_close())
        dialog.force_close()  # e.g. the window closes
        tokens.pending[0](TOKEN)
        run_until(lambda: False, 0.5)
        self.assertEqual(self.server.posts, [])
        self.assertEqual(self.done, [None])

    def test_a_pet_taken_meanwhile_is_not_stolen(self):
        tokens = DeferredTokens()
        self.app.tokens = tokens
        dialog = self.open()
        dialog.use_template("finance")
        self.assertEqual(dialog.pet_id, "sunny")
        dialog.create()
        self.prefs.set_pet("d0", "sunny")  # chosen in the gallery while the request runs
        tokens.pending[0](TOKEN)
        self.assertTrue(run_until(lambda: self.done))
        self.assertEqual(self.done, ["new-1"])
        self.assertEqual(self.prefs.pet("d0"), "sunny")
        self.assertNotEqual(self.prefs.pet("new-1"), "sunny")

    def test_created_from_the_settings_window(self):
        """Settings has toast(text) only: the dialog still closes and says the Dot is ready."""
        from src.ui.new_dot import create_dot
        window = SettingsLikeWindow()
        window.present()
        self.addCleanup(window.destroy)
        dialog = create_dot(self.app, self.profile, window, self.done.append)
        _close_at_cleanup(self, dialog)
        closed = _closed(dialog)
        dialog.use_template("writer")
        dialog.create()
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, ["new-1"])
        self.assertEqual(window.toasts, ["Writer is ready"])

    def test_server_permissions_and_status(self):
        from src.ui.new_dot import create_dot
        static = StaticMonitor({"spaces": [{"id": "s1", "name": "Money & Bills",
                                            "description": "Bills & budgets"}], "dots": []})
        static.state = ServerState(research_allowed=False)
        dialog = create_dot(FakeApp(self.profile, static), self.profile, self.window,
                            self.done.append)
        _close_at_cleanup(self, dialog)
        self.assertEqual(dialog.research_row.get_subtitle(),
                         "Off for the whole server, in Privacy")
        self.assertEqual(dialog.memory_row.get_subtitle(), "Can save and use memories about you")
        self.assertEqual(dialog.space_row.get_subtitle(), "Bills &amp; budgets")
        static.state = ServerState()
        static.emit("changed")
        self.assertEqual(dialog.research_row.get_subtitle(), "Can search the web and read pages")

        offline = StaticMonitor({"spaces": [], "dots": []}, status="offline")
        other = create_dot(FakeApp(self.profile, offline), self.profile, self.window, None)
        _close_at_cleanup(self, other)
        self.assertEqual(other.space_row.get_subtitle(), "The server is not answering right now.")
        offline.status = "online"
        offline.emit("changed")
        self.assertIn("no Spaces", other.space_row.get_subtitle())

    def test_cancel_reports_none(self):
        dialog = self.open()
        closed = _closed(dialog)
        dialog.cancel_button.emit("clicked")
        self.assertTrue(run_until(lambda: closed))
        self.assertEqual(self.done, [None])
        self.assertEqual(self.server.posts, [])


if __name__ == "__main__":
    unittest.main()
