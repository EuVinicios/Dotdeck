"""The native sidebar: rows, selection, keyboard and the Dot context menu."""

from __future__ import annotations

import time
import unittest

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Graphene, Gtk  # noqa: E402

from src.core.workspace import Workspace  # noqa: E402
from src.ui import pet as pet_ui  # noqa: E402
from src.ui.sidebar import DotRow, Sidebar, compact_age, dot_menu  # noqa: E402

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

    def fill(self, pinned=frozenset(), in_call=None, hidden=(), **extra):
        dots = [d for d in WORKSPACE.dots if d.id not in hidden]
        self.sidebar.set_dots(WORKSPACE, sorted(dots, key=lambda d: d.id not in pinned),
                              [d for d in WORKSPACE.dots if d.id in hidden],
                              {"a": "sky", "b": "bolt"}, density="comfortable", show_job=True,
                              pinned=set(pinned), in_call=in_call, offline=False, **extra)
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

    def tiles(self):
        return list(self.sidebar._iter_tiles())

    def test_call_badge_and_pin(self):
        """A pinned Dot leaves the list for the grid of pets; the one in a call shows it."""
        self.fill(pinned={"b"}, in_call="a")
        self.assertEqual([t.dot.id for t in self.tiles()], ["b"])
        rows = self.rows()
        self.assertEqual([r.dot.id for r in rows], ["a"])
        self.assertEqual(rows[0].status.get_visible_child_name(), "call")
        self.assertEqual(rows[0].subtitle.get_label(), "In a call")
        self.assertEqual(self.sidebar.visible_dot_ids(), ["b", "a"])     # Ctrl+1…9 order
        self.assertTrue(self.sidebar.pinned_header.get_visible())

    def test_choosing_a_pinned_pet_opens_its_dot(self):
        self.fill(pinned={"a"})
        tile, = self.tiles()
        self.sidebar.select_dot("a")                  # programmatic: opens nothing
        self.assertEqual(self.opened, [])
        self.assertEqual(self.sidebar.selected_dot(), "a")
        self.sidebar.select_dot("b")                  # one selection between grid and list
        self.assertEqual(self.sidebar.pinned.get_selected_children(), [])
        self.sidebar.pinned.select_child(tile)        # what a click or the arrows do
        self.assertEqual(self.opened, ["a"])
        self.assertIsNone(self.sidebar.dots.get_selected_row())

    def test_every_dot_pinned_leaves_no_empty_list(self):
        self.fill(pinned={"a", "b"})
        self.assertFalse(self.sidebar.dots.get_visible())
        self.fill()
        self.assertTrue(self.sidebar.dots.get_visible())
        self.assertFalse(self.sidebar.pinned.get_visible())

    def test_a_pinned_pet_has_the_dot_menu(self):
        menus = []
        self.sidebar.menu_for_dot = lambda dot_id: (menus.append(dot_id), dot_menu(
            WORKSPACE.dot(dot_id), pinned=True, chief=False, can_call=True))[1]
        self.fill(pinned={"a"})
        tile, = self.tiles()
        _ok, bounds = tile.compute_bounds(self.sidebar.pinned)
        x, y = bounds.get_x() + 10, bounds.get_y() + 10
        self.sidebar._on_tile_context(Gtk.GestureClick(), 1, x, y)
        self.assertEqual(menus, ["a"])
        popover, = self.menus()
        self.assertTrue(tile.has_css_class("menu-open"))
        popover.popdown()
        self.assertTrue(run_until(lambda: not self.menus(), 2))
        self.assertFalse(tile.has_css_class("menu-open"))

    def test_hidden_dots_fold_at_the_end(self):
        self.fill(hidden={"b"})
        self.assertTrue(self.sidebar.hidden_expander.get_visible())
        self.assertFalse(self.sidebar.hidden_expander.get_expanded())
        self.assertEqual(self.sidebar.hidden_count.get_label(), "1")
        self.assertEqual(self.sidebar.visible_dot_ids(), ["a"])
        hidden, = self.sidebar._iter_hidden_rows()
        self.sidebar.hidden_list.select_row(hidden)  # opening a hidden Dot still works
        self.assertEqual(self.opened, ["b"])
        self.assertIsNone(self.sidebar.dots.get_selected_row())
        self.fill()
        self.assertFalse(self.sidebar.hidden_expander.get_visible())

    def test_a_hidden_dots_menu_brings_it_back(self):
        actions = _menu_actions(dot_menu(WORKSPACE.dot("b"), pinned=False, chief=False,
                                         can_call=True, hidden=True))
        self.assertIn("win.dot-unhide", actions)
        self.assertNotIn("win.dot-hide", actions)
        self.assertNotIn("win.dot-pin", actions)
        actions = _menu_actions(dot_menu(WORKSPACE.dot("b"), pinned=False, chief=False,
                                         can_call=True))
        self.assertEqual(actions[-1], "win.dot-hide")    # last, in a section of its own

    def test_the_second_line_says_what_is_new_and_the_pet_is_marked(self):
        now = time.time()
        self.fill(chief="b", unread={"a"},
                  activity={"a": (now - 300, "Backup report"), "b": (now - 1800, "Plan")})
        a, b = self.rows()
        self.assertEqual(a.subtitle.get_label(), "New: Backup report")
        self.assertEqual(a.when.get_label(), "5 min")
        self.assertEqual(a.avatar.badge, "unread")
        self.assertTrue(a.has_css_class("unread"))
        self.assertFalse(a.avatar.star)
        self.assertEqual(b.subtitle.get_label(), "backups e o servidor de casa")
        self.assertEqual(b.when.get_label(), "30 min")
        self.assertEqual(b.avatar.badge, "")
        self.assertTrue(b.avatar.star)                 # the Chief
        self.sidebar.set_dot_state("b", "working")
        self.fill(chief="b")
        a, b = self.rows()
        self.assertEqual(b.subtitle.get_label(), "Working…")
        self.assertEqual(a.subtitle.get_label(), "organiza o dia e os outros Dots")
        self.assertFalse(a.when.get_visible())         # no conversation, no age

    def test_the_rail_keeps_only_the_pets(self):
        self.sidebar.set_model("Claude Haiku 5.5 · OpenRouter")
        self.fill(pinned={"b"}, hidden={"a"})
        self.sidebar.set_rail(True)
        tile, = self.tiles()
        self.assertFalse(tile.name.get_visible())
        self.assertFalse(self.sidebar.dots_header.get_visible())
        self.assertFalse(self.sidebar.pinned_header.get_visible())
        self.assertFalse(self.sidebar.hidden_expander.get_visible())
        self.assertFalse(self.sidebar.model_button.get_visible())
        self.assertFalse(self.sidebar.nav_rows["settings"].label.get_visible())
        self.assertEqual(self.sidebar.nav_rows["settings"].get_tooltip_text(), "Settings")
        self.fill(hidden={"a"})                         # rows made while in the rail
        row, = self.rows()
        self.assertFalse(row.texts.get_visible())
        self.sidebar.set_rail(False)
        self.assertTrue(row.texts.get_visible())
        self.assertTrue(self.sidebar.model_button.get_visible())
        self.assertTrue(self.sidebar.hidden_expander.get_visible())
        self.assertIsNone(self.sidebar.nav_rows["settings"].get_tooltip_text())

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

    def test_the_focus_goes_back_to_the_dot_when_its_menu_closes(self):
        """Escape, or an item that rebuilds the rows: the arrows go on from the same Dot.

        The menu belongs to the sidebar, which cannot take the focus GTK hands back to it.
        """
        self.sidebar.menu_for_dot = lambda dot_id: dot_menu(
            WORKSPACE.dot(dot_id), pinned=False, chief=False, can_call=True)
        row = self.rows()[1]
        row.grab_focus()
        self.assertTrue(self.sidebar._on_dot_menu_key())
        popover, = self.menus()
        self.assertTrue(run_until(popover.get_mapped, 3))
        self.assertFalse(row.has_focus())          # the menu has it while open
        popover.popdown()
        self.assertTrue(run_until(lambda: not self.menus(), 2))
        self.assertIs(self.window.get_focus(), row)
        # Pin to Top from the keyboard moves the Dot to the grid: its pet keeps the focus.
        self.fill(pinned={"b"})
        tile, = self.tiles()
        self.assertEqual(tile.dot.id, "b")
        self.assertIs(self.window.get_focus(), tile)
        # Hide From Sidebar: the Dot is folded away, the list keeps the focus.
        self.fill(hidden={"b"})
        self.assertIs(self.window.get_focus(), self.sidebar.dots)
        self.assertEqual(self.opened, [])          # the focus alone opens no Dot

    def test_a_long_press_is_for_touch_screens(self):
        press = next(c for c in self.sidebar.dots.observe_controllers()
                     if isinstance(c, Gtk.GestureLongPress))
        self.assertTrue(press.get_touch_only())

    def test_search(self):
        self.sidebar.search.set_text("backups")
        run_until(lambda: False, 0.3)
        visible = [r.dot.id for r in self.rows() if r.get_child_visible()]
        self.assertEqual(visible, ["b"])

    def test_search_finds_pinned_and_hidden_dots(self):
        self.fill(pinned={"a"}, hidden={"b"})
        self.sidebar.search.set_text("organiza")
        run_until(lambda: False, 0.3)
        self.assertEqual(self.sidebar.visible_dot_ids(), ["a"])
        self.assertEqual(self.sidebar.placeholder.get_label(), "")   # found, in the grid
        self.sidebar.search.set_text("backups")
        run_until(lambda: False, 0.3)
        self.assertEqual(self.sidebar.visible_dot_ids(), [])
        self.assertTrue(self.sidebar.hidden_expander.get_expanded())
        self.sidebar.search.emit("activate")
        self.assertEqual(self.opened, ["b"])

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

    def test_the_openrouter_spend_sits_under_the_model(self):
        self.assertFalse(self.sidebar.spend_button.get_visible())    # server did not say
        keys = []
        self.sidebar.connect("nav-activated", lambda _s, key: keys.append(key))
        self.sidebar.set_model("Claude Haiku 5.5 · OpenRouter")
        self.assertEqual(self.sidebar.model_button.get_margin_bottom(), 6)
        self.sidebar.set_spend("US$ 0.29 this month · US$ 6.11 left", "Today: US$ 0.18")
        self.assertTrue(self.sidebar.spend_button.get_visible())
        self.assertEqual(self.sidebar.spend_label.get_label(),
                         "US$ 0.29 this month · US$ 6.11 left")
        self.assertEqual(self.sidebar.spend_button.get_tooltip_text(), "Today: US$ 0.18")
        self.assertEqual(self.sidebar.model_button.get_margin_bottom(), 0)   # one gap
        self.sidebar.spend_button.emit("clicked")
        self.assertEqual(keys, ["model"])
        self.sidebar.set_spend("")
        self.assertFalse(self.sidebar.spend_button.get_visible())
        self.assertEqual(self.sidebar.model_button.get_margin_bottom(), 6)

    def test_animations_off_stops_running_avatars(self):
        row = self.rows()[0]
        row.avatar.state = "working"
        run_until(lambda: bool(row.avatar._tick), 1)
        pet_ui.set_animations("off")
        try:
            self.assertFalse(row.avatar._tick)
        finally:
            pet_ui.set_animations("full")


class CompactAgeTests(unittest.TestCase):
    def test_ages(self):
        now = time.mktime((2026, 10, 12, 15, 0, 0, 0, 0, -1))
        self.assertEqual(compact_age(0, now), "")
        self.assertEqual(compact_age(now - 20, now), "now")
        self.assertEqual(compact_age((now - 180) * 1000, now), "3 min")   # milliseconds too
        self.assertEqual(compact_age(now - 2 * 3600, now), "2 h")
        self.assertEqual(compact_age(now - 16 * 3600, now), "Yesterday")  # 23:00 the day before
        self.assertEqual(compact_age(now - 4 * 86400, now), "4 d")
        self.assertEqual(compact_age(now - 15 * 86400, now), "2 wk")
        self.assertEqual(compact_age(now - 70 * 86400, now), "2 mo")
        self.assertEqual(compact_age(now - 800 * 86400, now), "2 yr")


def _menu_actions(menu) -> list[str]:
    actions = []
    for section in range(menu.get_n_items()):
        items = menu.get_item_link(section, "section")
        for index in range(items.get_n_items()):
            actions.append(items.get_item_attribute_value(index, "action").unpack())
    return actions


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
