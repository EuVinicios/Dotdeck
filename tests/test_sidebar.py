"""The native sidebar: rows, selection, keyboard and the Dot context menu."""

from __future__ import annotations

import unittest

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.core.workspace import Workspace  # noqa: E402
from src.ui import pet as pet_ui  # noqa: E402
from src.ui.sidebar import DotRow, Sidebar, dot_menu  # noqa: E402

WORKSPACE = Workspace.from_json({
    "spaces": [{"id": "s1", "name": "Everyday"}],
    "dots": [{"id": "a", "name": "Chefe", "instructions": "# Chefe — chefe de gabinete"},
             {"id": "b", "name": "Zelador", "instructions": "# Zelador — SRE da casa"}],
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
        self.assertEqual([r.dot.name for r in rows], ["Chefe", "Zelador"])
        self.assertEqual(rows[1].subtitle.get_label(), "SRE da casa")
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

    def test_context_menu_opens_and_survives_a_rebuild(self):
        self.sidebar.menu_for_dot = lambda dot_id: dot_menu(WORKSPACE.dot(dot_id), pinned=False,
                                                            chief=False, can_call=True)
        row = self.rows()[0]
        _ok, bounds = row.compute_bounds(self.sidebar.dots)
        self.sidebar._on_dot_context(Gtk.GestureClick(), 1, 20, bounds.get_y() + 10)
        popovers = [c for c in _children(self.sidebar.dots) if isinstance(c, Gtk.PopoverMenu)]
        self.assertEqual(len(popovers), 1)
        # Pin to Top rebuilds every row while the menu is open
        self.fill(pinned={"b"})
        popovers[0].popdown()
        run_until(lambda: not any(isinstance(c, Gtk.PopoverMenu)
                                  for c in _children(self.sidebar.dots)), 2)

    def test_hidden_count_and_search(self):
        self.fill(hidden={"b"})
        self.assertTrue(self.sidebar.hidden_button.get_visible())
        self.fill()
        self.sidebar.search.set_text("sre")
        run_until(lambda: False, 0.3)
        visible = [r.dot.id for r in self.rows() if r.get_child_visible()]
        self.assertEqual(visible, ["b"])

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


if __name__ == "__main__":
    unittest.main()
