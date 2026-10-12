"""The Settings window: every page, search, bound settings, the Dot editor and the server pages.

A small recording OpenDots stands in for the server (strict ``PUT /api/dots/:id`` like
the real one), so the tests can check the exact bodies Dotdeck sends.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import run_until, tmp_path

# The IBus input method answers after a test window is gone and crashes the run.
os.environ.setdefault("GTK_IM_MODULE", "gtk-im-context-simple")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.core import models, monitor, secrets, store  # noqa: E402
from src.ui.settings import search  # noqa: E402
from tests.fake_server import (ANTHROPIC_URL, MODEL_CONFIG, MODEL_CONFIG_032,  # noqa: E402
                               FakeModelConfig)

TOKEN = "owner-token-for-settings-tests-0123456789"
DOT_KEYS = {"name", "instructions", "researchAllowed", "memoryAllowed", "learningContainerId",
            "skillDeliveryEnabled", "spaceIds", "spaceId"}
SETTINGS_KEYS = {"name", "paused", "researchAllowed", "memoryAllowed"}
# A server set up with Dotdeck 0.3.2 (calls through the voice gateway).
GATEWAY_CONFIG = MODEL_CONFIG_032


def sample_dots() -> list[dict]:
    """Fictional Dots like a real server's: a "# Name — job" first line, several Spaces."""
    def dot(index, name, job, body, spaces=("s1",), research=True, memory=True):
        instructions = f"# {name} — {job}\n\n{body}" if job else body
        return {"id": f"d{index}", "spaceId": spaces[0], "spaceIds": list(spaces), "name": name,
                "instructions": instructions, "researchAllowed": research,
                "memoryAllowed": memory, "createdAt": 1000 + index,
                "learningContainerId": None, "skillDeliveryEnabled": True}
    return [
        dot(0, "Dot", "", "Be thoughtful, practical, and concise. Help with everyday tasks."),
        dot(1, "Assistente", "organiza o dia e os outros Dots",
            "Coordinate the other Dots and keep the week on track.", ("s1", "s2")),
        dot(2, "Manutenção", "backups e o servidor de casa",
            "Keep the home server healthy: backups, updates, disks.", ("s3",)),
        dot(3, "Finanças", "orçamento, contas e investimentos",
            "Budgets, invoices and investments.", ("s4",), research=False),
        dot(4, "Estratégia", "estratégia e segundas opiniões",
            "Plans, trade-offs and second opinions.", ("s5",)),
        dot(5, "Laboratório", "código, scripts e automações",
            "Prototype small tools and scripts.", ("s6",)),
        dot(6, "Redação", "posts, e-mails e documentos", "Drafts posts and emails.", ("s7",)),
        dot(7, "Estudos", "cursos, idiomas e planos de estudo", "Courses and study plans.",
            ("s8",), memory=False),
    ]


def sample_connection(dot_id: str = "d2") -> dict:
    """An MCP server as ``GET /api/dots/:id/connections`` lists it."""
    def tool(name, title, read_only, enabled=True, approval=None):
        return {"name": name, "title": title, "description": f"{title}, on the home server.",
                "inputSchema": {}, "readOnly": read_only, "enabled": enabled,
                "requiresApproval": (not read_only) if approval is None else approval}
    return {"id": "c1", "dotId": dot_id, "name": "Homelab", "url": "https://mcp.home.test/mcp",
            "hasToken": True, "error": None, "createdAt": 1, "updatedAt": 1,
            "tools": [tool("list_disks", "List disks", True),
                      tool("restart_service", "Restart a service", False),
                      tool("wipe_disk", "Wipe a disk", False, enabled=False)]}


def sample_spaces() -> list[dict]:
    names = [("s1", "Dia a dia", "Everyday notes and plans"), ("s2", "Agenda", "The week ahead"),
             ("s3", "Casa", "The house and the home server"),
             ("s4", "Contas", "Money and investments"), ("s5", "Lançamento", "Launch plan"),
             ("s6", "Scripts", "Code experiments"), ("s7", "Textos", "Posts and emails"),
             ("s8", "Cursos", "Courses and notes")]
    return [{"id": i, "name": n, "description": d, "createdAt": 1} for i, n, d in names]


