"""Focus mode in a real WebKit, against an imitation of the OpenDots web client.

Covers what the native window relies on: the page's navigation hidden only on a
recognized page, page commands (open a Dot, a conversation, a Space, start and end a
call...), selection sync, pets on the page, colour scheme sync and native dialogs for
window.alert/confirm/prompt.
"""

from __future__ import annotations

import base64
import itertools
import json
import unittest
from unittest import mock

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from src.core.store import Profile  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402
from tests.test_webview import FakeHost  # noqa: E402

TOKEN = "owner-token-for-focus-tests"
DOTS = ["Assistente", "Manutenção", "Dot", "Estratégia", "Finanças", "Laboratório", "Estudos",
        "Pesquisa"]
PETS = {"Assistente": "rosy", "Manutenção": "bolt", "Dot": "sprout", "Estratégia": "ember",
        "Finanças": "sunny", "Laboratório": "plum", "Estudos": "sage", "Pesquisa": "graphite"}
LONG = "# Assistente — organiza o dia e os outros Dots\n\n" + \
    "Organiza a agenda e cobra prazos. " * 60
NEW = {"id": "c9", "dotId": "d5", "title": "Voice call", "createdAt": 9}
# Newest first, as GET /api/workspace lists them.
CONVERSATIONS = [
    {"id": "c5", "dotId": "d1", "title": "Backups da semana", "createdAt": 5},
    {"id": "c4", "dotId": "d0", "title": "Agenda da semana", "createdAt": 4},
    {"id": "c3", "dotId": "d2", "title": "A new thought", "createdAt": 3},
    {"id": "c2", "dotId": "d0", "title": "Conversa antiga", "createdAt": 2},
    {"id": "c1", "dotId": "d4", "title": "Orçamento", "createdAt": 1},
]
_ids = itertools.count(1)


def _descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


@unittest.skipUnless(Gtk.init_check() if hasattr(Gtk, "init_check") else True,
                     "no display available")
class FocusModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.web.webview import ServerView
        cls.ServerView = ServerView
        cls.fake = FakeOpenDots(token=TOKEN, opendots=True, dots=DOTS,
                                instructions={"Assistente": LONG},
                                conversations=CONVERSATIONS,
                                spaces=[{"id": "s0", "name": "Casa", "description": "",
                                         "createdAt": 1}]).start()
        cls.plain = FakeOpenDots(token=TOKEN).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        cls.plain.stop()

    def setUp(self):
        self.views = []
        self.focus_events: list[bool] = []
        self.selections: list[tuple[str, str]] = []
        self.states: list[tuple[str, str]] = []

    def tearDown(self):
        for view, window in self.views:
            view.destroy()
            window.destroy()

    # -- helpers ---------------------------------------------------------------------------

    def open_view(self, url: str | None = None, token: str | None = TOKEN, focus: bool = True,
                  path: str = "", probe: int | None = None, pets: dict | None = None,
                  width: int = 1200, height: int = 800):
        profile = Profile(f"focus{next(_ids)}", "Casa", url or self.fake.url)
        with mock.patch.object(self.ServerView, "PROBE_TIMEOUT_MS",
                               probe or self.ServerView.PROBE_TIMEOUT_MS):
            view = self.ServerView(profile, token, FakeHost())
            view.connect("focus-changed", lambda _v, active: self.focus_events.append(active))
            view.connect("page-selection", lambda _v, d, p: self.selections.append((d, p)))
            view.connect("dot-state", lambda _v, d, s: self.states.append((d, s)))
            view.set_focus_mode(focus)
            if pets is not None:
                view.set_pets(pets, True)
        if path:
            view.webview.load_uri(profile.url + path)
        window = Gtk.Window(child=view, default_width=width, default_height=height)
        window.present()
        self.views.append((view, window))
        self.view = view
        self.assertTrue(run_until(lambda: view.loaded_once, 20), "page did not load")
        return view

    def open_app(self, **kwargs):
        view = self.open_view(**kwargs)
        self.assertTrue(run_until(lambda: self.js("return !!window.__fake && "
                                                  "!!document.querySelector('.template-app');"),
                                  10), "the app did not render")
        return view

    def js(self, body: str, **args):
        box = {}
        variant = GLib.Variant("a{sv}", {k: GLib.Variant("s", v) for k, v in args.items()})

        def done(view, result):
            try:
                value = view.call_async_javascript_function_finish(result)
                box["value"] = json.loads(value.to_json(0)) if value and not \
                    value.is_undefined() else None
            except GLib.Error as error:
                box["error"] = error.message

        self.view.webview.call_async_javascript_function(body, -1, variant, None, None, None, done)
        self.assertTrue(run_until(lambda: box, 20), f"no result for {body[:40]}")
        if "error" in box:
            raise AssertionError(box["error"])
        return box["value"]

    def command(self, method, *args) -> bool:
        box = []
        method(*args, done=box.append)
        self.assertTrue(run_until(lambda: box, 25), f"{method.__name__} never answered")
        self.assertEqual(len(box), 1)
        return box[0]

    def selection_is(self, dot: str, view: str) -> bool:
        return run_until(lambda: self.view.page_selection == (dot, view), 5)

    def ask(self, script: str) -> None:
        """Run a page question a moment later, so that this script's answer goes out first."""
        self.js(f"setTimeout(() => {{ {script} }}, 200); return true;")

    def dialog_shown(self):
        self.assertTrue(run_until(lambda: self.view._script_dialog is not None, 10),
                        "no dialog was shown")
        return self.view._script_dialog

    def answer_dialog(self, response: str, text: str | None = None) -> None:
        self.assertTrue(run_until(lambda: self.view._script_dialog is not None, 10),
                        "no dialog was shown")
        dialog = self.view._script_dialog
        if text is not None:
            self.view._script_entry.set_text(text)
        label = dialog.get_response_label(response)
        button = next((w for w in _descendants(dialog) if isinstance(w, Gtk.Button)
                       and w.get_label() == label), None)
        if button is not None:
            button.emit("clicked")
        else:
            dialog.emit("response", response)
        run_until(lambda: self.view._script_dialog is None, 5)
        dialog.force_close()

    # -- focus mode ------------------------------------------------------------------------

    def test_focus_mode_hides_the_page_navigation_and_brings_it_back(self):
        view = self.open_app()
        self.assertTrue(run_until(lambda: self.focus_events, 15))
        self.assertEqual(self.focus_events, [True])
        self.assertTrue(view.focus_active)
        self.assertTrue(view.page_recognized)
        layout = """
            const display = (s) => getComputedStyle(document.querySelector(s)).display;
            return {
              attr: document.documentElement.hasAttribute('data-dotdeck-focus'),
              rail: display('nav.icon-rail'), sidebar: display('#workspace-sidebar'),
              topbar: display('header.topbar'), mobile: display('.mobile-menu'),
              margin: getComputedStyle(document.querySelector('.workspace')).marginLeft,
              height: Math.round(document.querySelector('.chat-workspace')
                                 .getBoundingClientRect().height),
              inner: innerHeight,
              clamp: getComputedStyle(document.querySelector('.empty-chat-persona p'))
                     .webkitLineClamp,
              collapsed: localStorage.getItem('opendots_dots_collapsed'),
            };"""
        hidden = self.js(layout)
        self.assertEqual(hidden, {"attr": True, "rail": "none", "sidebar": "none",
                                  "topbar": "none", "mobile": "none", "margin": "0px",
                                  "height": hidden["inner"], "inner": hidden["inner"],
                                  "clamp": "3", "collapsed": "false"})

        # The home card shows the job from "# Assistente — job" instead of the instructions.
        self.assertEqual(self.js("""return document.querySelector('.empty-chat-persona p')
            .getAttribute('data-dotdeck-summary');"""), "organiza o dia e os outros Dots")
        self.assertTrue(self.command(view.open_dot, 0, "Assistente"))
        self.assertEqual(self.js("""const p = document.querySelector('.chat-welcome > p');
            return [getComputedStyle(p).display, getComputedStyle(
            document.querySelector('.chat-welcome > p.muted')).display];"""),
            ["none", "-webkit-box"])
        self.assertTrue(self.command(view.new_chat, 0, "Assistente"))

        view.set_focus_mode(False)
        self.assertTrue(run_until(lambda: len(self.focus_events) == 2, 5))
        self.assertEqual(self.focus_events, [True, False])
        self.assertFalse(view.focus_active)
        shown = self.js(layout)
        self.assertFalse(shown["attr"])
        self.assertEqual((shown["rail"], shown["sidebar"], shown["topbar"]),
                         ("flex", "flex", "flex"))
        self.assertEqual(shown["margin"], "268px")

        view.set_focus_mode(True)
        self.assertTrue(run_until(lambda: len(self.focus_events) == 3, 5))
        self.assertEqual(self.focus_events[-1], True)

    def test_a_page_that_is_not_opendots_is_left_alone(self):
        view = self.open_view(url=self.plain.url, probe=1200)
        self.assertTrue(run_until(lambda: self.focus_events, 6))
        self.assertEqual(self.focus_events, [False])
        self.assertFalse(view.focus_active)
        self.assertIs(view.page_recognized, False)
        self.assertEqual(self.js("""return [document.documentElement.hasAttribute(
            'data-dotdeck-focus'), document.querySelector('h1').textContent,
            getComputedStyle(document.querySelector('h1')).display];"""),
            [False, "OpenDots", "block"])
        self.assertFalse(self.command(view.open_dot, 0, "Assistente"))

    def test_the_unlock_screen_is_opendots_and_focus_follows_the_page(self):
        # Without a token the page shows its unlock form: OpenDots, with nothing to hide,
        # so Dotdeck keeps its own sidebar instead of falling back to the web layout.
        view = self.open_view(token=None, probe=1200, pets=PETS)
        self.assertTrue(run_until(lambda: self.focus_events, 6))
        self.assertEqual(self.focus_events, [True])
        self.assertTrue(view.page_recognized)
        # The page renders asynchronously: wait for the element before reading its style.
        self.assertTrue(run_until(lambda: self.js("""const form = document.querySelector(
            'main.unlock form'); return form ? getComputedStyle(form).display : null;""")
            == "block", 5))
        # Mascots of no Dot get the brand pet, even with a Dot named "Dot".
        self.assertTrue(run_until(lambda: self.js(
            "return document.querySelector('main.unlock span.mascot')"
            ".getAttribute('data-dotdeck-pet');") == "sky", 5))
        self.js("sessionStorage.setItem('opendots-token', token); return true;", token=TOKEN)
        self.assertTrue(run_until(lambda: self.js(
            "return !!document.querySelector('.template-app');"), 8))
        self.assertTrue(run_until(lambda: self.js(
            "const rail = document.querySelector('nav.icon-rail');"
            " return rail ? getComputedStyle(rail).display : null;") == "none", 5))
        self.assertEqual(self.focus_events, [True])
        # Another layout takes over the page: its navigation comes back, then focus does.
        self.js("""const other = document.createElement('h1');
            other.textContent = 'Somewhere else';
            document.getElementById('root').replaceChildren(other); return true;""")
        self.assertTrue(run_until(lambda: len(self.focus_events) == 2, 5))
        self.assertEqual(self.focus_events, [True, False])
        self.assertFalse(view.focus_active)
        self.assertFalse(self.js("return document.documentElement.hasAttribute("
                                 "'data-dotdeck-focus');"))
        self.js("window.__fakeRender(); return true;")
        self.assertTrue(run_until(lambda: len(self.focus_events) == 3, 5))
        self.assertEqual(self.focus_events, [True, False, True])
        self.assertTrue(view.focus_active)

    def test_turning_focus_on_during_the_probe_waits_for_its_answer(self):
        # A page that is not OpenDots, before its probe settles: no early "no".
        view = self.open_view(url=self.plain.url, focus=False, probe=3000)
        view.set_focus_mode(True)
        run_until(lambda: False, 1)
        self.assertEqual(self.focus_events, [])
        self.assertTrue(run_until(lambda: self.focus_events, 6))
        run_until(lambda: False, 0.5)
        self.assertEqual(self.focus_events, [False])
        self.assertIs(view.page_recognized, False)
        # Turning it off on a page that never had it says nothing; on again answers.
        view.set_focus_mode(False)
        run_until(lambda: False, 1)
        self.assertEqual(self.focus_events, [False])
        view.set_focus_mode(True)
        self.assertTrue(run_until(lambda: len(self.focus_events) == 2, 5))
        self.assertEqual(self.focus_events, [False, False])

    # -- page commands ---------------------------------------------------------------------

    def test_open_dot_by_index_checks_the_name(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_dot, 1, "Manutenção"))
        self.assertTrue(self.selection_is("Manutenção", "chat"))
        self.assertEqual(self.js("""return [window.__fake.selectedThread,
            document.querySelector('.chat-persona strong').textContent,
            getComputedStyle(document.querySelector('.chat-persona')).display];"""),
            ["c5", "Manutenção", "none"])
        # The page's list changed under the native one: the name wins over the index.
        self.assertTrue(self.command(view.open_dot, 3, "Laboratório"))
        self.assertTrue(self.selection_is("Laboratório", "home"))
        self.assertFalse(self.command(view.open_dot, 0, "Nobody"))
        self.assertEqual(view.page_selection, ("Laboratório", "home"))

    def test_open_dot_expands_a_collapsed_dots_list(self):
        view = self.open_app()
        self.js("document.querySelector('button[aria-label=\"Collapse dots list\"]').click();"
                "return true;")
        self.assertTrue(run_until(lambda: self.js(
            "return !document.querySelector('nav[aria-label=\"Dots\"]');"), 5))
        self.assertTrue(self.command(view.open_dot, 4, "Finanças"))
        self.assertTrue(self.selection_is("Finanças", "chat"))

    def test_new_chat_shows_an_empty_composer(self):
        view = self.open_app()
        self.assertTrue(self.command(view.new_chat, 0, "Assistente"))
        self.assertTrue(self.selection_is("Assistente", "home"))
        self.assertEqual(self.js("""return [window.__fake.selectedThread === undefined,
            document.activeElement.getAttribute('aria-label')];"""),
            [True, "Start a conversation"])
        # From a conversation of the open Dot: straight home, without reopening the Dot.
        self.assertTrue(self.command(view.open_conversation, "c2", 0, "Assistente"))
        chosen = self.js("return window.__fake.chosen;")
        self.assertTrue(self.command(view.new_chat, 0, "Assistente"))
        self.assertTrue(self.selection_is("Assistente", "home"))
        self.assertEqual(self.js("return window.__fake.chosen;"), chosen)
        # Another Dot is opened first.
        self.assertTrue(self.command(view.new_chat, 1, "Manutenção"))
        self.assertTrue(self.selection_is("Manutenção", "home"))

    def test_open_conversation_through_react(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_conversation, "c2", 0, "Assistente"))
        self.assertTrue(self.selection_is("Assistente", "chat"))
        self.assertEqual(self.js("return window.__fake.selectedThread;"), "c2")
        # Another Dot's conversation opens directly (not its Dot's latest one first).
        self.assertTrue(self.command(view.open_conversation, "c1", 4, "Finanças"))
        self.assertEqual(self.js("return [window.__fake.selectedThread, window.__fake.chosen];"),
                         ["c1", 0])
        # One the server does not have fails at once; the page shows the Dot instead.
        started = GLib.get_monotonic_time()
        self.assertFalse(self.command(view.open_conversation, "missing", 1, "Manutenção"))
        self.assertLess(GLib.get_monotonic_time() - started, 4_000_000)
        self.assertTrue(self.selection_is("Manutenção", "chat"))

    def test_open_conversation_waits_for_a_new_one(self):
        # Dotdeck creates a conversation through the API, then opens it at once: the
        # page learns about it only at its next refresh.
        view = self.open_app()
        self.assertTrue(run_until(lambda: self.js(
            "return window.__fake.workspace.conversations.length;") == len(CONVERSATIONS), 5))
        self.fake.conversations.insert(0, NEW)
        self.addCleanup(self.fake.conversations.remove, NEW)
        self.assertTrue(self.command(view.open_conversation, "c9", 5, "Laboratório"))
        self.assertTrue(self.selection_is("Laboratório", "chat"))
        self.assertEqual(self.js("return window.__fake.selectedThread;"), "c9")

    def test_open_conversation_without_react_internals(self):
        view = self.open_app(path="/?nofiber=1")
        self.assertTrue(self.command(view.open_conversation, "c2", 0, "Assistente"))
        self.assertTrue(self.selection_is("Assistente", "chat"))
        self.assertEqual(self.js("return window.__fake.selectedThread;"), "c2")
        self.assertTrue(self.command(view.open_conversation, "c3", 2, "Dot"))
        self.assertEqual(self.js("return window.__fake.selectedThread;"), "c3")
        self.assertFalse(self.command(view.open_conversation, "missing", 2, "Dot"))

    def test_spaces_activity_memories_settings_and_computer(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_space, "s0"))
        self.assertTrue(self.selection_is("", "space"))
        self.assertTrue(self.command(view.open_space, "s0", "p1"))
        self.assertEqual(self.js("return location.hash;"), "#/spaces/s0/pages/p1")
        self.assertFalse(self.command(view.open_space, "../evil"))
        self.assertTrue(self.command(view.open_activity))
        self.assertTrue(self.selection_is("", "tasks"))
        self.assertTrue(self.command(view.open_memories))
        self.assertTrue(self.selection_is("", "memories"))
        self.assertTrue(self.command(view.open_web_settings))
        self.js("document.querySelector('.modal-close').click(); return true;")
        self.assertTrue(self.command(view.edit_dot_in_page, "Manutenção"))
        self.assertEqual(self.js("return document.querySelector('#dialog-title').textContent;"),
                         "Edit Manutenção")
        self.js("document.querySelector('.modal-close').click(); return true;")
        self.assertFalse(self.command(view.edit_dot_in_page, "Nobody"))
        self.assertTrue(self.command(view.open_dot, 0, "Assistente"))
        self.assertTrue(self.command(view.toggle_computer))
        self.assertEqual(self.js("""return document.querySelector(
            'button[aria-label="Hide computer"]').getAttribute('aria-expanded');"""), "true")

    def test_calls_start_once_the_button_is_ready_and_end(self):
        view = self.open_app()
        self.assertFalse(self.command(view.end_call))
        self.assertTrue(self.command(view.start_call, None, 1, "Manutenção"))
        self.assertEqual(self.js("return [!!document.querySelector('section.call-view'), "
                                 "window.__fake.voice !== 'idle'];"), [True, True])
        self.assertTrue(self.command(view.end_call))
        self.assertTrue(run_until(lambda: self.js(
            "return !document.querySelector('section.call-view');"), 5))
        # A conversation given by id; a Dot without any conversation cannot be called.
        self.assertTrue(self.command(view.start_call, "c2", 0, "Assistente"))
        self.assertEqual(self.js("return window.__fake.selectedThread;"), "c2")
        # Opening the Dot of the call keeps the call (pressing its row again would switch
        # to its latest conversation and end the call); calling it again is a no-op.
        self.assertTrue(self.command(view.open_dot, 0, "Assistente"))
        self.assertTrue(self.command(view.start_call, None, 0, "Assistente"))
        self.assertEqual(self.js("return [window.__fake.selectedThread, "
                                 "window.__fake.voice !== 'idle'];"), ["c2", True])
        self.assertTrue(self.command(view.end_call))
        self.assertFalse(self.command(view.start_call, None, 5, "Laboratório"))

    def test_commands_wait_for_the_page_to_load(self):
        profile = Profile(f"focus{next(_ids)}", "Casa", self.fake.url)
        view = self.ServerView(profile, TOKEN, FakeHost())
        window = Gtk.Window(child=view, default_width=1000, default_height=700)
        window.present()
        self.views.append((view, window))
        self.view = view
        box = []
        view.open_dot(2, "Dot", box.append)   # Before the page has even loaded.
        self.assertTrue(run_until(lambda: box, 20))
        self.assertEqual(box, [True])

    def test_banners_keep_the_composer_in_the_window(self):
        # "All Dots are paused" sits above the chat: the chat gives it room.
        self.fake.paused = True
        self.addCleanup(setattr, self.fake, "paused", False)
        bottoms = """const notice = document.querySelector('.notice');
            const bottom = (s) => Math.round(
                document.querySelector(s).getBoundingClientRect().bottom);
            return notice ? [bottom('.notice') > 0, bottom('.chat-workspace') <= innerHeight,
                             bottom('.live-chat') <= innerHeight,
                             document.scrollingElement.scrollHeight <= innerHeight] : null;"""
        for width in (1200, 900):
            with self.subTest(width=width):
                view = self.open_app(width=width)
                self.assertTrue(self.command(view.open_dot, 0, "Assistente"))
                self.assertTrue(run_until(lambda: self.js(bottoms) is not None, 5))
                self.assertEqual(self.js(bottoms), [True, True, True, True])
                self.assertTrue(self.command(view.toggle_computer))   # The split view.
                self.assertEqual(self.js(bottoms), [True, True, True, True])

    # -- selection sync --------------------------------------------------------------------

    def test_the_page_reports_what_it_shows(self):
        view = self.open_app()
        self.assertTrue(self.selection_is("Assistente", "home"))
        # The owner clicks in the page itself (with focus mode off, its sidebar shows).
        self.js("""document.querySelectorAll('nav[aria-label="Dots"] button.dot-nav')[2].click();
                   return true;""")
        self.assertTrue(self.selection_is("Dot", "chat"))
        self.assertTrue(run_until(lambda: self.states and self.states[-1] == ("Dot", "idle"), 5))
        self.js("window.__fake.running = true; window.__fakeRender(); return true;")
        self.assertTrue(run_until(lambda: self.states[-1] == ("Dot", "working"), 5))
        self.js("window.__fake.running = false; window.__fakeRender(); return true;")
        self.assertTrue(run_until(lambda: self.states[-1] == ("Dot", "idle"), 5))
        # Re-renders that change nothing are not reported again.
        for _ in range(3):
            self.js("window.__fakeRender(); return true;")
        run_until(lambda: False, 0.5)
        self.assertEqual(self.selections, [("Assistente", "home"), ("Dot", "chat")])
        self.assertEqual([s for s in self.states if s[0] == "Dot"],
                         [("Dot", "idle"), ("Dot", "working"), ("Dot", "idle")])

    # -- pets ------------------------------------------------------------------------------

    def test_pets_replace_the_mascots(self):
        view = self.open_app(focus=False)
        view.set_pets(PETS, True)
        pet_of = """const span = document.querySelector(selector);
            if (!span) return null;
            const image = span.querySelector('img');
            const content = getComputedStyle(image).content;
            return [span.getAttribute('data-dotdeck-pet'),
                    content.startsWith('url("data:image/png;base64,') ? 'pet' : content];"""
        uri = "pet"
        self.assertTrue(run_until(lambda: self.js(pet_of, selector=".empty-chat-persona .mascot")
                                  == ["rosy", uri], 5))
        rows = self.js("""return [...document.querySelectorAll('nav[aria-label="Dots"] .mascot')]
                          .map((m) => m.getAttribute('data-dotdeck-pet'));""")
        self.assertEqual(rows, [PETS[name] for name in DOTS])
        # The Dot named "Dot" keeps its own pet; mascots of no Dot get the brand pet.
        self.assertTrue(self.command(view.open_dot, 2, "Dot"))
        self.assertTrue(run_until(lambda: self.js(pet_of, selector=".chat-persona .mascot")
                                  == ["sprout", uri], 5))
        self.assertTrue(self.command(view.start_call, None, 2, "Dot"))
        self.assertEqual(self.js(pet_of, selector=".call-persona .mascot"), ["sprout", uri])
        self.assertTrue(self.command(view.end_call))
        self.assertTrue(self.command(view.open_memories))
        self.assertTrue(run_until(lambda: self.js(pet_of, selector=".large-empty .mascot")
                                  == ["sky", uri], 5))
        # Same pets again: nothing to do. Pets off: the stock mascots come back.
        sheet = view._pets_sheet
        view.set_pets(dict(PETS), True)
        self.assertIs(view._pets_sheet, sheet)
        view.set_pets(PETS, False)
        self.assertTrue(run_until(lambda: self.js(pet_of, selector=".large-empty .mascot")
                                  == [None, "normal"], 5))

    def test_pet_pictures_are_small_data_uris(self):
        from src.web import webview
        uri = webview.pet_data_uri("sky")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertLess(len(uri), 60_000)
        self.assertIsNone(webview.pet_data_uri("not-a-pet"))
        css = webview.pets_style_sheet({"sky", "bolt", "nope"})
        self.assertEqual(css.count("content: url("), 2)
        self.assertIn('span.mascot[data-dotdeck-pet="bolt"] > img.dot-body', css)
        # HiDPI screens get the full picture, untouched.
        from src.core import pets
        sharp = webview.pet_data_uri("sky", 320)
        self.assertEqual(base64.b64decode(sharp.split(",", 1)[1]),
                         pets.image_path("sky").read_bytes())
        self.assertIn(sharp, webview.pets_style_sheet({"sky"}, 320))

    # -- colour scheme ---------------------------------------------------------------------

    def test_the_page_follows_the_colour_scheme(self):
        view = self.open_app()
        theme = """return [localStorage.getItem('opendots-theme'),
                           document.documentElement.getAttribute('data-theme'),
                           document.querySelector('meta[name="theme-color"]')
                           .getAttribute('content'), window.__themeAtStart];"""
        view.sync_color_scheme("dark")
        self.assertTrue(run_until(lambda: self.js(theme)[:3] == ["dark", "dark", "#1b1b1b"], 5))
        view.sync_color_scheme("light")
        self.assertTrue(run_until(lambda: self.js(theme)[:3] == ["light", "light", "#f8f7f4"], 5))
        # Later loads read the choice before the page's first paint.
        self.reload()
        self.assertEqual(self.js(theme)[3], "light")
        view.sync_color_scheme("system")
        self.assertTrue(run_until(lambda: self.js(theme)[0] is None, 5))
        self.reload()
        self.assertEqual(self.js(theme)[3], "system")

    def test_the_page_error_bar_steps_aside_while_offline(self):
        """Dotdeck's banner says the server is out of reach: no second bar, in English."""
        view = self.open_app()
        self.assertTrue(run_until(lambda: self.focus_events == [True], 15))
        bar = """let bar = document.querySelector('.template-app .error-banner');
            if (!bar) {
              bar = document.createElement('div');
              bar.className = 'error-banner';
              bar.textContent = 'Load failed';
              document.querySelector('.template-app').prepend(bar);
            }
            return [document.documentElement.hasAttribute('data-dotdeck-offline'),
                    getComputedStyle(bar).display];"""
        self.assertEqual(self.js(bar), [False, "block"])
        view.set_offline(True)
        self.assertTrue(run_until(lambda: self.js(bar) == [True, "none"], 5))
        # A page loaded while offline starts with the mark.
        self.reload()
        self.assertTrue(run_until(lambda: self.js(bar) == [True, "none"], 15))
        view.set_offline(False)
        self.assertTrue(run_until(lambda: self.js(bar) == [False, "block"], 5))

    def reload(self) -> None:
        finished = []
        handler = self.view.webview.connect(
            "load-changed", lambda _v, event: finished.append(event))
        self.view.reload()
        from gi.repository import WebKit
        self.assertTrue(run_until(lambda: WebKit.LoadEvent.FINISHED in finished, 15))
        self.view.webview.disconnect(handler)
        self.assertTrue(run_until(lambda: self.js("return !!window.__fake;"), 5))

    # -- native dialogs --------------------------------------------------------------------

    def test_confirm_is_a_native_dialog_and_the_page_waits(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_space, "s0", "p1"))
        self.js("window.__fake.dirty = true; return true;")
        # A moment later, so that this script's answer goes out before the page blocks.
        leave = """setTimeout(() => document.querySelector(
            'button[aria-label="Open activity"]').click(), 200); return true;"""
        self.js(leave)
        self.assertTrue(run_until(lambda: view._script_dialog is not None, 10))
        self.assertEqual(view._script_dialog.get_heading(), "Leave Unsaved Draft?")
        self.answer_dialog("cancel")
        self.assertEqual(self.js("return [window.__fake.view, window.__fake.dirty];"),
                         ["space", True])
        # Through a command: the command waits for the answer, then finishes.
        box = []
        view.open_activity(box.append)
        self.answer_dialog("ok")
        self.assertTrue(run_until(lambda: box, 10))
        self.assertEqual(box, [True])
        self.assertEqual(self.js("return window.__fake.view;"), "tasks")

    def test_prompt_and_alert_are_native_dialogs(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_conversation, "c4", 0, "Assistente"))
        save = """setTimeout(() => document.querySelector(
            'button[aria-label="Save conversation as page"]').click(), 200); return true;"""
        self.js(save)
        self.assertTrue(run_until(lambda: view._script_dialog is not None, 10))
        self.assertEqual(view._script_dialog.get_heading(), "Save as Page")
        self.assertEqual(view._script_entry.get_text(), "Agenda da semana")
        self.answer_dialog("ok", text="Minha página")
        self.js(save)
        self.answer_dialog("cancel")
        self.assertTrue(run_until(lambda: len(self.js("return window.__fake.prompts;")) == 2, 5))
        self.assertEqual(self.js("return window.__fake.prompts;"), ["Minha página", None])

        self.js("setTimeout(() => { window.__alerted = alert('Backup finished.') || 'seen'; },"
                " 200); return true;")
        self.assertTrue(run_until(lambda: view._script_dialog is not None, 10))
        self.assertEqual(view._script_dialog.get_heading(), "Backup finished.")
        self.answer_dialog("ok")
        self.assertTrue(run_until(lambda: self.js("return window.__alerted || null;") == "seen",
                                  5))

    def test_the_hidden_chat_header_actions_can_still_be_pressed(self):
        view = self.open_app()
        self.assertTrue(self.command(view.open_conversation, "c4", 0, "Assistente"))

        def action(name: str, dialog: str | None = None) -> bool:
            box = []
            view.run_page_action(name, done=box.append)
            if dialog:
                # The page asks first; the action finishes once it has the answer.
                self.assertEqual(self.dialog_shown().get_heading(), dialog)
                self.answer_dialog("cancel")
            self.assertTrue(run_until(lambda: box, 10))
            return box[0]
        self.assertTrue(action("schedule"))
        self.assertEqual(self.js("return document.querySelector('#dialog-title').textContent;"),
                         "Schedule a task")
        self.js("document.querySelector('.modal-close').click(); return true;")
        self.assertTrue(action("save-page", dialog="Save as Page"))
        # While the Dot works the page disables saving: nothing is pressed.
        self.js("window.__fake.running = true; window.__fakeRender(); return true;")
        self.assertFalse(action("save-page"))
        self.assertIsNone(view._script_dialog)

    def test_destructive_questions_get_native_wording(self):
        view = self.open_app()
        cases = [
            ("confirm(`Delete \"Notas\"? This can't be undone. Any subpages will move to "
             "this page's parent.`)", "Delete Page?",
             "“Notas” will be permanently deleted. Its subpages move to its parent page.",
             "_Delete"),
            ("confirm('Remove GitHub from this Dot?')", "Remove Connection?",
             "This Dot will no longer use GitHub.", "_Remove"),
            ("confirm('Discard the changes to this task?')", "Discard the changes to this task?",
             "", "_Discard"),
            ("confirm('Use the new layout?')", "Use the new layout?", "", "_OK"),
        ]
        for question, heading, body, accept in cases:
            with self.subTest(heading=heading):
                self.js("window.__answer = undefined; return true;")
                self.ask(f"window.__answer = {question};")
                dialog = self.dialog_shown()
                destructive = accept != "_OK"
                self.assertEqual(
                    (dialog.get_heading(), dialog.get_body(), dialog.get_response_label("ok"),
                     dialog.get_response_appearance("ok"), dialog.get_default_response()),
                    (heading, body, accept,
                     Adw.ResponseAppearance.DESTRUCTIVE if destructive
                     else Adw.ResponseAppearance.SUGGESTED,
                     "cancel" if destructive else "ok"))
                self.answer_dialog("ok")
                self.assertTrue(run_until(lambda: self.js("return window.__answer;") is True, 5))

    def test_prompts_accept_only_what_the_page_can_use(self):
        view = self.open_app()
        self.ask("window.__answer = prompt("
                 "'Repeat interval in minutes (0 removes the schedule)', '60');")
        dialog = self.dialog_shown()
        entry = view._script_entry
        self.assertEqual((dialog.get_heading(), entry.get_title(), entry.get_text()),
                         ("Repeat Task", "Minutes", "60"))
        self.assertTrue(dialog.get_response_enabled("ok"))
        for text, enabled, error in (("abc", False, True), ("", False, False), ("-5", False, True),
                                     ("0", True, False), ("1,5", True, False)):
            with self.subTest(text=text):
                entry.set_text(text)
                self.assertEqual((dialog.get_response_enabled("ok"), entry.has_css_class("error")),
                                 (enabled, error))
        self.answer_dialog("ok")
        # The page reads the number with Number(), which wants a dot.
        self.assertTrue(run_until(lambda: self.js("return window.__answer;") == "1.5", 5))

        self.ask("window.__answer = prompt('Page title', '');")
        dialog = self.dialog_shown()
        self.assertFalse(dialog.get_response_enabled("ok"))
        self.answer_dialog("ok", text="Notas da semana")
        self.assertTrue(run_until(lambda: self.js("return window.__answer;")
                                  == "Notas da semana", 5))

    def test_a_question_left_open_is_answered_when_the_view_goes(self):
        view = self.open_app()
        self.ask("window.__answer = confirm('Discard the draft?');")
        dialog = self.dialog_shown()
        # The dialog goes away without a response (as on destroy()); the page still gets
        # its answer instead of waiting forever.
        view._dismiss_script_dialog()
        self.assertIsNone(view._script_dialog)
        self.assertTrue(run_until(lambda: self.js("return window.__answer;") is False, 5))
        self.assertFalse(dialog.get_visible() and dialog.get_mapped())


    def test_a_page_that_asks_before_closing_lets_the_view_go(self):
        view = self.open_app()
        self.js("""window.addEventListener('beforeunload', (event) => {
            event.preventDefault(); event.returnValue = ''; }); return true;""")
        view.reload()
        if not run_until(lambda: view._script_dialog is not None, 5):
            self.skipTest("this WebKit does not ask before unloading here")
        self.assertEqual(view._script_dialog.get_heading(), "Leave This Page?")
        self.answer_dialog("cancel")
        # Closing the view asks again: nobody is there to answer, so the page may close.
        closed = []
        view.webview.connect("close", lambda _w: closed.append(True))
        view.destroy()
        self.assertTrue(run_until(lambda: closed, 10))
        self.assertIsNone(view._script_dialog)


if __name__ == "__main__":
    unittest.main()
