"""The native sidebar: every Dot with its pet, name and job, the Spaces, and shortcuts to
Activity, Memories and Settings.

It only shows data (from the server monitor and the owner's per-Dot choices) and emits
signals; the window decides what each click does on the page.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from ..core import pets  # noqa: E402
from ..core.workspace import Dot, Workspace  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from .pet import PetAvatar  # noqa: E402

AVATAR = {"comfortable": 36, "compact": 26}


def _label(text: str = "", css: tuple[str, ...] = (), xalign: float = 0.0, **kwargs) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, **kwargs)
    for name in css:
        label.add_css_class(name)
    return label


class DotRow(Gtk.ListBoxRow):
    """Pet, name, job line and a status at the end (call, working, pinned)."""

    def __init__(self, dot: Dot, pet_id: str, density: str, show_job: bool):
        super().__init__()
        self.dot = dot
        self.add_css_class("dot-row")
        box = Gtk.Box(spacing=10)
        self.avatar = PetAvatar(pet_id, size=AVATAR.get(density, 36))
        self.avatar.set_valign(Gtk.Align.CENTER)
        box.append(self.avatar)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = _label(dot.name, ("dot-name",), ellipsize=Pango.EllipsizeMode.END)
        self.subtitle = _label("", ("dot-job", "dim-label", "caption"),
                               ellipsize=Pango.EllipsizeMode.END)
        texts.append(self.title)
        texts.append(self.subtitle)
        box.append(texts)

        self.status = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                valign=Gtk.Align.CENTER, hhomogeneous=False)
        self.status.add_named(Gtk.Box(), "none")
        call = Gtk.Image(icon_name="call-start-symbolic", tooltip_text=_("In a call"))
        call.add_css_class("success")
        self.status.add_named(call, "call")
        spinner = Adw.Spinner(tooltip_text=_("Working"))
        spinner.set_size_request(16, 16)
        self.status.add_named(spinner, "working")
        pin = Gtk.Image(icon_name="view-pin-symbolic", tooltip_text=_("Pinned"))
        pin.add_css_class("dim-label")
        self.status.add_named(pin, "pinned")
        box.append(self.status)
        self.set_child(box)
        self.update(dot, pet_id, density=density, show_job=show_job)

    def update(self, dot: Dot, pet_id: str, *, density: str, show_job: bool,
               state: str = "idle", in_call: bool = False, pinned: bool = False,
               offline: bool = False) -> None:
        self.dot = dot
        self.avatar.pet = pet_id
        self.avatar.size = AVATAR.get(density, 36)
        self.avatar.state = "offline" if offline else "speaking" if in_call and state == "speaking" \
            else "call" if in_call else state
        self.title.set_label(dot.name)
        job = dot.job
        self.subtitle.set_label(_("In a call") if in_call else
                                _("Working…") if state == "working" else job)
        self.subtitle.set_visible(bool(show_job and (job or in_call or state == "working")))
        for name in ("comfortable", "compact"):
            self.remove_css_class(name)
        self.add_css_class(density)
        self.status.set_visible_child_name("call" if in_call else
                                           "working" if state == "working" else
                                           "pinned" if pinned else "none")
        pet = pets.get(pet_id) or pets.DEFAULT
        tooltip = f"{dot.name} — {job}" if job else dot.name
        self.set_tooltip_text(tooltip)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_("{name}, {job}").format(name=dot.name, job=job) if job
                              else dot.name])
        self.avatar.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                                    [_("{pet}, pet of {dot}").format(pet=_(pet.name),
                                                                     dot=dot.name)])


class NavRow(Gtk.ListBoxRow):
    def __init__(self, key: str, icon: str, title: str):
        super().__init__()
        self.key = key
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Image(icon_name=icon))
        box.append(_label(title, hexpand=True))
        self.badge = _label("", ("nav-badge", "numeric"), visible=False, xalign=0.5,
                            valign=Gtk.Align.CENTER)
        box.append(self.badge)
        self.set_child(box)

    def set_badge(self, value: int | str | None, accent: bool = False) -> None:
        text = str(value) if value else ""
        self.badge.set_label(text)
        self.badge.set_visible(bool(text))
        if accent:
            self.badge.add_css_class("accent-badge")
        else:
            self.badge.remove_css_class("accent-badge")


class SpaceRow(Gtk.ListBoxRow):
    def __init__(self, space_id: str, name: str):
        super().__init__()
        self.space_id = space_id
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Image(icon_name="folder-symbolic"))
        self.title = _label(name, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(self.title)
        self.set_child(box)
        self.set_tooltip_text(name)


class Sidebar(Adw.Bin):
    """The list part of the sidebar (the header bar belongs to the window)."""

    __gtype_name__ = "DotdeckSidebar"
    __gsignals__ = {
        "dot-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "space-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "nav-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "new-dot": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.add_css_class("dotdeck-sidebar")
        self._rows: dict[str, DotRow] = {}
        self._space_rows: dict[str, SpaceRow] = {}
        self._selecting = False
        self._filter = ""
        self._states: dict[str, str] = {}
        self._hidden_count = 0
        self.menu_for_dot = None   # callable(dot_id) -> Gio.MenuModel, set by the window

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.search = Gtk.SearchEntry(placeholder_text=_("Search Dots and Spaces"),
                                      margin_start=10, margin_end=10, margin_top=4,
                                      margin_bottom=6)
        self.search.connect("search-changed", self._on_search)
        self.search.connect("activate", self._on_search_activate)
        self.search.connect("stop-search", lambda *_: self.hide_search())
        self.search_bar = Gtk.Revealer(child=self.search, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        outer.append(self.search_bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.dots_header = self._section_header(_("Dots"), _("New Dot…"),
                                                lambda *_: self.emit("new-dot"))
        content.append(self.dots_header)
        self.dots = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.dots.add_css_class("navigation-sidebar")
        self.dots.add_css_class("dots-list")
        # Selecting opens (mouse and arrow keys alike); programmatic selection does not.
        self.dots.connect("row-selected", self._on_dot_selected)
        self.dots.set_filter_func(self._filter_dot)
        self.placeholder = _label(_("Connecting…"), ("dim-label", "caption"), xalign=0.5,
                                  margin_top=12, margin_bottom=12, wrap=True)
        self.dots.set_placeholder(self.placeholder)
        # The Dot menu: right click, a long press on a touch screen, or Shift+F10 and the
        # Menu key on the focused Dot (as in GTK's own text fields).
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_dot_context)
        self.dots.add_controller(click)
        press = Gtk.GestureLongPress(touch_only=True)
        press.connect("pressed", lambda g, x, y: self._on_dot_context(g, 1, x, y))
        self.dots.add_controller(press)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Shift>F10|Menu"),
                                           Gtk.CallbackAction.new(self._on_dot_menu_key)))
        self.dots.add_controller(keys)
        content.append(self.dots)

        self.hidden_button = Gtk.Button(halign=Gtk.Align.START, margin_start=12, visible=False)
        self.hidden_button.add_css_class("flat")
        self.hidden_button.add_css_class("caption")
        self.hidden_button.add_css_class("hidden-dots")
        self.hidden_button.connect("clicked", lambda *_: self.emit("nav-activated", "hidden"))
        content.append(self.hidden_button)

        self.spaces_expander = Gtk.Expander(expanded=False, margin_top=8)
        self.spaces_expander.add_css_class("spaces-expander")
        self.spaces_expander.set_label_widget(_label(_("Spaces"), ("sidebar-heading",)))
        self.spaces = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.spaces.add_css_class("navigation-sidebar")
        self.spaces.connect("row-selected", self._on_space_selected)
        self.spaces.set_filter_func(self._filter_space)
        self.spaces_expander.set_child(self.spaces)
        content.append(self.spaces_expander)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                      child=content)
        outer.append(scroller)

        outer.append(Gtk.Separator())
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, margin_top=4,
                               margin_bottom=6)
        self.nav.add_css_class("navigation-sidebar")
        self.nav.connect("row-activated", lambda _l, row: self.emit("nav-activated", row.key))
        self.nav_rows = {
            "activity": NavRow("activity", "task-due-symbolic", _("Activity")),
            "memories": NavRow("memories", "user-bookmarks-symbolic", _("Memories")),
            "settings": NavRow("settings", "emblem-system-symbolic", _("Settings")),
        }
        for row in self.nav_rows.values():
            self.nav.append(row)
        outer.append(self.nav)

        # The chat model at a glance ("Claude Haiku 5.5 · OpenRouter"): opens AI Models.
        self.model_label = _label("", ("caption",), hexpand=True,
                                  ellipsize=Pango.EllipsizeMode.END)
        box = Gtk.Box(spacing=8)
        icon = Gtk.Image(icon_name="applications-science-symbolic")
        icon.add_css_class("dim-label")
        box.append(icon)
        box.append(self.model_label)
        self.model_button = Gtk.Button(child=box, visible=False, margin_start=6, margin_end=6,
                                       margin_bottom=6)
        self.model_button.add_css_class("flat")
        self.model_button.add_css_class("model-glance")
        self.model_button.connect("clicked", lambda *_: self.emit("nav-activated", "model"))
        outer.append(self.model_button)

        # What the OpenRouter key spent ("US$ 0,29 this month · US$ 6,11 left"), under the
        # model, in the same quiet style; also opens AI Models.
        self.spend_label = _label("", ("caption", "numeric"), hexpand=True,
                                  ellipsize=Pango.EllipsizeMode.END)
        box = Gtk.Box(spacing=8)
        sign = _label("$", ("dim-label", "caption-heading"), xalign=0.5, width_chars=2)
        box.append(sign)
        box.append(self.spend_label)
        self.spend_button = Gtk.Button(child=box, visible=False, margin_start=6, margin_end=6,
                                       margin_bottom=6)
        self.spend_button.add_css_class("flat")
        self.spend_button.add_css_class("model-glance")
        self.spend_button.connect("clicked", lambda *_: self.emit("nav-activated", "model"))
        outer.append(self.spend_button)
        self.set_child(outer)

    @staticmethod
    def _section_header(title: str, add_tooltip: str, on_add) -> Gtk.Widget:
        box = Gtk.Box(margin_start=18, margin_end=8, margin_top=6, margin_bottom=2)
        heading = _label(title, ("sidebar-heading",), hexpand=True)
        box.append(heading)
        box._heading = heading
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=add_tooltip,
                         valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.add_css_class("circular")
        add.add_css_class("sidebar-add")
        add.connect("clicked", on_add)
        box.append(add)
        return box

    # -- data ------------------------------------------------------------------------------

    def set_dots(self, workspace: Workspace, visible: list[Dot], hidden: list[Dot],
                 pet_map: dict[str, str], *, density: str, show_job: bool,
                 pinned: set[str], in_call: str | None, offline: bool) -> None:
        """Rebuild or update the rows; keeps the selection."""
        selected = self.selected_dot()
        order = [d.id for d in visible]
        if order != [r.dot.id for r in self._iter_rows()]:
            # Pin or Hide from the keyboard: the focus stays with the Dot (or the list).
            focused = next((r.dot.id for r in self._iter_rows() if r.has_focus()), None)
            self._selecting = True
            for row in list(self._iter_rows()):
                self.dots.remove(row)
            self._rows.clear()
            for dot in visible:
                row = DotRow(dot, pet_map.get(dot.id, pets.DEFAULT.id), density, show_job)
                self._rows[dot.id] = row
                self.dots.append(row)
            self._selecting = False
            if focused is not None:
                self._rows.get(focused, self.dots).grab_focus()
        for dot in visible:
            self._rows[dot.id].update(dot, pet_map.get(dot.id, pets.DEFAULT.id), density=density,
                                      show_job=show_job, state=self._states.get(dot.id, "idle"),
                                      in_call=dot.id == in_call, pinned=dot.id in pinned,
                                      offline=offline)
        self._hidden_count = len(hidden)
        self.hidden_button.set_label(ngettext("{n} hidden Dot", "{n} hidden Dots",
                                              len(hidden)).format(n=len(hidden)))
        self.hidden_button.set_visible(bool(hidden))
        self.dots_header._heading.set_label(_("Dots"))
        if selected:
            self.select_dot(selected)

        spaces = [(s.id, s.name) for s in workspace.spaces]
        if spaces != [(r.space_id, r.title.get_label()) for r in self._iter_space_rows()]:
            for row in list(self._iter_space_rows()):
                self.spaces.remove(row)
            self._space_rows.clear()
            for space_id, name in spaces:
                row = SpaceRow(space_id, name)
                self._space_rows[space_id] = row
                self.spaces.append(row)
        self.spaces_expander.set_visible(bool(spaces))

    def set_dot_state(self, dot_id: str | None, state: str) -> None:
        """What the page says the open Dot is doing (idle, working, paused)."""
        for key in [k for k, v in self._states.items() if v != "idle"]:
            if key != dot_id:
                self._states[key] = "idle"
        if dot_id:
            self._states[dot_id] = state

    def set_spend(self, text: str, tooltip: str = "") -> None:
        """The OpenRouter spend line, or "" to hide it (no key, or an older server)."""
        self.spend_label.set_label(text)
        self.spend_button.set_visible(bool(text))
        self.spend_button.set_tooltip_text(tooltip or None)
        # One gap under the pair, not one under each line.
        self.model_button.set_margin_bottom(0 if text else 6)
        self.spend_button.update_property([Gtk.AccessibleProperty.LABEL],
                                          [_("OpenRouter spend: {spend}").format(spend=text)])

    def set_model(self, text: str) -> None:
        """The chat model the Dots use, or "" to hide the line (servers that do not say)."""
        self.model_label.set_label(text)
        self.model_button.set_visible(bool(text))
        tooltip = _("Chat model: {model}. Click to change it in AI Models.").format(
            model=text) if text else None
        self.model_button.set_tooltip_text(tooltip)
        self.model_button.update_property([Gtk.AccessibleProperty.LABEL],
                                          [_("Chat model: {model}").format(model=text)])

    def set_counts(self, activity: int, memories: int) -> None:
        self.nav_rows["activity"].set_badge(activity, accent=True)
        self.nav_rows["memories"].set_badge(memories)

    # -- selection -------------------------------------------------------------------------

    def _iter_rows(self):
        child = self.dots.get_first_child()
        while child is not None:
            if isinstance(child, DotRow):
                yield child
            child = child.get_next_sibling()

    def _iter_space_rows(self):
        child = self.spaces.get_first_child()
        while child is not None:
            if isinstance(child, SpaceRow):
                yield child
            child = child.get_next_sibling()

    def visible_dot_ids(self) -> list[str]:
        return [row.dot.id for row in self._iter_rows() if row.get_child_visible()]

    def selected_dot(self) -> str | None:
        row = self.dots.get_selected_row()
        return row.dot.id if isinstance(row, DotRow) else None

    def select_dot(self, dot_id: str | None) -> None:
        self._selecting = True
        row = self._rows.get(dot_id or "")
        if row is not None:
            self.dots.select_row(row)
            self.spaces.unselect_all()
            self.nav.unselect_all()
        else:
            self.dots.unselect_all()
        self._selecting = False

    def select_space(self, space_id: str | None) -> None:
        self._selecting = True
        row = self._space_rows.get(space_id or "")
        self.dots.unselect_all()
        self.nav.unselect_all()
        if row is not None:
            self.spaces_expander.set_expanded(True)
            self.spaces.select_row(row)
        self._selecting = False

    def select_nav(self, key: str | None) -> None:
        self._selecting = True
        row = self.nav_rows.get(key or "")
        self.dots.unselect_all()
        self.spaces.unselect_all()
        if row is not None and key != "settings":
            self.nav.select_row(row)
        else:
            self.nav.unselect_all()
        self._selecting = False

    def _on_dot_selected(self, _list, row) -> None:
        if self._selecting or not isinstance(row, DotRow):
            return
        self._selecting = True
        self.spaces.unselect_all()
        self.nav.unselect_all()
        self._selecting = False
        self.emit("dot-activated", row.dot.id)

    def _on_space_selected(self, _list, row) -> None:
        if self._selecting or not isinstance(row, SpaceRow):
            return
        self._selecting = True
        self.dots.unselect_all()
        self.nav.unselect_all()
        self._selecting = False
        self.emit("space-activated", row.space_id)

    def _on_dot_context(self, gesture, _n, x, y) -> None:
        row = self.dots.get_row_at_y(int(y))
        if isinstance(row, DotRow) and self._open_dot_menu(row, _rect(x, y)):
            if hasattr(gesture, "set_state"):
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_dot_menu_key(self, *_args) -> bool:
        """Shift+F10 or the Menu key: the menu of the focused Dot, under its row."""
        row = self.dots.get_focus_child()
        if not isinstance(row, DotRow):
            row = self.dots.get_selected_row()
        if not isinstance(row, DotRow):
            return False
        ok, bounds = row.compute_bounds(self.dots)
        if not ok:
            return False
        area = _rect(bounds.get_x(), bounds.get_y())
        area.width, area.height = int(bounds.get_width()), int(bounds.get_height())
        return self._open_dot_menu(row, area)

    def _open_dot_menu(self, row: DotRow, area) -> bool:
        """Show the Dot's menu pointing at ``area`` (in the coordinates of the Dots list)."""
        if self.menu_for_dot is None:
            return False
        menu = self.menu_for_dot(row.dot.id)
        if menu is None:
            return False
        # Parented to the sidebar, not to the list or the row: Pin and Hide rebuild the rows
        # while the menu is open, and Gtk.ListBox never re-sizes a popover child, so the menu
        # kept the height it had before its section separator appeared and had to scroll.
        # The sidebar (an Adw.Bin) re-presents its popovers on every allocation.
        _ok, origin = self.dots.compute_point(self, Graphene.Point().init(area.x, area.y))
        area.x, area.y = int(origin.x), int(origin.y)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        # Opens to the right of the pointer (left in right-to-left languages) and below it,
        # like GTK's own context menus, so the Dot's name and job stay visible.
        popover.set_halign(Gtk.Align.START)
        popover.set_pointing_to(area)
        row.add_css_class("menu-open")

        def closed(popover):
            row.remove_css_class("menu-open")
            # GTK hands the focus back to the menu's parent, and the sidebar cannot take it,
            # so it fell out of the list (onto the header's New Chat button). It goes back to
            # the Dot, before the chosen item runs: Open or New Chat can still move it on.
            root = self.get_root()
            focus = root.get_focus() if root is not None else None
            if focus is None or focus.is_ancestor(popover):
                (row if row.get_parent() is self.dots else self.dots).grab_focus()
            GLib.idle_add(popover.unparent)
        popover.connect("closed", closed)
        popover.popup()
        return True

    # -- search ----------------------------------------------------------------------------

    def show_search(self) -> None:
        self.search_bar.set_reveal_child(True)
        self.search.grab_focus()

    def hide_search(self) -> None:
        self.search.set_text("")
        self.search_bar.set_reveal_child(False)

    def toggle_search(self) -> None:
        if self.search_bar.get_reveal_child():
            self.hide_search()
        else:
            self.show_search()

    def set_placeholder(self, text: str) -> None:
        """What the empty Dots list says (while connecting, offline, no Dots yet)."""
        self._empty_text = text
        if not self._filter:
            self.placeholder.set_label(text)

    def _on_search(self, entry) -> None:
        self._filter = entry.get_text().strip().casefold()
        self.placeholder.set_label(_("No Dots match your search") if self._filter
                                   else getattr(self, "_empty_text", ""))
        self.dots.invalidate_filter()
        self.spaces.invalidate_filter()
        if self._filter:
            self.spaces_expander.set_expanded(True)

    def _on_search_activate(self, _entry) -> None:
        for row in self._iter_rows():
            if self._filter_dot(row):
                self.hide_search()
                self.select_dot(row.dot.id)
                self.emit("dot-activated", row.dot.id)
                return
        for row in self._iter_space_rows():
            if self._filter_space(row):
                self.hide_search()
                self.emit("space-activated", row.space_id)
                return

    def _filter_dot(self, row) -> bool:
        if not self._filter or not isinstance(row, DotRow):
            return True
        return self._filter in f"{row.dot.name} {row.dot.job}".casefold()

    def _filter_space(self, row) -> bool:
        if not self._filter or not isinstance(row, SpaceRow):
            return True
        return self._filter in row.title.get_label().casefold()


def _rect(x: float, y: float):
    from gi.repository import Gdk
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect


def dot_menu(dot: Dot, *, pinned: bool, chief: bool, can_call: bool) -> Gio.Menu:
    """Right-click menu of a Dot (actions live on the window, with the Dot id as target)."""
    target = GLib.Variant("s", dot.id)
    menu = Gio.Menu()
    section = Gio.Menu()
    for label, action, enabled in ((_("Open"), "win.dot-open", True),
                                   (_("New Chat"), "win.dot-new-chat", True),
                                   (_("Call"), "win.dot-call", can_call)):
        if not enabled:
            continue
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, target)
        section.append_item(item)
    menu.append_section(None, section)
    section = Gio.Menu()
    for label, action in ((_("Change Pet…"), "win.dot-pet"),
                          (_("Edit Dot…"), "win.dot-edit"),
                          (_("Unpin") if pinned else _("Pin to Top"), "win.dot-pin"),
                          (_("Hide From Sidebar"), "win.dot-hide")):
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, target)
        section.append_item(item)
    if not chief:
        item = Gio.MenuItem.new(_("Make Chief"), None)
        item.set_action_and_target_value("win.dot-chief", target)
        section.append_item(item)
    menu.append_section(None, section)
    return menu