class RecordingOpenDots:
    """An OpenDots stand-in that records every write and validates it like the server."""

    def __init__(self, token: str = TOKEN, model_config: dict | None = None,
                 voice: bool = True):
        self.token = token
        self.model = FakeModelConfig(model_config)
        self.dots = sample_dots()
        self.spaces = sample_spaces()
        self.settings = {"name": "Aurora", "paused": False, "researchAllowed": True,
                         "memoryAllowed": True}
        self.memories = [{"id": f"m{i}", "text": f"Memory {i}", "createdAt": i}
                         for i in range(21)]
        self.voice = voice
        self.connections: list[dict] | None = [sample_connection()]   # None: an older server
        # The fork's voice per Dot (GET /api/dot-voices); None: a server without it.
        self.dot_voices: dict | None = {"voices": {"d1": "onyx"}, "default": "marin",
                                        "available": ["marin", "cedar", "onyx", "nova"]}
        self.requests: list[tuple[str, str, object]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, data) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token."})
                    return False
                return True

            def _body(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                try:
                    return json.loads(raw.decode() or "null")
                except ValueError:
                    return None

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return
                if self.path == "/api/state":
                    self._json(200, {"settings": fake.settings, "tasks": [],
                                     "memories": fake.memories, "mode": "live",
                                     "configured": True})
                elif self.path == "/api/workspace":
                    self._json(200, {"spaces": fake.spaces, "dots": fake.dots,
                                     "conversations": [], "calls": [],
                                     "setup": {"voice": fake.voice, "missing": []},
                                     **fake.model.workspace_fields()})
                elif self.path == "/api/model-config":
                    self._json(*fake.model.get())
                elif self.path == "/api/dot-voices" and fake.dot_voices is not None:
                    self._json(200, fake.dot_voices)
                elif fake.connections is not None and self.path.startswith("/api/dots/") \
                        and self.path.endswith("/connections"):
                    dot_id = self.path.split("/")[3]
                    self._json(200, [c for c in fake.connections if c["dotId"] == dot_id])
                else:
                    self._json(404, {"error": "Not found."})

            def _connection(self):
                """The connection of /api/connections/<id>/…, or None after a 400."""
                parts = self.path.split("/")
                found = next((c for c in fake.connections or ()
                              if len(parts) > 3 and c["id"] == parts[3]), None)
                if found is None:
                    self._json(400, {"error": "Connection not found."})
                return found

            def do_DELETE(self):  # noqa: N802
                body = self._body()
                fake.requests.append(("DELETE", self.path, body))
                if not self._authorized():
                    return
                connection = self._connection()
                if connection is not None:
                    fake.connections.remove(connection)
                    self._json(200, {"ok": True})

            def do_PUT(self):  # noqa: N802
                body = self._body()
                fake.requests.append(("PUT", self.path, body))
                if not self._authorized():
                    return
                if self.path.endswith("/voice"):
                    voices = fake.dot_voices
                    voice = body.get("voice", "?") if isinstance(body, dict) else "?"
                    if voices is None:
                        self._json(404, {"error": "Not found."})
                    elif set(body) != {"voice"} or (voice is not None
                                                    and voice not in voices["available"]):
                        self._json(400, {"error": f"Unknown voice: {voice}."})
                    else:
                        dot_id = self.path.split("/")[3]
                        if voice is None:
                            voices["voices"].pop(dot_id, None)
                        else:
                            voices["voices"][dot_id] = voice
                        self._json(200, voices)
                    return
                dot_id = self.path.rsplit("/", 1)[-1]
                dot = next((d for d in fake.dots if d["id"] == dot_id), None)
                required = {"name", "instructions", "researchAllowed", "memoryAllowed"}
                if dot is None:
                    self._json(404, {"error": "Specialist not found."})
                elif not isinstance(body, dict) or set(body) - DOT_KEYS or required - set(body) \
                        or not 3 <= len(body["instructions"]) <= 2000 \
                        or not 1 <= len(body["name"]) <= 40:
                    self._json(400, {"error": "Invalid specialist settings."})
                else:
                    dot.update(body)
                    self._json(200, dot)

            def do_PATCH(self):  # noqa: N802
                body = self._body()
                fake.requests.append(("PATCH", self.path, body))
                if not self._authorized():
                    return
                if self.path.startswith("/api/connections/"):
                    connection = self._connection()
                    tool = next((t for t in connection["tools"]
                                 if t["name"] == self.path.rsplit("/", 1)[-1]), None) \
                        if connection else None
                    if connection is None:
                        return
                    if tool is None or not isinstance(body, dict) or not body \
                            or set(body) - {"enabled", "requiresApproval"}:
                        self._json(400, {"error": "Connection tool not found."})
                        return
                    tool.update(body)
                    self._json(200, connection)
                    return
                if self.path != "/api/settings" or not isinstance(body, dict) \
                        or set(body) - SETTINGS_KEYS:
                    self._json(400, {"error": "Invalid settings."})
                    return
                fake.settings.update(body)
                self._json(200, fake.settings)

            def do_POST(self):  # noqa: N802
                body = self._body()
                fake.requests.append(("POST", self.path, body))
                if not self._authorized():
                    return
                if self.path == "/api/model-config":
                    self._json(*fake.model.post(body))
                elif self.path == "/api/model-config/test":
                    self._json(*fake.model.test(body))
                elif self.path.endswith("/refresh") and self.path.startswith("/api/connections/"):
                    connection = self._connection()
                    if connection is not None:
                        self._json(200, connection)
                elif self.path.startswith("/api/dots/") and self.path.endswith("/connections"):
                    if not isinstance(body, dict) or set(body) - {"name", "url", "token"} \
                            or not body.get("name") or not body.get("url", "").startswith("http"):
                        self._json(400, {"error": "Use an http(s) MCP endpoint without "
                                                  "embedded credentials."})
                        return
                    created = {**sample_connection(self.path.split("/")[3]),
                               "id": f"added-{len(fake.requests)}", "name": body["name"],
                               "url": body["url"], "hasToken": bool(body.get("token"))}
                    fake.connections.append(created)
                    self._json(201, created)
                else:
                    self._json(404, {"error": "Not found."})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def model_config(self) -> dict | None:
        """The server's model settings as they are now (tests may change them)."""
        return self.model.data

    def writes(self, method: str | None = None) -> list[tuple[str, str, object]]:
        return [r for r in self.requests if method is None or r[0] == method]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class FakeWebView:
    def __init__(self):
        self.bypassed = 0

    def reload_bypass_cache(self):
        self.bypassed += 1


class FakeView:
    def __init__(self, profile):
        self.profile = profile
        self.webview = FakeWebView()
        self.calls: list[tuple] = []

    def set_zoom(self, zoom):
        self.calls.append(("zoom", zoom))

    def open_memories(self, done=None):
        self.calls.append(("memories",))


class FakeWindow:
    def __init__(self):
        self.view = None
        self.opened: list[str] = []

    def open_external(self, uri):
        self.opened.append(uri)

    def toast(self, text, button=None, action=None):
        pass


class FakeApp:
    SHORTCUT_TITLES = {"show-window": "Show or Hide Dotdeck",
                       "toggle-mute": "Mute or Unmute the Microphone in a Call",
                       "push-to-talk": "Hold to Talk in a Call"}

    def __init__(self, name: str):
        self.settings = store.Settings(store.Path(tmp_path(f"settings-{name}.json")))
        self.profiles = store.ProfileStore(store.Path(tmp_path(f"profiles-{name}.json")))
        self.tokens = secrets.TokenStore()
        self.monitors: dict[str, monitor.ServerMonitor] = {}
        self.window = FakeWindow()
        self.shortcuts = None
        self.tray_host_present = False
        self.notifications: list[tuple] = []
        self.calls: list[tuple] = []
        self.cleared: list[str] = []
        self.removed: list[str] = []

    def change_setting(self, key, value):
        self.settings.set(key, value)

    def add_server(self, url: str, name: str = "Homelab", token: str = TOKEN):
        profile = self.profiles.add(name, url)
        self.profiles.set_active(profile.id)
        self.tokens.store(profile.id, profile.url, "test", token)
        mon = monitor.ServerMonitor(profile, lambda cb: cb(token))
        self.monitors[profile.id] = mon
        mon.start()
        return profile

    def status_line(self, profile_id):
        mon = self.monitors.get(profile_id)
        return "8 Dots" if mon and mon.status == "online" else "Connecting…"

    def save_profile(self, profile_id, url, name, token):
        self.calls.append(("save_profile", profile_id, url, name))
        return self.profiles.get(profile_id) or self.profiles.add(name, url)

    def remove_profile(self, profile_id):
        self.removed.append(profile_id)
        self.profiles.remove(profile_id)

    def clear_website_data(self, profile_id, done):
        self.cleared.append(profile_id)
        done()

    def reload_server(self, profile_id):
        self.calls.append(("reload_server", profile_id))

    def configure_shortcuts(self):
        self.calls.append(("configure_shortcuts",))

    def show_about(self, parent=None):
        self.calls.append(("show_about", parent))

    def show_shortcuts(self, parent=None):
        self.calls.append(("show_shortcuts", parent))

    def send_notification(self, notification_id, notification):
        self.notifications.append((notification_id, notification))

    def stop(self):
        for mon in self.monitors.values():
            mon.stop()


def descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


class SearchTests(unittest.TestCase):
    """The matcher alone (no display needed)."""

    def entries(self):
        return [
            search.Entry("appearance", "Appearance", "Style", "Follow the system",
                         "Style", ("theme", "dark mode")),
            search.Entry("voice", "Voice Calls", "Noise Suppression", "Softens fans",
                         "Sound Processing", ("noise",)),
            search.Entry("notifications", "Notifications", "Desktop Notifications",
                         "Shown while the window is not in focus"),
            search.Entry("notifications", "Notifications", "Notifications", is_page=True),
            search.Entry("general", "General", "Language", "Dotdeck follows the system "
                         "language", "Language", ("idioma", "português")),
        ]

    def test_title_matches_rank_first(self):
        found = search.search(self.entries(), "notif")
        self.assertEqual([e.title for e in found][:2], ["Notifications",
                                                         "Desktop Notifications"])

    def test_every_word_must_match_and_accents_do_not_matter(self):
        self.assertEqual([e.title for e in search.search(self.entries(), "dark mode")],
                         ["Style"])
        self.assertEqual([e.title for e in search.search(self.entries(), "portugues")],
                         ["Language"])
        self.assertEqual(search.search(self.entries(), "dark kitten"), [])
        self.assertEqual(search.search(self.entries(), "   "), [])

    def test_fold(self):
        self.assertEqual(search.fold("Notificação ÉCHO"), "notificacao echo")


class JobLineTests(unittest.TestCase):
    def test_split_and_join(self):
        from src.ui.settings.dots import join_job, split_job
        text = "# Manutenção — backups e o servidor de casa\n\nKeep the home server healthy."
        self.assertEqual(split_job("Manutenção", text),
                         ("backups e o servidor de casa", "Keep the home server healthy."))
        self.assertEqual(split_job("Other", text), ("", text))
        self.assertEqual(join_job("Upkeep", "Keep it.", "SRE"), "# Upkeep — SRE\n\nKeep it.")
        self.assertEqual(join_job("Upkeep", "Keep it.", "  "), "Keep it.")
        self.assertEqual(join_job("Upkeep", "# Notes — misc\nKeep it.", "SRE"),
                         "# Upkeep — SRE\n\n# Notes — misc\nKeep it.")


class PlainTextTests(unittest.TestCase):
    def test_markup_is_searched_as_the_visible_text(self):
        from src.ui.settings.window import _plain
        self.assertEqual(_plain("R&amp;D &lt;Lab&gt; chief&#39;s"), "R&D <Lab> chief's")
        self.assertEqual(_plain("Plain words"), "Plain words")
        self.assertEqual(_plain("broken & markup"), "broken & markup")

    def test_language_follows_the_loaded_translation(self):
        from src.ui.settings.app_pages import current_language

        class Catalog:
            def __init__(self, language):
                self.language = language

            def info(self):
                return {"language": self.language} if self.language else {}

        self.assertEqual(current_language(Catalog("pt_BR")), "Português (Brasil)")
        self.assertEqual(current_language(Catalog("es")), "Español")
        self.assertEqual(current_language(Catalog("")), "English")       # no catalog
        self.assertEqual(current_language(object()), "English")


@unittest.skipUnless(Gtk.init_check(), "no display available")
class SettingsWindowTests(unittest.TestCase):
    model_config: dict | None = GATEWAY_CONFIG

    def setUp(self):
        Adw.init()
        self.fake = RecordingOpenDots(model_config=self.model_config)
        self.addCleanup(self.fake.stop)
        self.app = FakeApp(self.id())
        self.addCleanup(self.app.stop)
        self.profile = self.app.add_server(self.fake.url)
        self.app.window.view = FakeView(self.profile)
        self.mon = self.app.monitors[self.profile.id]
        self.assertTrue(run_until(lambda: self.mon.status == "online"
                                  and len(self.mon.workspace.dots) == 8, 10))

    def tearDown(self):
        from src.ui.settings import window
        win = window.current()
        if win is not None:
            win._shutdown()
            win.destroy()
        run_until(lambda: False, 0.05)

    def open(self, page="general", **kwargs):
        from src.ui.settings import get_window, open_settings
        open_settings(self.app, page, **kwargs)
        win = get_window()
        self.assertIsNotNone(win)
        run_until(lambda: win.get_mapped(), 5)
        return win

    def wait_model(self, win, state="ok"):
        self.assertTrue(run_until(lambda: win.model_config()["state"] == state, 10),
                        win.model_config())

    # -- window ----------------------------------------------------------------------------

    def test_every_page_opens_in_one_window(self):
        from src.ui.settings import PAGES
        first = self.open()
        for name in PAGES:
            win = self.open(name)
            self.assertIs(win, first)
            self.assertEqual(win.visible_page, name)
            self.assertEqual(win.title_widget.get_title(), win.pages[name].title)
            self.assertEqual(self.app.settings.get("settings-page"), name)
        self.assertEqual(first.get_default_size(), (980, 760))

    def test_a_narrow_window_shows_the_page_asked_for(self):
        """Edit Dot, Microphone… or a link to another page: the page, not the list."""
        win = self.open("general")
        win.set_default_size(360, 720)
        self.assertTrue(run_until(win.split.get_collapsed, 5))
        win.split.set_show_content(False)
        win.show_page("voice")
        self.assertTrue(win.split.get_show_content())
        self.assertEqual(win.visible_page, "voice")

    def test_a_narrow_page_is_only_as_wide_as_itself(self):
        """At 360 px the pages fit: none takes the width of the widest one (Appearance)."""
        win = self.open("general")
        win.set_default_size(360, 720)
        self.assertTrue(run_until(win.split.get_collapsed, 5))
        widest = max(page.widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
                     for page in win.pages.values())
        for name in ("general", "servers"):
            win.show_page(name)
            run_until(lambda: False, 0.2)
            page_min = win.pages[name].widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            self.assertLess(page_min, widest)
            self.assertEqual(win.stack.measure(Gtk.Orientation.HORIZONTAL, -1)[0], page_min)
            self.assertLessEqual(page_min, win.stack.get_width())

    def test_reopens_on_the_last_page(self):
        self.app.settings.set("settings-page", "privacy")
        win = self.open(None)
        self.assertEqual(win.visible_page, "privacy")

    def test_search_finds_a_row_and_points_at_it(self):
        win = self.open("general")
        win.search_entry.set_text("noise")
        self.assertTrue(run_until(lambda: win.search_results(), 3))
        first = win.search_results()[0]
        self.assertEqual((first.page, first.title), ("voice", "Noise Suppression"))
        self.assertEqual(win.sidebar_stack.get_visible_child_name(), "results")
        win.search_entry.emit("activate")
        self.assertTrue(run_until(lambda: win.visible_page == "voice", 2))
        self.assertTrue(run_until(lambda: first.target.has_css_class("settings-highlight"), 2))
        win.search_entry.set_text("zzzz-nothing")
        self.assertTrue(run_until(
            lambda: win.sidebar_stack.get_visible_child_name() == "empty", 3))
        win.search_entry.set_text("")
        self.assertTrue(run_until(
            lambda: win.sidebar_stack.get_visible_child_name() == "pages", 3))

    def test_search_uses_keywords_and_dot_names(self):
        win = self.open()
        win.search_entry.set_text("dark mode")
        self.assertTrue(run_until(lambda: win.search_results(), 3))
        self.assertEqual(win.search_results()[0].page, "appearance")
        win.search_entry.set_text("manutencao")
        self.assertTrue(run_until(lambda: any(e.title == "Manutenção"
                                              for e in win.search_results()), 3))

    # -- bound settings --------------------------------------------------------------------

    def test_rows_write_settings_and_follow_outside_changes(self):
        win = self.open("general")
        win.rows_by_key["tray-icon"].set_active(False)
        self.assertFalse(self.app.settings.get("tray-icon"))
        win.rows_by_key["color-scheme"].set_active_name("dark")
        self.assertEqual(self.app.settings.get("color-scheme"), "dark")
        win.rows_by_key["mic-mode"].set_active_name("push-to-talk")
        self.assertEqual(self.app.settings.get("mic-mode"), "push-to-talk")
        zoom = win.rows_by_key["zoom"]
        from src.ui.settings.app_pages import ZOOM_STEPS
        zoom.set_selected(ZOOM_STEPS.index(1.25))
        self.assertEqual(self.app.settings.get("zoom"), 1.25)
        self.assertIn(("zoom", 1.25), self.app.window.view.calls)
        self.app.settings.set("run-in-background", False)
        self.assertFalse(win.rows_by_key["run-in-background"].get_active())
        self.app.settings.set("zoom", 0.8)
        self.assertEqual(zoom.get_selected(), ZOOM_STEPS.index(0.8))
        win.rows_by_key["notifications"].set_active(False)
        self.assertFalse(win.pages["notifications"].events.get_sensitive())

    def test_test_notification_is_sent(self):
        win = self.open("notifications")
        win.pages["notifications"].send_test()
        self.assertEqual(self.app.notifications[0][0], "settings-test")

    def test_reset_restores_defaults_but_keeps_the_window_size(self):
        win = self.open("advanced")
        changed = {"color-scheme": "dark", "tray-icon": False, "zoom": 1.5, "density": "compact",
                   "notification-content": "private", "mic-mode": "push-to-talk",
                   "window-width": 1500, "window-maximized": True}
        for key, value in changed.items():
            self.app.settings.set(key, value)
        win.pages["advanced"].reset()
        for key, default in store.Settings.DEFAULTS.items():
            if key in ("window-width", "window-height", "window-maximized"):
                continue
            self.assertEqual(self.app.settings.get(key), default, key)
        self.assertEqual(self.app.settings.get("window-width"), 1500)
        self.assertTrue(self.app.settings.get("window-maximized"))
        self.assertTrue(win.rows_by_key["tray-icon"].get_active())

    def test_system_report_has_the_self_test_facts(self):
        win = self.open("advanced")
        report = json.loads(win.pages["advanced"].copy_report())
        for key in ("version", "python", "gtk", "libadwaita", "webkitgtk", "gstreamer",
                    "voice_engine", "keyring", "sandbox"):
            self.assertIn(key, report)

    # -- Dots ------------------------------------------------------------------------------

    def editor(self, win, dot_id):
        key = (self.profile.id, dot_id)
        self.assertTrue(run_until(lambda: key in win.editors and
                                  win.nav.get_visible_page().get_tag() == f"dot-{dot_id}", 5))
        return win.editors[key]

    def test_dots_page_lists_every_dot_with_its_job(self):
        win = self.open("dots")
        page = win.pages["dots"]
        self.assertTrue(run_until(lambda: len(page._rows) == 8, 5))
        titles = [row.get_title() for row in page._rows]
        self.assertIn("Manutenção", titles)
        upkeep = page._rows[titles.index("Manutenção")]
        self.assertEqual(upkeep.get_subtitle(), "backups e o servidor de casa")

    def test_dot_editor_sends_the_full_body(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        self.assertEqual(editor.name_row.get_text(), "Manutenção")
        self.assertEqual(editor.job_row.get_text(), "backups e o servidor de casa")
        self.assertFalse(editor.dirty)
        editor.name_row.set_text("Upkeep")
        editor.job_row.set_text("keeps the home server healthy")
        editor.buffer.set_text("Keep the servers healthy.")
        editor.research_row.set_active(False)
        self.assertTrue(editor.dirty)
        self.assertTrue(editor.bar.get_revealed())
        results = []
        editor.save(results.append)
        self.assertTrue(run_until(lambda: results, 5))
        self.assertEqual(results, [True])
        method, path, body = self.fake.writes("PUT")[-1]
        self.assertEqual((method, path), ("PUT", "/api/dots/d2"))
        self.assertEqual(body, {
            "name": "Upkeep", "instructions": "# Upkeep — keeps the home server healthy\n\n"
            "Keep the servers "
            "healthy.", "researchAllowed": False, "memoryAllowed": True, "spaceId": "s3",
            "spaceIds": ["s3"], "learningContainerId": None, "skillDeliveryEnabled": True})
        self.assertFalse(editor.dirty)
        self.assertFalse(editor.bar.get_revealed())

    def test_untouched_instructions_are_sent_exactly(self):
        original = "#  Laboratório - código, scripts e automações\nPrototype small tools."
        self.fake.dots[5]["instructions"] = original
        self.mon.poll_now()
        self.assertTrue(run_until(lambda: self.mon.workspace.dot("d5").instructions
                                  == original, 5))
        win = self.open("dots", dot="d5")
        editor = self.editor(win, "d5")
        self.assertEqual(editor.job_row.get_text(), "código, scripts e automações")
        editor.memory_row.set_active(False)
        done = []
        editor.save(done.append)
        self.assertTrue(run_until(lambda: done, 5))
        body = self.fake.writes("PUT")[-1][2]
        self.assertEqual(body["instructions"], original)
        self.assertFalse(body["memoryAllowed"])

    def test_dot_editor_spaces_and_limits(self):
        win = self.open("dots", dot="d1")
        editor = self.editor(win, "d1")
        self.assertTrue(editor.spaces.get_visible())
        self.assertEqual(editor._checked_spaces(), ["s1", "s2"])
        editor._space_checks["s1"].set_active(False)
        self.assertEqual(editor.changes()["spaceIds"], ["s2"])
        self.assertEqual(editor.changes()["spaceId"], "s2")
        editor._space_checks["s2"].set_active(False)     # the last one stays
        self.assertEqual(editor._checked_spaces(), ["s2"])
        editor.buffer.set_text("x" * 2100)
        self.assertTrue(editor.counter.has_css_class("error"))
        self.assertFalse(editor.save_button.get_sensitive())
        editor.discard()
        self.assertFalse(editor.dirty)
        self.assertEqual(editor._checked_spaces(), ["s1", "s2"])

    def test_server_errors_are_shown(self):
        win = self.open("dots", dot="d1")
        editor = self.editor(win, "d1")
        editor.name_row.set_text("N" * 40)
        self.fake.token = "rotated"
        done = []
        editor.save(done.append)
        self.assertTrue(run_until(lambda: done, 5))
        self.assertEqual(done, [False])
        self.assertTrue(editor.banner.get_revealed())
        self.assertIn("owner access token", editor.banner.get_title())
        self.assertTrue(editor.dirty)

    def test_drafts_survive_server_refreshes(self):
        win = self.open("dots", dot="d1")
        editor = self.editor(win, "d1")
        editor.name_row.set_text("Chief of Staff")
        self.fake.dots[1]["instructions"] = "# Assistente — novo cargo\n\nChanged in the browser."
        self.mon.poll_now()
        self.assertTrue(run_until(lambda: editor.banner.get_revealed(), 8))
        self.assertEqual(editor.name_row.get_text(), "Chief of Staff")
        self.assertTrue(editor.dirty)
        editor._on_banner()      # "Show Theirs"
        self.assertEqual(editor.job_row.get_text(), "novo cargo")
        self.assertFalse(editor.dirty)

    def test_local_choices_apply_at_once(self):
        from src.core import dotprefs
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        editor.chief_row.set_active(True)
        self.assertEqual(self.app.profiles.get(self.profile.id).chief, "Manutenção")
        editor.pin_row.set_active(True)
        self.assertTrue(dotprefs.for_profile(self.profile.id).is_pinned("d2"))
        self.assertEqual(self.app.window.view.calls, [])     # the page's form stays closed

    # -- Automatic Learning and Connections (ADR-023) --------------------------------------

    def connections(self, editor, count=1):
        group = editor.connections
        self.assertTrue(run_until(lambda: len(group._rows) == count, 5),
                        group.status.get_title())
        return group

    def tool_rows(self, row) -> dict:
        return {r.tool_name: r for r in descendants(row) if isinstance(r, Adw.ComboRow)}

    def test_learning_is_edited_with_the_rest_of_the_dot(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        self.assertTrue(editor.learning.get_visible())
        self.assertEqual(editor.container_row.get_text(), "")
        self.assertFalse(editor.skills_row.get_sensitive())      # no container yet
        editor.container_row.set_text("Home Lab")
        self.assertTrue(editor.container_row.has_css_class("error"))
        self.assertFalse(editor.save_button.get_sensitive())
        self.assertIn("lowercase", editor.bar_label.get_label())
        editor.container_row.set_text("home-lab")
        self.assertFalse(editor.container_row.has_css_class("error"))
        self.assertTrue(editor.skills_row.get_sensitive())
        editor.skills_row.set_active(True)
        done = []
        editor.save(done.append)
        self.assertTrue(run_until(lambda: done, 5))
        self.assertEqual(done, [True])
        body = self.fake.writes("PUT")[-1][2]
        self.assertEqual((body["learningContainerId"], body["skillDeliveryEnabled"]),
                         ("home-lab", True))
        editor.container_row.set_text("")          # no container: no skills either
        self.assertFalse(editor.skills_row.get_active())
        self.assertEqual(editor.changes()["learningContainerId"], None)
        self.assertFalse(editor.changes()["skillDeliveryEnabled"])

    def test_learning_hides_on_servers_without_it(self):
        for dot in self.fake.dots:
            del dot["learningContainerId"], dot["skillDeliveryEnabled"]
        self.mon.poll_now()
        self.assertTrue(run_until(lambda: not self.mon.workspace.dot("d2").has_learning, 5))
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        self.assertFalse(editor.learning.get_visible())
        editor.memory_row.set_active(False)
        self.assertNotIn("learningContainerId", editor.changes())

    def test_each_tool_is_allowed_asks_first_or_is_off(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        group = self.connections(editor)
        row = group._rows[0]
        self.assertEqual(row.get_title(), "Homelab")
        self.assertEqual(row.get_subtitle(), "mcp.home.test · 2 of 3 tools on · token saved")
        tools = self.tool_rows(row)
        self.assertEqual({name: r.get_selected() for name, r in tools.items()},
                         {"list_disks": 0, "restart_service": 1, "wipe_disk": 2})
        tools["wipe_disk"].set_selected(0)                        # Blocked → Allowed
        self.assertTrue(run_until(lambda: self.fake.writes("PATCH"), 5))
        self.assertEqual(self.fake.writes("PATCH")[-1][1:], (
            "/api/connections/c1/tools/wipe_disk", {"enabled": True, "requiresApproval": False}))
        self.assertTrue(run_until(lambda: "3 of 3" in row.get_subtitle(), 5))
        tools["restart_service"].set_selected(2)                  # Ask First → Blocked
        self.assertTrue(run_until(lambda: len(self.fake.writes("PATCH")) == 2, 5))
        self.assertEqual(self.fake.writes("PATCH")[-1][2], {"enabled": False})
        group.refresh("c1")
        self.assertTrue(run_until(lambda: self.fake.writes("POST"), 5))
        self.assertEqual(self.fake.writes("POST")[-1][1], "/api/connections/c1/refresh")
        self.assertFalse(editor.dirty)                            # nothing waits for Save
        self.assertEqual(self.fake.writes("PUT"), [])

    def test_a_tool_the_server_did_not_change_goes_back(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        group = self.connections(editor)
        tools = self.tool_rows(group._rows[0])
        self.fake.token = "rotated"
        tools["list_disks"].set_selected(1)
        self.assertTrue(run_until(lambda: tools["list_disks"].get_selected() == 0, 5))
        self.assertTrue(tools["list_disks"].get_sensitive())

    def test_add_and_remove_an_mcp_server(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        group = self.connections(editor)
        dialog = group.add_server()
        self.assertFalse(dialog.connect_button.get_sensitive())
        dialog.name_row.set_text("  GitHub ")
        dialog.url_row.set_text("ftp://example.test")
        self.assertTrue(dialog.url_row.has_css_class("error"))
        self.assertFalse(dialog.connect_button.get_sensitive())
        dialog.url_row.set_text("https://user:pw@example.test/mcp")
        self.assertIn("token", dialog.problem())
        dialog.url_row.set_text("https://mcp.github.test/mcp")
        dialog.token_row.set_text("ghp-secret-token")
        self.assertTrue(dialog.connect_button.get_sensitive())
        dialog.submit()
        self.assertTrue(run_until(lambda: len(group._rows) == 2, 5))
        method, path, body = self.fake.writes("POST")[-1]
        self.assertEqual(path, "/api/dots/d2/connections")
        self.assertEqual(body, {"name": "GitHub", "url": "https://mcp.github.test/mcp",
                                "token": "ghp-secret-token"})
        self.assertEqual(dialog.token_row.get_text(), "")
        added = group.connections[-1]
        self.assertTrue(group._rows[-1].get_expanded())
        group.remove_server(added.id)
        self.assertTrue(run_until(lambda: isinstance(win.get_visible_dialog(),
                                                     Adw.AlertDialog), 3))
        alert = win.get_visible_dialog()
        alert.emit("response", "ok")
        alert.force_close()
        self.assertTrue(run_until(lambda: len(group._rows) == 1, 5))
        self.assertEqual(self.fake.writes("DELETE")[-1][1], f"/api/connections/{added.id}")

    def test_no_connections_and_older_servers(self):
        self.fake.connections = []
        win = self.open("dots", dot="d2")
        group = self.editor(win, "d2").connections
        self.assertTrue(run_until(lambda: group.status.get_title() == "No Connections Yet", 5))
        self.assertTrue(group.add_button.get_visible())
        self.fake.connections = None                             # before upstream #52
        group.load()
        self.assertTrue(run_until(lambda: group.status.get_title()
                                  == "Not on This Server Yet", 5))
        self.assertFalse(group.add_button.get_visible())

    # -- the voice of each Dot -------------------------------------------------------------

    def test_each_dot_has_its_own_call_voice(self):
        win = self.open("dots", dot="d1")
        group = self.editor(win, "d1").voice
        self.assertTrue(run_until(group.get_visible, 5))
        self.assertEqual(group._choices, [None, "marin", "cedar", "onyx", "nova"])
        self.assertEqual(group.row.get_selected(), 3)               # Onyx, saved before
        labels = group.row.get_model()
        self.assertEqual(labels.get_string(0), "Server voice (Marin)")
        self.assertEqual(labels.get_string(3), "Onyx — deep and authoritative")
        group.row.set_selected(4)
        self.assertTrue(run_until(lambda: self.fake.writes("PUT"), 5))
        self.assertEqual(self.fake.writes("PUT")[-1][1:], ("/api/dots/d1/voice",
                                                           {"voice": "nova"}))
        self.assertTrue(run_until(group.row.get_sensitive, 5))
        group.row.set_selected(0)                                    # back to the server's
        self.assertTrue(run_until(lambda: self.fake.writes("PUT")[-1][2] == {"voice": None},
                                  5))
        self.assertTrue(run_until(lambda: group.row.get_subtitle()
                                  == "Dots without their own voice use Marin", 5))
        # Nothing else of the Dot was sent: the voice is not part of its Save.
        self.assertFalse([w for w in self.fake.writes("PUT") if w[1] == "/api/dots/d1"])

    def test_no_call_voice_row_on_an_older_server(self):
        self.fake.dot_voices = None
        win = self.open("dots", dot="d2")
        group = self.editor(win, "d2").voice
        run_until(lambda: False, 0.5)
        self.assertFalse(group.get_visible())

    # -- model and voice -------------------------------------------------------------------

    def test_model_page_switches_the_model(self):
        win = self.open("model")
        self.wait_model(win)
        page = win.pages["model"]
        self.assertTrue(page.change.get_visible())
        self.assertFalse(page.problem.get_visible())
        self.assertIn("deepseek", page.model_row.get_subtitle())
        self.assertEqual(page.key_row.get_text(), "")
        page.combo.set_selected(page._choices.index("anthropic/claude-sonnet-5.5"))
        self.assertTrue(page.apply.get_sensitive())
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: self.fake.writes("POST"), 5))
        self.assertEqual(self.fake.writes("POST")[-1][2], {"model": "anthropic/claude-sonnet-5.5"})
        self.assertTrue(run_until(lambda: "claude" in page.model_row.get_subtitle(), 5))

    def test_api_key_is_sent_only_when_typed(self):
        win = self.open("model")
        self.wait_model(win)
        page = win.pages["model"]
        page.key_row.emit("apply")
        run_until(lambda: False, 0.3)
        self.assertEqual(self.fake.writes("POST"), [])
        page.key_row.set_text("sk-or-test-key")
        page.key_row.emit("apply")
        self.assertTrue(run_until(lambda: self.fake.writes("POST"), 5))
        self.assertEqual(self.fake.writes("POST")[-1][2], {"apiKey": "sk-or-test-key"})
        self.assertTrue(run_until(lambda: page.key_row.get_text() == "", 5))

    def test_voice_page_never_offers_a_key_for_gateway_models(self):
        win = self.open("voice")
        self.wait_model(win)
        page = win.pages["voice"]
        self.assertFalse(any(isinstance(w, Adw.PasswordEntryRow)
                             for w in descendants(page.widget)))
        timbre = next(row for row in page._voice_rows if isinstance(row, Adw.ComboRow)
                      and row.get_title() == "Voice")
        timbre.set_selected(1)    # cedar
        self.assertTrue(run_until(lambda: self.fake.writes("POST"), 5))
        self.assertEqual(self.fake.writes("POST")[-1][2], {"voiceName": "cedar"})

    def test_voice_page_offers_a_key_for_direct_openai(self):
        self.fake.model_config.update(voiceModel="gpt-realtime", voiceGateway=False)
        win = self.open("voice")
        self.wait_model(win)
        page = win.pages["voice"]
        self.assertTrue(any(isinstance(w, Adw.PasswordEntryRow)
                            for w in descendants(page.widget)))
        speech = next(row for row in page._voice_rows if row.get_title() == "Speech")
        self.assertNotIsInstance(speech, Adw.ComboRow)     # gateway ids would not work here
        self.assertEqual(speech.get_subtitle(), "gpt-realtime · OpenAI Realtime")

    def test_a_0_3_2_server_shows_what_it_reports(self):
        """Older fork servers: one key, no provider menu, no price, no test, no transcription."""
        self.fake.model_config["hasApiKey"] = False
        win = self.open("model")
        self.wait_model(win)
        page = win.pages["model"]
        self.assertEqual(page.model_row.get_title(), "DeepSeek V4 Flash")
        self.assertEqual(page.model_row.get_subtitle(),
                         "deepseek/deepseek-v4-flash-0731 · OpenRouter")
        for row in (page.provider_combo, page.price_row, page.test_row, page.advanced,
                    page.new_key):
            self.assertFalse(row.get_visible(), row.get_title())
        self.assertEqual((page.key_status.get_title(), page.key_status.get_subtitle()),
                         ("OpenRouter Key", "No key yet"))
        self.assertEqual([row.get_title() for row in page._key_rows], ["OpenRouter"])
        self.assertEqual(page.key_row.get_title(), "Set a New OpenRouter Key")
        self.assertEqual(page.speech_row.get_subtitle(),
                         "openai/gpt-audio-mini · Dotdeck voice gateway")
        self.assertEqual(page.transcribe_row.get_subtitle(), "This server does not report it")
        self.assertEqual(page.thinking_row.get_subtitle(), "DeepSeek V4 Flash · the chat model")
        # Ids go as typed: these servers check nothing per provider and ignore "provider".
        page.combo.set_selected(page._choices.index(""))
        page.custom.set_text("my-model")
        self.assertTrue(page.apply.get_sensitive())
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {"model": "my-model"})
        voice = win.pages["voice"]
        self.assertTrue(run_until(lambda: any(r.get_title() == "Transcription"
                                              for r in voice._voice_rows), 5))

    def test_audio_devices_are_listed_and_chosen(self):
        from unittest import mock

        from src.core import voice
        if not hasattr(voice, "list_devices"):
            self.skipTest("no device listing in src.core.voice")
        Device = getattr(voice, "AudioDevice", None) or (lambda i, n: type(
            "D", (), {"id": i, "name": n})())
        mics = [Device("alsa_input.usb-headset", "USB Headset"),
                Device("alsa_input.pci-analog", "Built-in Audio")]

        def fake_async(kind, callback):
            callback(mics if kind == "source" else [])

        with mock.patch.object(voice, "list_devices", lambda kind: mics if kind == "source"
                               else [], create=True), \
                mock.patch.object(voice, "list_devices_async", fake_async, create=True):
            win = self.open("voice")
            combo = win.pages["voice"].mic_combo
            self.assertTrue(run_until(lambda: combo.get_model().get_n_items() == 3, 3))
            self.assertEqual(combo.get_model().get_string(0), "System Default")
            combo.set_selected(1)
            self.assertEqual(self.app.settings.get("microphone-device"),
                             "alsa_input.usb-headset")
            run_until(lambda: False, 0.2)
            self.assertEqual(combo.get_selected(), 1)
            self.app.settings.set("microphone-device", "unplugged-mic")
            self.assertTrue(run_until(lambda: combo.get_model().get_n_items() == 4, 3))
            self.assertIn("not connected", combo.get_model().get_string(3))

    def test_microphone_permission_per_server(self):
        win = self.open("voice")
        page = win.pages["voice"]
        page.permission.set_selected(2)
        self.assertEqual(self.app.profiles.get(self.profile.id).microphone, store.MIC_DENY)

    def test_call_sounds_switch(self):
        self.assertTrue(store.Settings.DEFAULTS["call-sounds"])
        win = self.open("voice")
        row = win.rows_by_key["call-sounds"]
        self.assertEqual(row.get_title(), "Call Sounds")
        self.assertTrue(row.get_active())
        row.set_active(False)
        self.assertFalse(self.app.settings.get("call-sounds"))
        self.app.settings.set("call-sounds", True)
        self.assertTrue(row.get_active())
        win.search_entry.set_text("ringtone")
        self.assertTrue(run_until(lambda: win.search_results(), 3))
        first = win.search_results()[0]
        self.assertEqual((first.page, first.title), ("voice", "Call Sounds"))

    # -- server pages ----------------------------------------------------------------------

    def test_privacy_switch_asks_then_patches(self):
        win = self.open("privacy")
        page = win.pages["privacy"]
        self.assertTrue(page.research.get_active())
        page.research.set_active(False)
        self.assertTrue(run_until(lambda: isinstance(win.get_visible_dialog(),
                                                     Adw.AlertDialog), 3))
        self.assertEqual(self.fake.writes("PATCH"), [])      # nothing before the answer
        dialog = win.get_visible_dialog()
        dialog.emit("response", "cancel")
        dialog.force_close()
        self.assertTrue(run_until(lambda: page.research.get_active(), 3))
        page.research.set_active(False)
        self.assertTrue(run_until(lambda: isinstance(win.get_visible_dialog(),
                                                     Adw.AlertDialog), 3))
        dialog = win.get_visible_dialog()
        dialog.emit("response", "ok")
        dialog.force_close()
        self.assertTrue(run_until(lambda: self.fake.writes("PATCH"), 5))
        self.assertEqual(self.fake.writes("PATCH")[-1][1:], ("/api/settings",
                                                             {"researchAllowed": False}))
        self.assertTrue(run_until(lambda: not page.research.get_active(), 5))
        self.assertIn("21", page.memories_row.get_subtitle())
        page.manage.emit("clicked")
        self.assertIn(("memories",), self.app.window.view.calls)

    def test_server_page_opens_for_editing(self):
        win = self.open("servers", edit=self.profile.id)
        self.assertTrue(run_until(
            lambda: win.nav.get_visible_page().get_tag() == f"server-{self.profile.id}", 3))
        win.pop_to_root()
        win.pages["servers"].add_server()
        self.assertEqual(win.nav.get_visible_page().get_tag(), "add-server")

    def test_unsaved_dot_asks_before_closing(self):
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        editor.name_row.set_text("Upkeep")
        win.close()
        self.assertTrue(run_until(lambda: isinstance(win.get_visible_dialog(),
                                                     Adw.AlertDialog), 3))
        from src.ui.settings import get_window
        self.assertIs(get_window(), win)                  # still open
        dialog = win.get_visible_dialog()
        dialog.emit("response", "discard")
        dialog.force_close()
        self.assertTrue(run_until(lambda: get_window() is None, 3))
        self.assertEqual(self.fake.writes("PUT"), [])

    def test_save_from_the_close_prompt(self):
        from src.ui.settings import get_window
        win = self.open("dots", dot="d2")
        editor = self.editor(win, "d2")
        editor.job_row.set_text("guardian of the home server")
        win.close()
        self.assertTrue(run_until(lambda: isinstance(win.get_visible_dialog(),
                                                     Adw.AlertDialog), 3))
        dialog = win.get_visible_dialog()
        dialog.emit("response", "save")
        dialog.force_close()
        self.assertTrue(run_until(lambda: get_window() is None, 5))
        body = self.fake.writes("PUT")[-1][2]
        self.assertTrue(body["instructions"].startswith(
            "# Manutenção — guardian of the home server"))

    def test_escape_steps_back_then_closes(self):
        from src.ui.settings import get_window
        win = self.open("dots", dot="d2")
        self.editor(win, "d2")
        win._on_escape()
        self.assertTrue(run_until(lambda: win.nav.get_visible_page() is win.root_page, 3))
        win.search_entry.set_text("zoom")
        win._on_escape()
        self.assertEqual(win.search_entry.get_text(), "")
        win._on_escape()
        self.assertTrue(run_until(lambda: get_window() is None, 3))

    def test_about_page(self):
        win = self.open("about")
        page = win.pages["about"]
        page._about()
        self.assertEqual(self.app.calls[-1], ("show_about", win))
        self.assertTrue(page.notes)
        dialog = page.show_notes()
        self.assertIsInstance(dialog, Adw.Dialog)
        dialog.force_close()


    # -- review fixes ----------------------------------------------------------------------

    def test_search_works_with_markup_in_server_names_and_jobs(self):
        profile = self.app.profiles.get(self.profile.id)
        profile.name = "R&D <Lab>"
        self.app.profiles.update(profile)
        self.fake.dots[2]["instructions"] = "# Manutenção — Aurora's keeper\n\nKeep it healthy."
        self.mon.poll_now()
        self.assertTrue(run_until(lambda: "Aurora's" in self.mon.workspace.dot("d2").job, 5))
        errors = self.collect_errors()
        win = self.open("voice")
        self.assertTrue(run_until(lambda: "R&amp;D" in win.pages["voice"].status.get_title(), 3))
        self.assertTrue(run_until(lambda: len(win.pages["dots"]._rows) == 8, 5))
        win.search_entry.set_text("noise")
        self.assertTrue(run_until(lambda: win.search_results(), 3))
        self.assertEqual(win.search_results()[0].title, "Noise Suppression")
        win.search_entry.set_text("aurora's keeper")
        self.assertTrue(run_until(lambda: any(e.title == "Manutenção"
                                              for e in win.search_results()), 3))
        found = next(e for e in win.search_results() if e.title == "Manutenção")
        self.assertEqual(found.subtitle, "Aurora's keeper")
        self.assertEqual(errors, [])

    def collect_errors(self) -> list[str]:
        """Exceptions raised inside GTK callbacks (PyGObject only prints them)."""
        import sys
        import traceback
        errors: list[str] = []
        previous = sys.excepthook

        def hook(kind, value, tb):
            errors.append("".join(traceback.format_exception(kind, value, tb)))
            previous(kind, value, tb)

        sys.excepthook = hook
        self.addCleanup(setattr, sys, "excepthook", previous)
        return errors

    def test_dialogs_of_the_app_move_over_the_settings_window(self):
        host = Adw.Window(default_width=900, default_height=700)
        host.view = self.app.window.view
        host.present()
        self.addCleanup(host.destroy)
        self.app.window = host
        shown = []

        def show_about():          # like main.py: no parent, presents on the main window
            dialog = Adw.AboutDialog(application_name="Dotdeck")
            dialog.present(host)
            shown.append(dialog)

        self.app.show_about = show_about
        win = self.open("about")
        win.pages["about"]._about()
        self.assertTrue(shown)
        self.assertIs(win.get_visible_dialog(), shown[0])
        self.assertIsNone(host.get_visible_dialog())
        shown[0].force_close()

    def test_escape_in_an_empty_search_closes_the_subpage_first(self):
        from src.ui.settings import get_window
        win = self.open("dots", dot="d2")
        self.editor(win, "d2")
        win.search_entry.emit("stop-search")
        self.assertTrue(run_until(lambda: win.nav.get_visible_page() is win.root_page, 3))
        self.assertIs(get_window(), win)
        win.search_entry.set_text("zoom")
        win.search_entry.emit("stop-search")
        self.assertEqual(win.search_entry.get_text(), "")
        self.assertIs(get_window(), win)

    def test_down_from_the_search_field_goes_to_the_results(self):
        from gi.repository import Gdk
        win = self.open("general")
        win.search_entry.set_text("echo")
        self.assertTrue(run_until(lambda: win.search_results(), 3))
        self.assertTrue(win._on_search_key(None, Gdk.KEY_Down, 0, 0))
        self.assertIs(win.get_focus(), win.results.get_row_at_index(0))
        win.search_entry.set_text("")
        self.assertTrue(run_until(
            lambda: win.sidebar_stack.get_visible_child_name() == "pages", 3))
        self.assertFalse(win._on_search_key(None, Gdk.KEY_Down, 0, 0))

    def test_audio_devices_are_listed_off_the_main_thread(self):
        import threading
        from unittest import mock

        from src.core import voice
        if not hasattr(voice, "list_devices"):
            self.skipTest("no device listing in src.core.voice")
        threads = []
        mics = [voice.AudioDevice("alsa_input.usb", "USB Headset")]

        def listing(kind):
            threads.append(threading.current_thread() is threading.main_thread())
            return mics if kind == "source" else []

        def default(kind):
            threads.append(threading.current_thread() is threading.main_thread())
            return "Built-in Audio" if kind == "source" else None

        with mock.patch.object(voice, "list_devices", listing), \
                mock.patch.object(voice, "default_device_name", default):
            win = self.open("voice")
            page = win.pages["voice"]
            self.assertTrue(run_until(lambda: page.mic_combo.get_model().get_n_items() == 2,
                                      5))
            self.assertIn("Built-in Audio", page.mic_combo.get_subtitle())
            self.assertTrue(run_until(lambda: page._mic_test is not None, 3))
            page.mic_combo.set_selected(1)
            self.assertEqual(self.app.settings.get("microphone-device"), "alsa_input.usb")
            self.assertEqual(page.mic_combo.get_subtitle(), "")
            self.assertTrue(run_until(lambda: page._mic_test is not None, 3))
            self.assertEqual(page._mic_test.device_id, "alsa_input.usb")
        self.assertTrue(threads)
        self.assertNotIn(True, threads)        # never on the main thread
        win.show_page("general")
        self.assertIsNone(page._mic_test)      # the meter stops with the page

    def test_speaker_test_plays_the_chime(self):
        from src.core import voice
        if not hasattr(voice, "SpeakerTest"):
            self.skipTest("no speaker test in src.core.voice")
        win = self.open("voice")
        page = win.pages["voice"]
        page.play_test_sound()
        self.assertFalse(page.play_button.get_sensitive())
        self.assertTrue(run_until(lambda: page.play_button.get_sensitive(), 8))

    def test_shortcut_labels_follow_the_portal(self):
        from gi.repository import GObject

        class Portal(GObject.Object):
            __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
            available = True
            can_configure = False
            triggers: dict = {}

        portal = Portal()
        self.app.shortcuts = portal
        win = self.open("shortcuts")
        page = win.pages["shortcuts"]
        self.assertEqual(page._triggers["show-window"].get_label(), "Not set")
        self.assertFalse(page.change.get_visible())
        portal.triggers = {"show-window": "Super+Shift+D"}
        portal.can_configure = True
        portal.emit("changed")
        self.assertEqual(page._triggers["show-window"].get_label(), "Super+Shift+D")
        self.assertTrue(page.change.get_visible())
        page.change.emit("activated")
        self.assertIn(("configure_shortcuts",), self.app.calls)
        self.app.settings.set("global-shortcut", False)
        self.assertTrue(run_until(lambda: page._triggers["show-window"].get_label() == "Off",
                                  3))
        win._shutdown()
        portal.emit("changed")                  # disconnected: nothing happens


class NoServerTests(unittest.TestCase):
    """No server at all, then one that is offline: every page opens and says why."""

    def setUp(self):
        if not Gtk.init_check():
            self.skipTest("no display available")
        Adw.init()
        self.app = FakeApp(self.id())
        self.addCleanup(self.app.stop)
        self.errors = SettingsWindowTests.collect_errors(self)

    def tearDown(self):
        SettingsWindowTests.tearDown(self)

    def visit_every_page(self):
        from src.ui.settings import PAGES, get_window, open_settings
        for name in PAGES:
            open_settings(self.app, name)
            run_until(lambda: False, 0.05)
        win = get_window()
        for text in ("mic", "key", "server", "memories"):
            win.search_entry.set_text(text)
            run_until(lambda: False, 0.2)
            win.search_entry.emit("activate")
            run_until(lambda: False, 0.1)
        return win

    def test_without_a_server(self):
        win = self.visit_every_page()
        model = win.pages["model"]
        self.assertTrue(model.problem.get_visible())
        self.assertEqual(model.problem.status.get_title(), "No Server Yet")
        self.assertIn("model", model.problem.status.get_description())
        self.assertTrue(model.problem._button.get_visible())
        model.problem._button.emit("clicked")
        self.assertTrue(run_until(
            lambda: win.nav.get_visible_page().get_tag() == "add-server", 3))
        self.assertIn("Add a server", win.pages["privacy"].server.get_description())
        self.assertEqual(self.errors, [])

    def test_with_an_offline_server(self):
        profile = self.app.add_server("http://127.0.0.1:9", "Down")
        mon = self.app.monitors[profile.id]
        self.assertTrue(run_until(lambda: mon.status == "offline", 10))
        win = self.visit_every_page()
        self.assertEqual(win.pages["dots"].problem.status.get_title(), "Down Is Offline")
        self.assertIn("model", win.pages["model"].problem.status.get_description())
        self.assertFalse(win.pages["privacy"].server.get_sensitive())
        self.assertEqual(self.errors, [])


@unittest.skipUnless(Gtk.init_check(), "no display available")
class ModernModelTests(unittest.TestCase):
    """A server for Dotdeck 0.3.3: providers, a key per provider, prices and call models."""

    model_config = MODEL_CONFIG
    open = SettingsWindowTests.open
    wait_model = SettingsWindowTests.wait_model

    def setUp(self):
        import locale
        numeric = locale.setlocale(locale.LC_NUMERIC)
        locale.setlocale(locale.LC_NUMERIC, "C")     # "0.10" whatever the machine speaks
        self.addCleanup(locale.setlocale, locale.LC_NUMERIC, numeric)
        SettingsWindowTests.setUp(self)

    def tearDown(self):
        SettingsWindowTests.tearDown(self)

    def page(self):
        win = self.open("model")
        self.wait_model(win)
        return win, win.pages["model"]

    def test_the_model_in_use_with_its_price_key_and_a_test(self):
        _win, page = self.page()
        self.assertEqual(page.model_row.get_title(), "Claude Haiku 5.5")
        self.assertEqual(page.model_row.get_subtitle(), "anthropic/claude-haiku-5.5 · OpenRouter")
        self.assertTrue(page.price_row.get_visible())
        self.assertEqual(page.price_row.get_title(), "Approximate Cost")
        self.assertEqual(page.price_row.get_subtitle(),
                         "US$ 0.10 input · US$ 0.50 output per million tokens")
        self.assertEqual((page.key_status.get_title(), page.key_status.get_subtitle()),
                         ("OpenRouter Key", "Configured"))
        self.assertTrue(page.test_row.get_visible())
        page.test_button.emit("clicked")
        self.assertTrue(run_until(lambda: page.test_row.get_subtitle() == "Answered in 1.2 s",
                                  5), page.test_row.get_subtitle())
        self.assertEqual(self.fake.model.tests, [{}])
        self.fake.model.test_result = {"ok": False, "ms": 900, "error": "401 Invalid key"}
        page.test_button.emit("clicked")
        self.assertTrue(run_until(
            lambda: page.test_row.get_subtitle() == "Did not answer: 401 Invalid key", 5))
        self.assertEqual(self.fake.model.posts, [])        # testing changes nothing

    def test_models_of_the_provider_with_price_and_speed(self):
        _win, page = self.page()
        self.assertTrue(page.provider_combo.get_visible())
        self.assertEqual(page._provider, "openrouter")
        self.assertEqual(page._choices, ["deepseek/deepseek-v4-flash-0731",
                                         "anthropic/claude-haiku-5.5",
                                         "anthropic/claude-sonnet-5.5", ""])
        self.assertEqual(page._selected_model(), "anthropic/claude-haiku-5.5")
        item = page.combo.get_selected_item()
        self.assertEqual(item.props.detail,
                         "In use · US$ 0.10 / 0.50 per million tokens · fast")
        self.assertFalse(page.apply.get_visible())          # nothing to change yet
        page.combo.set_selected(0)
        self.assertEqual(page.combo.get_subtitle(),
                         "US$ 0.0046 / 1.28 per million tokens · fast · lowest cost")
        self.assertTrue(page.apply.get_sensitive())
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {"model": "deepseek/deepseek-v4-flash-0731"})
        self.assertTrue(run_until(lambda: page.model_row.get_title() == "DeepSeek V4 Flash", 5))

    def test_switching_to_anthropic_takes_its_key(self):
        _win, page = self.page()
        page.select_provider("anthropic")
        self.assertEqual(page._choices, ["claude-haiku-5-5", "claude-sonnet-5-5",
                                         "claude-opus-5-5", ""])
        self.assertTrue(page.new_key.get_visible())
        self.assertEqual(page.new_key.get_title(), "Anthropic Key")
        self.assertTrue(page.apply.get_visible())
        self.assertFalse(page.apply.get_sensitive())        # no Anthropic key yet
        page.new_key.set_text("sk-ant-test-key")
        self.assertTrue(page.apply.get_sensitive())
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {"model": "claude-haiku-5-5",
                                                     "provider": "anthropic",
                                                     "apiKey": "sk-ant-test-key"})
        self.assertTrue(run_until(
            lambda: page.model_row.get_subtitle() == "claude-haiku-5-5 · Anthropic", 5),
            page.model_row.get_subtitle())
        self.assertEqual(page.model_row.get_title(), "Claude Haiku 5.5")
        self.assertEqual((page.key_status.get_title(), page.key_status.get_subtitle()),
                         ("Anthropic Key", "Configured"))
        self.assertEqual(page.new_key.get_text(), "")
        self.assertFalse(page.new_key.get_visible())
        self.assertEqual(page._provider, "anthropic")
        # Back to OpenRouter: its key is there, so none is asked.
        page.select_provider("openrouter")
        self.assertEqual(page.new_key.get_title(), "New OpenRouter Key (optional)")
        self.assertTrue(page.apply.get_sensitive())

    def test_custom_ids_are_checked_against_the_provider(self):
        _win, page = self.page()
        page.combo.set_selected(page._choices.index(""))
        self.assertTrue(page.custom.get_visible())
        page.custom.set_text("haiku")
        self.assertFalse(page.apply.get_sensitive())
        self.assertTrue(page.custom.has_css_class("error"))
        page.custom.set_text("vendor/model-x")
        self.assertTrue(page.apply.get_sensitive())
        self.assertFalse(page.custom.has_css_class("error"))
        page.select_provider("anthropic")
        page.new_key.set_text("sk-ant-test-key")
        page.combo.set_selected(page._choices.index(""))
        self.assertEqual(page.custom.get_title(), "Model ID, for example claude-haiku-5-5")
        page.custom.set_text("anthropic/claude-haiku-5.5")
        self.assertFalse(page.apply.get_sensitive())
        page.custom.set_text("claude-haiku-5-5-20261007")
        self.assertTrue(page.apply.get_sensitive())

    def test_an_openai_compatible_server_needs_its_address(self):
        _win, page = self.page()
        self.assertFalse(page.advanced.get_visible())
        page.select_provider("openai-compatible")
        self.assertTrue(page.advanced.get_visible())
        self.assertEqual(page._choices, [""])                # no presets: type the id
        self.assertTrue(page.custom.get_visible())
        page.custom.set_text("llama-local")
        page.new_key.set_text("local-test-key")
        self.assertFalse(page.apply.get_sensitive())        # no address yet
        page.base_url.set_text("not an address")
        self.assertFalse(page.apply.get_sensitive())
        page.base_url.set_text("http://192.168.0.10:8080/v1")
        self.assertTrue(page.apply.get_sensitive())
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {
            "model": "llama-local", "provider": "openai-compatible",
            "baseUrl": "http://192.168.0.10:8080/v1", "apiKey": "local-test-key"})
        self.assertTrue(run_until(
            lambda: page.model_row.get_subtitle() == "llama-local · OpenAI-compatible", 5))
        self.assertEqual(page.key_status.get_title(), "OpenAI-compatible Server Key")

    def test_switching_to_a_compatible_server_always_sends_its_address(self):
        """Even the address in use now: the server refuses the switch without one."""
        self.fake.model_config.update(provider="anthropic", baseUrl=ANTHROPIC_URL,
                                      model="claude-haiku-5-5")
        self.fake.model_config["keys"]["anthropic"] = True
        _win, page = self.page()
        page.select_provider("openai-compatible")
        page.custom.set_text("claude-haiku-5-5")
        page.new_key.set_text("local-test-key")
        page.base_url.set_text(ANTHROPIC_URL)               # Anthropic's compatible API
        self.assertTrue(page.apply.get_sensitive())
        self.assertEqual(page._pending()[0], {
            "model": "claude-haiku-5-5", "provider": "openai-compatible",
            "baseUrl": ANTHROPIC_URL, "apiKey": "local-test-key"})

    def test_a_key_typed_for_one_provider_never_goes_to_another(self):
        _win, page = self.page()
        page.select_provider("anthropic")
        page.new_key.set_text("sk-ant-test-key")
        self.assertEqual(page._pending()[0].get("apiKey"), "sk-ant-test-key")
        # Changed their mind: the Anthropic key must not go to a compatible server.
        page.select_provider("openai-compatible")
        self.assertEqual(page.new_key.get_text(), "")
        self.assertEqual(page.new_key.get_title(), "OpenAI-compatible Server Key")
        page.custom.set_text("llama-local")
        page.base_url.set_text("http://192.168.0.10:8080/v1")
        changes, problem = page._pending()
        self.assertNotIn("apiKey", changes)
        self.assertIsNotNone(problem)                       # its own key is still missing
        self.assertFalse(page.apply.get_sensitive())
        # Nor back to Anthropic after a refresh of the page.
        page.select_provider("anthropic")
        page.new_key.set_text("sk-ant-test-key")
        page._signature = None
        page.model_changed()                                # the server answered again
        self.assertEqual(page._provider, "openrouter")
        self.assertEqual(page.new_key.get_text(), "")
        self.assertEqual(self.fake.model.posts, [])

    def test_one_key_row_per_provider(self):
        _win, page = self.page()
        self.assertEqual([r.get_title() for r in page._key_rows],
                         ["OpenRouter", "Anthropic", "OpenAI-compatible"])
        self.assertEqual([r.get_subtitle() for r in page._key_rows],
                         ["Configured", "No key yet", "No key yet"])
        # No warning for providers the owner does not use: they are just not set up.
        icons = [next(w.get_icon_name() for w in descendants(r) if isinstance(w, Gtk.Image)
                      and w.get_visible() and w.get_icon_name()) for r in page._key_rows]
        self.assertEqual(icons, ["object-select-symbolic", "dialog-password-symbolic",
                                 "dialog-password-symbolic"])
        self.assertEqual(page.key_row.get_title(), "Set a New OpenRouter Key")
        page.key_row.set_text("sk-or-test-key")
        page.key_row.emit("apply")
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {"apiKey": "sk-or-test-key"})
        self.assertTrue(run_until(lambda: page.key_row.get_text() == "", 5))
        # Another provider's key goes in with one of its models, under Change.
        button = next(w for w in descendants(page._key_rows[1]) if isinstance(w, Gtk.Button)
                      and w.get_label() == "Set New Key…")
        button.emit("clicked")
        self.assertEqual(page._provider, "anthropic")
        self.assertTrue(page.new_key.get_visible())
        self.assertEqual(len(self.fake.model.posts), 1)

    def test_the_three_models_of_a_call(self):
        win, page = self.page()
        self.assertEqual(page.calls.get_title(), "Voice Call Model")
        self.assertEqual(page.speech_row.get_subtitle(),
                         "openai/gpt-audio-mini · Dotdeck voice gateway")
        self.assertEqual(page.transcribe_row.get_subtitle(), "google/gemini-2.5-flash-lite")
        self.assertEqual(page.thinking_row.get_subtitle(), "Claude Haiku 5.5 · the chat model")
        page.calls_button.emit("activated")
        self.assertTrue(run_until(lambda: win.visible_page == "voice", 2))
        voice = win.pages["voice"]
        groups = [w for w in descendants(voice.widget) if isinstance(w, Adw.PreferencesGroup)]
        self.assertEqual(groups.index(voice.voice_group), groups.index(voice.status) + 1)
        self.assertEqual([r.get_title() for r in voice._voice_rows][:4],
                         ["Speech", "Transcription", "Research During Calls", "Voice"])
        speech = voice._voice_rows[0]
        self.assertIsInstance(speech, Adw.ComboRow)
        self.assertEqual(voice._voice_rows[1].get_subtitle(), "google/gemini-2.5-flash-lite")
        speech.set_selected(1)          # openai/gpt-audio
        self.assertTrue(run_until(lambda: self.fake.model.posts, 5))
        self.assertEqual(self.fake.model.posts[-1], {"voiceModel": "openai/gpt-audio"})
        self.assertTrue(run_until(lambda: "openai/gpt-audio ·" in page.speech_row.get_subtitle(),
                                  5), page.speech_row.get_subtitle())

    def test_a_change_the_server_could_not_save_is_reported(self):
        win, page = self.page()
        toasts: list[str] = []
        win.toast = toasts.append
        self.fake.model.persisted = False
        page.combo.set_selected(0)
        page._apply(page._selected_model())
        self.assertTrue(run_until(lambda: len(toasts) == 2, 5), toasts)
        self.assertIn("until OpenDots restarts", toasts[0])
        self.assertEqual(toasts[1], "The Dots now use DeepSeek V4 Flash")
        # The name of the old model is not lent to the new one meanwhile.
        self.assertTrue(run_until(lambda: page.model_row.get_title() == "DeepSeek V4 Flash", 5))

    def test_no_model_yet(self):
        self.fake.model_config.update(model=None, modelName=None, price=None, configured=False,
                                      hasApiKey=False)
        self.fake.model_config["keys"]["openrouter"] = False
        _win, page = self.page()
        self.assertEqual(page.model_row.get_title(), "No Model Yet")
        self.assertFalse(page.price_row.get_visible())
        self.assertEqual(page.key_status.get_subtitle(), "No key yet")
        self.assertFalse(page.test_button.get_sensitive())
        self.assertEqual(page._choices[0], "deepseek/deepseek-v4-flash-0731")

    def test_the_sidebar_names_the_model_from_the_monitor(self):
        self.assertTrue(run_until(lambda: self.mon.model_config is not None, 10))
        self.assertEqual(models.at_a_glance(self.mon.workspace.active_model,
                                            self.mon.workspace.active_provider,
                                            self.mon.model_config),
                         "Claude Haiku 5.5 · OpenRouter")


