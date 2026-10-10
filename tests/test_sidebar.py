"""The native sidebar: rows, selection, keyboard and the Dot context menu."""

from __future__ import annotations

import unittest

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Graphene, Gtk  # noqa: E402

from src.core.workspace import Workspace  # noqa: E402
from src.ui import pet as pet_ui  # noqa: E402
from src.ui.sidebar import DotRow, Sidebar, dot_menu  # noqa: E402

WORKSPACE = Workspace.from_json({
    "spaces": [{"id": "s1", "name": "Everyday"}],
    "dots": [{"id": "a", "name": "Assistente",
              "instructions": "# Assistente — organiza o dia e os outros Dots"},
             {"id": "b", "name": "Manutenção",
              "instructions": "# Manutenção — backups e o servidor de casa"}],
})


class SidebarTests(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.sidebar = Sidebar()
        self.window = Gtk.Window(child=self.sidebar, default_width=320, default_height=600)
        self.window.present()
        self.opened = []
        self.sidebar.connect("dot-activated", lambda _s, dot_id: self.opened.append(dot_id))
        self.fill()

    def tearDown(self):
        self.window.destroy()

    def fill(self, pinned=frozenset(), in_call=None, hidden=()):
        dots = [d for d in WORKSPACE.dots if d.id not in hidden]
        self.sidebar.set_dots(WORKSPACE, sorted(dots, key=lambda d: d.id not in pinned),
                              [d for d in WORKSPACE.dots if d.id in hidden],
                              {"a": "sky", "b": "bolt"}, density="comfortable", show_job=True,
                              pinned=set(pinned), in_call=in_call, offline=False)
        run_until(lambda: False, 0.1)

    def rows(self):
        return [r for r in self.sidebar._iter_rows()]

    def test_rows_show_name_job_and_pet(self):
        rows = self.rows()
        self.assertEqual([r.dot.name for r in rows], ["Assistente", "Manutenção"])
        self.assertEqual(rows[1].subtitle.get_label(), "backups e o servidor de casa")
        self.assertEqual(rows[1].avatar.pet, "bolt")

    def test_selecting_opens_but_programmatic_selection_does_not(self):
        self.sidebar.select_dot("a")
        self.assertEqual(self.opened, [])
        # what a click or the arrow keys do: the list selects the row
        self.sidebar.dots.select_row(self.rows()[1])
        self.assertEqual(self.opened, ["b"])

    def test_call_badge_and_pin(self):
        self.fill(pinned={"b"}, in_call="a")
        rows = self.rows()
        self.assertEqual([r.dot.id for r in rows], ["b", "a"])
        self.assertEqual(rows[1].status.get_visible_child_name(), "call")
        self.assertEqual(rows[0].status.get_visible_child_name(), "pinned")

    def menus(self):
        """The Dot menus open now."""
        return [c for c in _descendants(self.sidebar) if isinstance(c, Gtk.PopoverMenu)]

    def test_context_menu_opens_and_survives_a_rebuild(self):
        self.sidebar.menu_for_dot = lambda dot_id: dot_menu(WORKSPACE.dot(dot_id), pinned=False,
                                                            chief=False, can_call=True)
        row = self.rows()[0]
        _ok, bounds = row.compute_bounds(self.sidebar.dots)
        self.sidebar._on_dot_context(Gtk.GestureClick(), 1, 20, bounds.get_y() + 10)
        popovers = self.menus()
        self.assertEqual(len(popovers), 1)
        # Pin to Top rebuilds every row while the menu is open
        self.fill(pinned={"b"})
        popovers[0].popdown()
        self.assertTrue(run_until(lambda: not self.menus(), 2))

    def test_context_menu_shows_every_item_without_scrolling(self):
        """The whole menu fits: no item hidden behind a scroll, on any Dot.

        Parented to the Gtk.ListBox, GTK sized the menu once, before its section separator
        appeared, and never again: it came out 13 px short and the last item needed a scroll.
        """
        self.sidebar.menu_for_dot = lambda dot_id: dot_menu(
            WORKSPACE.dot(dot_id), pinned=False, chief=dot_id == "a", can_call=True)
        for row in self.rows():                 # the Chief (7 items) and another Dot (8)
            with self.subTest(dot=row.dot.name):
                _ok, bounds = row.compute_bounds(self.sidebar.dots)
                y = bounds.get_y() + 10
                self.sidebar._on_dot_context(Gtk.GestureClick(), 1, 40, y)
                popover, = self.menus()
                try:
                    self.check_menu_fits(popover, row, y)
                finally:
                    popover.popdown()
                    self.assertTrue(run_until(lambda: not self.menus(), 2))
                self.assertFalse(row.has_css_class("menu-open"))

    def check_menu_fits(self, popover, row, y):
        scroller = next(w for w in _descendants(popover) if isinstance(w, Gtk.ScrolledWindow))
        adjustment = scroller.get_vadjustment()

        def fits():
            return popover.get_mapped() and adjustment.get_page_size() > 0 and \
                adjustment.get_upper() <= adjustment.get_page_size() + 0.5
        run_until(fits, 3)
        run_until(lambda: False, 0.3)     # and it stays that way
        self.assertTrue(popover.get_mapped())
        self.assertGreater(adjustment.get_page_size(), 0)
        self.assertLessEqual(adjustment.get_upper(), adjustment.get_page_size() + 0.5)
        # It points at the click (now in the sidebar's coordinates) and opens to the right
        # of it; the Dot stays highlighted while its menu is open.
        _ok, point = self.sidebar.dots.compute_point(self.sidebar, Graphene.Point().init(40, y))
        _ok, area = popover.get_pointing_to()
        self.assertEqual((area.x, area.y), (int(point.x), int(point.y)))
        self.assertEqual(popover.get_halign(), Gtk.Align.START)
        self.assertTrue(row.has_css_class("menu-open"))

    def test_shift_f10_and_the_menu_key_open_the_focused_dots_menu(self):
        opened = []
        self.sidebar.menu_for_dot = lambda dot_id: (opened.append(dot_id), dot_menu(
            WORKSPACE.dot(dot_id), pinned=False, chief=False, can_call=True))[1]
        shortcut = next((item for c in self.sidebar.dots.observe_controllers()
                         if isinstance(c, Gtk.ShortcutController) for item in c
                         if item.get_trigger().to_string() == "<Shift>F10|Menu"), None)
        self.assertIsNotNone(shortcut)
        self.sidebar.dots.unselect_all()
        self.window.set_focus(None)
        self.assertFalse(shortcut.get_action().activate(0, self.sidebar.dots, None))
        row = self.rows()[1]
        row.grab_focus()
        self.assertTrue(shortcut.get_action().activate(0, self.sidebar.dots, None))
        self.assertEqual(opened, ["b"])
        popover, = self.menus()
        # Under the focused row, from its left edge.
        _ok, bounds = row.compute_bounds(self.sidebar)
        _ok, area = popover.get_pointing_to()
        self.assertEqual((area.x, area.y, area.height),
                         (int(bounds.get_x()), int(bounds.get_y()), int(bounds.get_height())))
        popover.popdown()
        self.assertTrue(run_until(lambda: not self.menus(), 2))

    def test_a_long_press_is_for_touch_screens(self):
        press = next(c for c in self.sidebar.dots.observe_controllers()
                     if isinstance(c, Gtk.GestureLongPress))
        self.assertTrue(press.get_touch_only())

    def test_hidden_count_and_search(self):
        self.fill(hidden={"b"})
        self.assertTrue(self.sidebar.hidden_button.get_visible())
        self.fill()
        self.sidebar.search.set_text("backups")
        run_until(lambda: False, 0.3)
        visible = [r.dot.id for r in self.rows() if r.get_child_visible()]
        self.assertEqual(visible, ["b"])

    def test_the_model_at_a_glance_opens_ai_models(self):
        self.assertFalse(self.sidebar.model_button.get_visible())    # server did not say
        keys = []
        self.sidebar.connect("nav-activated", lambda _s, key: keys.append(key))
        self.sidebar.set_model("Claude Haiku 5.5 · OpenRouter")
        self.assertTrue(self.sidebar.model_button.get_visible())
        self.assertEqual(self.sidebar.model_label.get_label(), "Claude Haiku 5.5 · OpenRouter")
        self.assertIn("Claude Haiku 5.5 · OpenRouter",
                      self.sidebar.model_button.get_tooltip_text())
        self.sidebar.model_button.emit("clicked")
        self.assertEqual(keys, ["model"])
        self.sidebar.set_model("")
        self.assertFalse(self.sidebar.model_button.get_visible())

    def test_animations_off_stops_running_avatars(self):
        row = self.rows()[0]
        row.avatar.state = "working"
        run_until(lambda: bool(row.avatar._tick), 1)
        pet_ui.set_animations("off")
        try:
            self.assertFalse(row.avatar._tick)
        finally:
            pet_ui.set_animations("full")


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _descendants(widget):
    for child in _children(widget):
        yield child
        yield from _descendants(child)


if __name__ == "__main__":
    unittest.main()