@unittest.skipUnless(Gtk.init_check(), "no display available")
class TwoServersTests(unittest.TestCase):
    """With two servers the server pages get a selector and follow the open server."""

    def setUp(self):
        Adw.init()
        self.home = RecordingOpenDots(model_config=GATEWAY_CONFIG)
        self.office = RecordingOpenDots(model_config=None)
        self.office.dots = self.office.dots[:2]
        for fake in (self.home, self.office):
            self.addCleanup(fake.stop)
        self.app = FakeApp(self.id())
        self.addCleanup(self.app.stop)
        self.first = self.app.add_server(self.home.url, "HomeLab")
        self.second = self.app.add_server(self.office.url, "Office")
        self.app.profiles.set_active(self.first.id)
        for profile in (self.first, self.second):
            mon = self.app.monitors[profile.id]
            self.assertTrue(run_until(lambda: mon.status == "online"
                                      and mon.workspace.dots, 10))

    def tearDown(self):
        SettingsWindowTests.tearDown(self)

    def test_selector_switches_the_server_pages(self):
        from src.ui.settings import get_window, open_settings
        open_settings(self.app, "dots")
        win = get_window()
        page = win.pages["dots"]
        self.assertTrue(page.selector.get_visible())
        self.assertTrue(run_until(lambda: len(page._rows) == 8, 5))
        page.selector.set_selected(1)
        self.assertTrue(run_until(lambda: win.profile.id == self.second.id, 3))
        self.assertTrue(run_until(lambda: len(page._rows) == 2, 5))
        self.assertTrue(run_until(lambda: win.model_config()["state"] == "unsupported", 5))
        self.assertEqual(win.pages["voice"].selector.get_selected(), 1)
        # Picked here: switching the open server elsewhere no longer moves it.
        self.app.profiles.set_active(self.second.id)
        self.app.profiles.set_active(self.first.id)
        self.assertEqual(win.profile.id, self.second.id)

    def test_follows_the_open_server_until_one_is_picked(self):
        from src.ui.settings import get_window, open_settings
        open_settings(self.app, "privacy")
        win = get_window()
        self.assertEqual(win.profile.id, self.first.id)
        self.app.profiles.set_active(self.second.id)
        self.assertEqual(win.profile.id, self.second.id)
        self.assertIn("Office", win.pages["privacy"].server.get_title())


@unittest.skipUnless(Gtk.init_check(), "no display available")
class UpstreamServerTests(unittest.TestCase):
    """A plain upstream OpenDots: no /api/model-config."""

    def setUp(self):
        Adw.init()
        self.fake = RecordingOpenDots(model_config=None)
        self.addCleanup(self.fake.stop)
        self.app = FakeApp(self.id())
        self.addCleanup(self.app.stop)
        self.profile = self.app.add_server(self.fake.url)
        self.mon = self.app.monitors[self.profile.id]
        self.assertTrue(run_until(lambda: self.mon.status == "online", 10))

    def tearDown(self):
        SettingsWindowTests.tearDown(self)

    def test_model_page_hides_its_controls_on_404(self):
        from src.ui.settings import get_window, open_settings
        open_settings(self.app, "model")
        win = get_window()
        self.assertTrue(run_until(lambda: win.model_config()["state"] == "unsupported", 10))
        page = win.pages["model"]
        for group in (page.current, page.change, page.key):
            self.assertFalse(group.get_visible())
        self.assertTrue(page.problem.get_visible())
        self.assertEqual(page.problem.status.get_title(),
                         "This server does not let Dotdeck change its model")
        win.search_entry.set_text("api key")
        run_until(lambda: False, 0.4)
        self.assertFalse(any(e.page == "model" for e in win.search_results()))
        voice = win.pages["voice"]
        self.assertFalse(any(isinstance(w, (Adw.PasswordEntryRow, Adw.ComboRow))
                             and w in voice._voice_rows for w in descendants(voice.widget)))
        self.assertIn("Set on the server", voice._voice_rows[0].get_subtitle())


if __name__ == "__main__":
    unittest.main()
