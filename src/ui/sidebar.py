"""The native sidebar: every Dot with its pet, name and job, the Spaces, and shortcuts to
Activity, Memories and Settings.

As in the Grok Bot app (ADR-029): pinned Dots sit in a grid of big pets at the top, the
hidden ones in a folded group at the bottom, and the whole sidebar can shrink to a rail of
pets. It only shows data (from the server monitor and the owner's per-Dot choices) and
emits signals; the window decides what each click does on the page.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from ..core import pets  # noqa: E402
from ..core.workspace import Dot, Workspace  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from .call_panel import LevelWave  # noqa: E402
from .pet import PetAvatar  # noqa: E402

AVATAR = {"comfortable": 36, "compact": 26}
TILE = {"comfortable": 52, "compact": 40}
RAIL_AVATAR = 40


def _label(text: str = "", css: tuple[str, ...] = (), xalign: float = 0.0, **kwargs) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, **kwargs)
    for name in css:
        label.add_css_class(name)
    return label


def compact_age(timestamp: float, now: float | None = None) -> str:
    """How long ago, in the fewest characters: "now", "3 min", "2 h", "Yesterday", "4 d",
    "3 wk", "2 mo", "1 yr" (timestamps in seconds or milliseconds), as the Grok Bot app
    dates its bots. "" for no time at all."""
    if timestamp <= 0:
        return ""
    if timestamp > 1e12:
        timestamp /= 1000
    now = time.time() if now is None else now
    delta = max(0.0, now - timestamp)
    if delta < 60:
        return _("now")
    if delta < 3600:
        return _("{n} min").format(n=int(delta // 60))
    then = GLib.DateTime.new_from_unix_local(int(timestamp))
    today = GLib.DateTime.new_from_unix_local(int(now))
    days = (GLib.Date.new_dmy(today.get_day_of_month(), GLib.DateMonth(today.get_month()),
                              today.get_year()).get_julian()
            - GLib.Date.new_dmy(then.get_day_of_month(), GLib.DateMonth(then.get_month()),
                                then.get_year()).get_julian())
    if delta < 86400 and days == 0:
        return _("{n} h").format(n=int(delta // 3600))
    if days <= 1:
        return _("Yesterday")
    if days < 7:
        return _("{n} d").format(n=days)
    if days < 35:
        return _("{n} wk").format(n=days // 7)
    if days < 365:
        n = max(1, days // 30)
        return ngettext("{n} mo", "{n} mo", n).format(n=n)
    n = days // 365
    return ngettext("{n} yr", "{n} yr", n).format(n=n)


def _live_line(job: str, *, state: str, in_call: bool, unread: bool, latest_title: str) -> str:
    """What a Dot's second line says: what is going on, or else its job."""
    if in_call:
        return _("In a call")
    if state == "working":
        return _("Working…")
    if state == "paused":
        return _("Paused")
    if unread and latest_title:
        return _("New: {title}").format(title=latest_title)
    return job


def _avatar_state(state: str, *, in_call: bool, offline: bool) -> str:
    if offline:
        return "offline"
    if in_call:
        return "speaking" if state == "speaking" else "call"
    return state


def _accessible(dot: Dot, line: str, *, chief: bool, unread: bool) -> str:
    job = dot.job
    label = _("{name}, {job}").format(name=dot.name, job=job) if job else dot.name
    extras = ([_("Chief")] if chief else []) + ([_("new conversation")] if unread else []) + \
        ([line] if line and line != job else [])
    return ", ".join([label] + extras)


class DotRow(Gtk.ListBoxRow):
    """Pet, name with when its latest conversation began, and a line that says what is
    going on (a call, work, a new conversation) or else the Dot's job.

    Marks on the pet: a dot for a conversation the owner has not seen yet and a star for
    the Chief. The end of the row keeps a phone for the Dot in the call."""

    def __init__(self, dot: Dot, pet_id: str, density: str, show_job: bool):
        super().__init__()
        self.dot = dot
        self._latest = 0.0
        self._density = density
        self._rail = False
        self.add_css_class("dot-row")
        self.box = Gtk.Box(spacing=8)
        self.avatar = PetAvatar(pet_id, size=AVATAR.get(density, 36))
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.box.append(self.avatar)

        self.texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                             hexpand=True)
        top = Gtk.Box(spacing=6)
        self.title = _label(dot.name, ("dot-name",), hexpand=True,
                            ellipsize=Pango.EllipsizeMode.END)
        self.when = _label("", ("dot-when", "dim-label", "caption", "numeric"), xalign=1.0,
                           visible=False, valign=Gtk.Align.BASELINE_CENTER)
        top.append(self.title)
        top.append(self.when)
        self.subtitle = _label("", ("dot-job", "dim-label", "caption"),
                               ellipsize=Pango.EllipsizeMode.END)
        self.texts.append(top)
        self.texts.append(self.subtitle)
        self.box.append(self.texts)

        self.status = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                valign=Gtk.Align.CENTER, hhomogeneous=False)
        self.status.add_named(Gtk.Box(), "none")
        # The Dot's voice as a little wave (the Grok Bot's "voice chat in progress").
        self.wave = LevelWave(bars=4, height=14, bar=2, gap=2, tooltip_text=_("In a call"))
        self.status.add_named(self.wave, "call")
        self.box.append(self.status)
        self.set_child(self.box)
        self.update(dot, pet_id, density=density, show_job=show_job)

    def update(self, dot: Dot, pet_id: str, *, density: str, show_job: bool,
               state: str = "idle", in_call: bool = False, pinned: bool = False,
               offline: bool = False, chief: bool = False, unread: bool = False,
               latest: float = 0.0, latest_title: str = "") -> None:
        self.dot = dot
        self._latest = latest
        self._density = density
        self.avatar.pet = pet_id
        self.avatar.size = RAIL_AVATAR if self._rail else AVATAR.get(density, 36)
        self.avatar.state = _avatar_state(state, in_call=in_call, offline=offline)
        self.avatar.badge = "unread" if unread and not in_call else ""
        self.avatar.star = chief
        self.title.set_label(dot.name)
        job = dot.job
        line = _live_line(job, state=state, in_call=in_call, unread=unread,
                          latest_title=latest_title)
        self.subtitle.set_label(line)
        self.subtitle.set_visible(bool(line) and (show_job or line != job))
        for name in ("comfortable", "compact"):
            self.remove_css_class(name)
        self.add_css_class(density)
        if unread:
            self.add_css_class("unread")
        else:
            self.remove_css_class("unread")
        self.status.set_visible_child_name("call" if in_call else "none")
        if not in_call:
            self.wave.reset()
        self.refresh_time()
        pet = pets.get(pet_id) or pets.DEFAULT
        tooltip = f"{dot.name} — {job}" if job else dot.name
        if latest_title:
            tooltip += "\n" + _("Latest conversation: {title}").format(title=latest_title)
        self.set_tooltip_text(tooltip)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_accessible(dot, line, chief=chief, unread=unread)])
        self.avatar.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                                    [_("{pet}, pet of {dot}").format(pet=_(pet.name),
                                                                     dot=dot.name)])

    def refresh_time(self) -> None:
        """The age of the latest conversation ("3 min"), redrawn every minute."""
        text = compact_age(self._latest)
        self.when.set_label(text)
        self.when.set_visible(bool(text))
        if self._latest:
            stamp = self._latest / 1000 if self._latest > 1e12 else self._latest
            self.when.set_tooltip_text(
                GLib.DateTime.new_from_unix_local(int(stamp)).format("%c"))

    def set_rail(self, rail: bool) -> None:
        """Only the pet, centred: the sidebar is a rail."""
        self._rail = rail
        self.texts.set_visible(not rail)
        self.status.set_visible(not rail)
        self.box.set_halign(Gtk.Align.CENTER if rail else Gtk.Align.FILL)
        self.avatar.size = RAIL_AVATAR if rail else AVATAR.get(self._density, 36)
        if rail:
            self.add_css_class("rail")
        else:
            self.remove_css_class("rail")


class PinnedTile(Gtk.FlowBoxChild):
    """A pinned Dot in the grid at the top: its pet, big, with its name under it."""

    def __init__(self, dot: Dot, pet_id: str, density: str):
        super().__init__()
        self.dot = dot
        self._density = density
        self.add_css_class("pinned-tile")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      halign=Gtk.Align.CENTER)
        self.avatar = PetAvatar(pet_id, size=TILE.get(density, 52))
        self.name = _label(dot.name, ("caption", "pinned-name"), xalign=0.5,
                           ellipsize=Pango.EllipsizeMode.END, max_width_chars=8)
        box.append(self.avatar)
        box.append(self.name)
        self.set_child(box)
        self.update(dot, pet_id, density=density)

    def update(self, dot: Dot, pet_id: str, *, density: str, state: str = "idle",
               in_call: bool = False, offline: bool = False, chief: bool = False,
               unread: bool = False, latest_title: str = "", rail: bool = False) -> None:
        self.dot = dot
        self._density = density
        self.avatar.pet = pet_id
        self.avatar.size = RAIL_AVATAR if rail else TILE.get(density, 52)
        self.avatar.state = _avatar_state(state, in_call=in_call, offline=offline)
        self.avatar.badge = "unread" if unread and not in_call else ""
        self.avatar.star = chief
        self.name.set_label(dot.name)
        self.name.set_visible(not rail)
        if unread:
            self.add_css_class("unread")
        else:
            self.remove_css_class("unread")
        job = dot.job
        line = _live_line(job, state=state, in_call=in_call, unread=unread,
                          latest_title=latest_title)
        self.set_tooltip_text("\n".join(p for p in (dot.name, line) if p))
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_accessible(dot, line, chief=chief, unread=unread)])


class NavRow(Gtk.ListBoxRow):
    def __init__(self, key: str, icon: str, title: str):
        super().__init__()
        self.key = key
        self._title = title
        self.box = Gtk.Box(spacing=12)
        self.box.append(Gtk.Image(icon_name=icon))
        self.label = _label(title, hexpand=True)
        self.box.append(self.label)
        self.badge = _label("", ("nav-badge", "numeric"), visible=False, xalign=0.5,
                            valign=Gtk.Align.CENTER)
        self.box.append(self.badge)
        self.set_child(self.box)
        self._rail = False

    def set_badge(self, value: int | str | None, accent: bool = False) -> None:
        text = str(value) if value else ""
        self.badge.set_label(text)
        self.badge.set_visible(bool(text) and not self._rail)
        if accent:
            self.badge.add_css_class("accent-badge")
        else:
            self.badge.remove_css_class("accent-badge")
        self._sync_tooltip()

    def set_rail(self, rail: bool) -> None:
        self._rail = rail
        self.label.set_visible(not rail)
        self.badge.set_visible(bool(self.badge.get_label()) and not rail)
        self.box.set_halign(Gtk.Align.CENTER if rail else Gtk.Align.FILL)
        self._sync_tooltip()

    def _sync_tooltip(self) -> None:
        count = self.badge.get_label()
        self.set_tooltip_text((f"{self._title} ({count})" if count else self._title)
                              if self._rail else None)


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
        self._tiles: dict[str, PinnedTile] = {}
        self._hidden_rows: dict[str, DotRow] = {}
        self._space_rows: dict[str, SpaceRow] = {}
        self._selecting = False
        self._filter = ""
        self._states: dict[str, str] = {}
        self._hidden_count = 0
        self._has_spaces = False
        self._in_call: str | None = None
        self._rail = False
        self._model_text = ""
        self._spend_text = ""
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

        # Pinned Dots: a grid of big pets, as the Grok Bot app pins its bots.
        self.pinned_header = Gtk.Box(margin_start=18, margin_end=8, margin_top=8,
                                     margin_bottom=4, visible=False)
        self.pinned_header.append(_label(_("Pinned Dots"), ("sidebar-heading",), hexpand=True))
        content.append(self.pinned_header)
        self.pinned = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
                                  min_children_per_line=3, max_children_per_line=4,
                                  activate_on_single_click=True, column_spacing=2,
                                  row_spacing=2, margin_start=6, margin_end=6, visible=False)
        self.pinned.add_css_class("pinned-grid")
        self.pinned.update_property([Gtk.AccessibleProperty.LABEL], [_("Pinned Dots")])
        # Choosing a pet opens its Dot, by click or by the arrow keys, as in the list.
        self.pinned.connect("selected-children-changed", self._on_tile_selected)
        self.pinned.set_filter_func(self._filter_tile)
        self._add_menu_controllers(self.pinned, self._on_tile_context, self._on_tile_menu_key)
        content.append(self.pinned)

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
        self._add_menu_controllers(self.dots, self._on_dot_context, self._on_dot_menu_key)
        content.append(self.dots)

        self.spaces_expander = Gtk.Expander(expanded=False, margin_top=8)
        self.spaces_expander.add_css_class("spaces-expander")
        self.spaces_expander.set_label_widget(_label(_("Spaces"), ("sidebar-heading",)))
        self.spaces = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.spaces.add_css_class("navigation-sidebar")
        self.spaces.connect("row-selected", self._on_space_selected)
        self.spaces.set_filter_func(self._filter_space)
        self.spaces_expander.set_child(self.spaces)
        content.append(self.spaces_expander)

        # Hidden Dots: folded at the very end, with how many there are.
        self.hidden_expander = Gtk.Expander(expanded=False, margin_top=4, visible=False)
        self.hidden_expander.add_css_class("spaces-expander")
        heading = Gtk.Box(spacing=6)
        heading.append(_label(_("Hidden Dots"), ("sidebar-heading",)))
        self.hidden_count = _label("", ("sidebar-count", "numeric"))
        heading.append(self.hidden_count)
        self.hidden_expander.set_label_widget(heading)
        self.hidden_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.hidden_list.add_css_class("navigation-sidebar")
        self.hidden_list.add_css_class("dots-list")
        self.hidden_list.add_css_class("hidden-list")
        self.hidden_list.update_property([Gtk.AccessibleProperty.LABEL], [_("Hidden Dots")])
        self.hidden_list.connect("row-selected", self._on_hidden_selected)
        self.hidden_list.set_filter_func(self._filter_dot)
        self._add_menu_controllers(self.hidden_list, self._on_hidden_context,
                                   self._on_hidden_menu_key)
        self.hidden_expander.set_child(self.hidden_list)
        content.append(self.hidden_expander)

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

        # "3 min" becomes "4 min": the ages are redrawn every minute while on screen.
        self._clock = 0
        self.connect("map", lambda *_: self._start_clock())
        self.connect("unmap", lambda *_: self._stop_clock())

    def _add_menu_controllers(self, widget: Gtk.Widget, on_context, on_menu_key) -> None:
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", on_context)
        widget.add_controller(click)
        press = Gtk.GestureLongPress(touch_only=True)
        press.connect("pressed", lambda g, x, y: on_context(g, 1, x, y))
        widget.add_controller(press)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Shift>F10|Menu"),
                                           Gtk.CallbackAction.new(on_menu_key)))
        widget.add_controller(keys)

    def _start_clock(self) -> None:
        if not self._clock:
            self._clock = GLib.timeout_add_seconds(60, self._on_clock)
        self._on_clock()

    def _stop_clock(self) -> None:
        if self._clock:
            GLib.source_remove(self._clock)
            self._clock = 0

    def _on_clock(self) -> bool:
        for row in list(self._iter_rows()) + list(self._iter_hidden_rows()):
            row.refresh_time()
        return GLib.SOURCE_CONTINUE

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
                 pinned: set[str], in_call: str | None, offline: bool,
                 chief: str | None = None, unread: set[str] | frozenset = frozenset(),
                 activity: dict[str, tuple[float, str]] | None = None) -> None:
        """Rebuild or update the grid and the lists; keeps the selection and the focus.

        ``visible`` Dots in ``pinned`` go to the grid, the others to the list, and
        ``hidden`` ones to the folded group. ``activity`` gives each Dot's latest
        conversation as ``(when, title)``; ``unread`` the Dots with one the owner has not
        seen; ``chief`` the Chief's id."""
        activity = activity or {}
        self._in_call = in_call
        selected = self.selected_dot()
        grid = [d for d in visible if d.id in pinned]
        listed = [d for d in visible if d.id not in pinned]
        focused = self._focused_dot()

        def pet(dot: Dot) -> str:
            return pet_map.get(dot.id, pets.DEFAULT.id)

        rebuilt = False
        if [d.id for d in grid] != [t.dot.id for t in self._iter_tiles()]:
            rebuilt = True
            self._selecting = True
            for tile in list(self._iter_tiles()):
                self.pinned.remove(tile)
            self._tiles.clear()
            for dot in grid:
                tile = PinnedTile(dot, pet(dot), density)
                self._tiles[dot.id] = tile
                self.pinned.append(tile)
            self._selecting = False
        if [d.id for d in listed] != [r.dot.id for r in self._iter_rows()]:
            rebuilt = True
            self._selecting = True
            for row in list(self._iter_rows()):
                self.dots.remove(row)
            self._rows.clear()
            for dot in listed:
                row = DotRow(dot, pet(dot), density, show_job)
                row.set_rail(self._rail)
                self._rows[dot.id] = row
                self.dots.append(row)
            self._selecting = False
        if [d.id for d in hidden] != [r.dot.id for r in self._iter_hidden_rows()]:
            rebuilt = True
            self._selecting = True
            for row in list(self._iter_hidden_rows()):
                self.hidden_list.remove(row)
            self._hidden_rows.clear()
            for dot in hidden:
                row = DotRow(dot, pet(dot), "compact", False)
                self._hidden_rows[dot.id] = row
                self.hidden_list.append(row)
            self._selecting = False

        def facts(dot: Dot) -> dict:
            latest, title = activity.get(dot.id, (0.0, ""))
            return {"state": self._states.get(dot.id, "idle"), "in_call": dot.id == in_call,
                    "offline": offline, "chief": dot.id == chief, "unread": dot.id in unread,
                    "latest_title": title, "_latest": latest}

        for dot in grid:
            info = facts(dot)
            info.pop("_latest")
            self._tiles[dot.id].update(dot, pet(dot), density=density, rail=self._rail, **info)
        for dot in listed:
            info = facts(dot)
            latest = info.pop("_latest")
            self._rows[dot.id].update(dot, pet(dot), density=density, show_job=show_job,
                                      pinned=False, latest=latest, **info)
        for dot in hidden:
            info = facts(dot)
            latest = info.pop("_latest")
            self._hidden_rows[dot.id].update(dot, pet(dot), density="compact", show_job=False,
                                             latest=latest, **info)

        self._hidden_count = len(hidden)
        self.hidden_count.set_label(str(len(hidden)) if hidden else "")
        self.dots_header._heading.set_label(_("Dots"))
        # Every Dot pinned: no empty list (with its "No Dots yet") under the grid.
        self.dots.set_visible(bool(listed) or not grid)
        self._sync_sections()
        if selected:
            self.select_dot(selected)
        if rebuilt and focused is not None:
            # Pin, Unpin or Hide from the keyboard: the focus stays with the Dot, wherever
            # it went, or else with the list it left.
            target = self._widget_for(focused)
            if target is None or not target.get_mapped() or not target.grab_focus():
                self.dots.grab_focus()

        spaces = [(s.id, s.name) for s in workspace.spaces]
        if spaces != [(r.space_id, r.title.get_label()) for r in self._iter_space_rows()]:
            for row in list(self._iter_space_rows()):
                self.spaces.remove(row)
            self._space_rows.clear()
            for space_id, name in spaces:
                row = SpaceRow(space_id, name)
                self._space_rows[space_id] = row
                self.spaces.append(row)
        self._has_spaces = bool(spaces)
        self._sync_sections()

    def _sync_sections(self) -> None:
        """What shows depends on the data and on the rail (pets only)."""
        rail = self._rail
        has_grid = bool(self._tiles)
        self.pinned_header.set_visible(has_grid and not rail)
        self.pinned.set_visible(has_grid)
        self.pinned.set_min_children_per_line(1 if rail else 3)
        self.pinned.set_max_children_per_line(1 if rail else 4)
        self.dots_header.set_visible(not rail)
        self.spaces_expander.set_visible(self._has_spaces and not rail)
        self.hidden_expander.set_visible(bool(self._hidden_count) and not rail)
        self.model_button.set_visible(bool(self._model_text) and not rail)
        self.spend_button.set_visible(bool(self._spend_text) and not rail)

    def set_dot_state(self, dot_id: str | None, state: str) -> None:
        """What the page says the open Dot is doing (idle, working, paused)."""
        for key in [k for k, v in self._states.items() if v != "idle"]:
            if key != dot_id:
                self._states[key] = "idle"
        if dot_id:
            self._states[dot_id] = state

    def set_call_level(self, level: float) -> None:
        """How loud the Dot in the call is now (0–1), for its row's little wave."""
        row = self._rows.get(self._in_call or "")
        if row is not None:
            row.wave.set_level(level)

    def set_spend(self, text: str, tooltip: str = "") -> None:
        """The OpenRouter spend line, or "" to hide it (no key, or an older server)."""
        self._spend_text = text
        self.spend_label.set_label(text)
        self.spend_button.set_tooltip_text(tooltip or None)
        # One gap under the pair, not one under each line.
        self.model_button.set_margin_bottom(0 if text else 6)
        self.spend_button.update_property([Gtk.AccessibleProperty.LABEL],
                                          [_("OpenRouter spend: {spend}").format(spend=text)])
        self._sync_sections()

    def set_model(self, text: str) -> None:
        """The chat model the Dots use, or "" to hide the line (servers that do not say)."""
        self._model_text = text
        self.model_label.set_label(text)
        tooltip = _("Chat model: {model}. Click to change it in AI Models.").format(
            model=text) if text else None
        self.model_button.set_tooltip_text(tooltip)
        self.model_button.update_property([Gtk.AccessibleProperty.LABEL],
                                          [_("Chat model: {model}").format(model=text)])
        self._sync_sections()

    def set_counts(self, activity: int, memories: int) -> None:
        self.nav_rows["activity"].set_badge(activity, accent=True)
        self.nav_rows["memories"].set_badge(memories)

    # -- rail ------------------------------------------------------------------------------

    @property
    def rail(self) -> bool:
        return self._rail

    def set_rail(self, rail: bool) -> None:
        """Pets only, in a narrow column (the Grok Bot's compact sidebar): names, jobs,
        headings and the model line give way; tooltips keep the names."""
        if rail == self._rail:
            return
        self._rail = rail
        if rail:
            self.add_css_class("rail")
            self.hide_search()
        else:
            self.remove_css_class("rail")
        for row in self._iter_rows():
            row.set_rail(rail)
        for tile in self._iter_tiles():
            tile.avatar.size = RAIL_AVATAR if rail else TILE.get(tile._density, 52)
            tile.name.set_visible(not rail)
        for row in self.nav_rows.values():
            row.set_rail(rail)
        self._sync_sections()

    # -- selection -------------------------------------------------------------------------

    def _iter_rows(self):
        yield from _children_of(self.dots, DotRow)

    def _iter_tiles(self):
        yield from _children_of(self.pinned, PinnedTile)

    def _iter_hidden_rows(self):
        yield from _children_of(self.hidden_list, DotRow)

    def _iter_space_rows(self):
        yield from _children_of(self.spaces, SpaceRow)

    def _widget_for(self, dot_id: str | None):
        return self._tiles.get(dot_id or "") or self._rows.get(dot_id or "") or \
            self._hidden_rows.get(dot_id or "")

    def _focused_dot(self) -> str | None:
        for widget in list(self._iter_tiles()) + list(self._iter_rows()) + \
                list(self._iter_hidden_rows()):
            if widget.has_focus():
                return widget.dot.id
        return None

    def visible_dot_ids(self) -> list[str]:
        """The pinned Dots, then the list: the order of Ctrl+1…9 and Alt+↑/↓."""
        return [tile.dot.id for tile in self._iter_tiles() if tile.get_child_visible()] + \
            [row.dot.id for row in self._iter_rows() if row.get_child_visible()]

    def selected_dot(self) -> str | None:
        tiles = self.pinned.get_selected_children()
        if tiles and isinstance(tiles[0], PinnedTile):
            return tiles[0].dot.id
        for listbox in (self.dots, self.hidden_list):
            row = listbox.get_selected_row()
            if isinstance(row, DotRow):
                return row.dot.id
        return None

    def select_dot(self, dot_id: str | None) -> None:
        self._selecting = True
        tile = self._tiles.get(dot_id or "")
        row = self._rows.get(dot_id or "")
        hidden = self._hidden_rows.get(dot_id or "")
        if tile is not None:
            self.pinned.select_child(tile)
        else:
            self.pinned.unselect_all()
        if row is not None:
            self.dots.select_row(row)
        else:
            self.dots.unselect_all()
        if hidden is not None:
            self.hidden_list.select_row(hidden)
        else:
            self.hidden_list.unselect_all()
        if tile or row or hidden:
            self.spaces.unselect_all()
            self.nav.unselect_all()
        self._selecting = False

    def _unselect_dots(self) -> None:
        self.pinned.unselect_all()
        self.dots.unselect_all()
        self.hidden_list.unselect_all()

    def select_space(self, space_id: str | None) -> None:
        self._selecting = True
        row = self._space_rows.get(space_id or "")
        self._unselect_dots()
        self.nav.unselect_all()
        if row is not None:
            self.spaces_expander.set_expanded(True)
            self.spaces.select_row(row)
        self._selecting = False

    def select_nav(self, key: str | None) -> None:
        self._selecting = True
        row = self.nav_rows.get(key or "")
        self._unselect_dots()
        self.spaces.unselect_all()
        if row is not None and key != "settings":
            self.nav.select_row(row)
        else:
            self.nav.unselect_all()
        self._selecting = False

    def _opened(self, dot_id: str, keep) -> None:
        """A Dot chosen by the owner in ``keep`` (the grid or a list): one selection only."""
        self._selecting = True
        if keep is not self.pinned:
            self.pinned.unselect_all()
        if keep is not self.dots:
            self.dots.unselect_all()
        if keep is not self.hidden_list:
            self.hidden_list.unselect_all()
        self.spaces.unselect_all()
        self.nav.unselect_all()
        self._selecting = False
        self.emit("dot-activated", dot_id)

    def _on_dot_selected(self, _list, row) -> None:
        if self._selecting or not isinstance(row, DotRow):
            return
        self._opened(row.dot.id, self.dots)

    def _on_hidden_selected(self, _list, row) -> None:
        if self._selecting or not isinstance(row, DotRow):
            return
        self._opened(row.dot.id, self.hidden_list)

    def _on_tile_selected(self, _box) -> None:
        if self._selecting:
            return
        tiles = self.pinned.get_selected_children()
        if tiles and isinstance(tiles[0], PinnedTile):
            self._opened(tiles[0].dot.id, self.pinned)

    def _on_space_selected(self, _list, row) -> None:
        if self._selecting or not isinstance(row, SpaceRow):
            return
        self._selecting = True
        self._unselect_dots()
        self.nav.unselect_all()
        self._selecting = False
        self.emit("space-activated", row.space_id)

    # -- the Dot menu ----------------------------------------------------------------------

    def _on_dot_context(self, gesture, _n, x, y) -> None:
        row = self.dots.get_row_at_y(int(y))
        if isinstance(row, DotRow) and self._open_dot_menu(row, _rect(x, y), self.dots):
            _claim(gesture)

    def _on_hidden_context(self, gesture, _n, x, y) -> None:
        row = self.hidden_list.get_row_at_y(int(y))
        if isinstance(row, DotRow) and self._open_dot_menu(row, _rect(x, y), self.hidden_list):
            _claim(gesture)

    def _on_tile_context(self, gesture, _n, x, y) -> None:
        tile = self.pinned.get_child_at_pos(int(x), int(y))
        if isinstance(tile, PinnedTile) and self._open_dot_menu(tile, _rect(x, y), self.pinned):
            _claim(gesture)

    def _menu_under(self, widget, source) -> bool:
        """Shift+F10 or the Menu key: the menu of the focused Dot, under it."""
        ok, bounds = widget.compute_bounds(source)
        if not ok:
            return False
        area = _rect(bounds.get_x(), bounds.get_y())
        area.width, area.height = int(bounds.get_width()), int(bounds.get_height())
        return self._open_dot_menu(widget, area, source)

    def _on_dot_menu_key(self, *_args) -> bool:
        row = self.dots.get_focus_child()
        if not isinstance(row, DotRow):
            row = self.dots.get_selected_row()
        return isinstance(row, DotRow) and self._menu_under(row, self.dots)

    def _on_hidden_menu_key(self, *_args) -> bool:
        row = self.hidden_list.get_focus_child()
        if not isinstance(row, DotRow):
            row = self.hidden_list.get_selected_row()
        return isinstance(row, DotRow) and self._menu_under(row, self.hidden_list)

    def _on_tile_menu_key(self, *_args) -> bool:
        tile = self.pinned.get_focus_child()
        if not isinstance(tile, PinnedTile):
            selected = self.pinned.get_selected_children()
            tile = selected[0] if selected else None
        return isinstance(tile, PinnedTile) and self._menu_under(tile, self.pinned)

    def _open_dot_menu(self, item, area, source=None) -> bool:
        """Show the menu of ``item``'s Dot pointing at ``area`` (in ``source``'s
        coordinates: the Dots list, the grid or the hidden list)."""
        source = source or self.dots
        if self.menu_for_dot is None:
            return False
        menu = self.menu_for_dot(item.dot.id)
        if menu is None:
            return False
        # Parented to the sidebar, not to the list or the row: Pin and Hide rebuild the rows
        # while the menu is open, and Gtk.ListBox never re-sizes a popover child, so the menu
        # kept the height it had before its section separator appeared and had to scroll.
        # The sidebar (an Adw.Bin) re-presents its popovers on every allocation.
        _ok, origin = source.compute_point(self, Graphene.Point().init(area.x, area.y))
        area.x, area.y = int(origin.x), int(origin.y)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        # Opens to the right of the pointer (left in right-to-left languages) and below it,
        # like GTK's own context menus, so the Dot's name and job stay visible.
        popover.set_halign(Gtk.Align.START)
        popover.set_pointing_to(area)
        item.add_css_class("menu-open")

        def closed(popover):
            item.remove_css_class("menu-open")
            # GTK hands the focus back to the menu's parent, and the sidebar cannot take it,
            # so it fell out of the list (onto the header's New Chat button). It goes back to
            # the Dot, before the chosen item runs: Open or New Chat can still move it on.
            root = self.get_root()
            focus = root.get_focus() if root is not None else None
            if focus is None or focus.is_ancestor(popover):
                (item if item.get_parent() is source else source).grab_focus()
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
        self.pinned.invalidate_filter()
        self.dots.invalidate_filter()
        self.hidden_list.invalidate_filter()
        self.spaces.invalidate_filter()
        found_elsewhere = any(self._filter_tile(t) for t in self._iter_tiles())
        self.placeholder.set_label(("" if found_elsewhere else _("No Dots match your search"))
                                   if self._filter else getattr(self, "_empty_text", ""))
        if self._filter:
            self.spaces_expander.set_expanded(True)
            if any(self._filter_dot(r) for r in self._iter_hidden_rows()):
                self.hidden_expander.set_expanded(True)

    def _on_search_activate(self, _entry) -> None:
        for widget in list(self._iter_tiles()) + list(self._iter_rows()):
            if self._matches(widget.dot):
                self.hide_search()
                self.select_dot(widget.dot.id)
                self.emit("dot-activated", widget.dot.id)
                return
        for row in self._iter_space_rows():
            if self._filter_space(row):
                self.hide_search()
                self.emit("space-activated", row.space_id)
                return
        for row in self._iter_hidden_rows():
            if self._matches(row.dot):
                self.hide_search()
                self.select_dot(row.dot.id)
                self.emit("dot-activated", row.dot.id)
                return

    def _matches(self, dot: Dot) -> bool:
        return not self._filter or self._filter in f"{dot.name} {dot.job}".casefold()

    def _filter_dot(self, row) -> bool:
        return not isinstance(row, DotRow) or self._matches(row.dot)

    def _filter_tile(self, tile) -> bool:
        return not isinstance(tile, PinnedTile) or self._matches(tile.dot)

    def _filter_space(self, row) -> bool:
        if not self._filter or not isinstance(row, SpaceRow):
            return True
        return self._filter in row.title.get_label().casefold()


def _children_of(container: Gtk.Widget, kind: type):
    child = container.get_first_child()
    while child is not None:
        if isinstance(child, kind):
            yield child
        child = child.get_next_sibling()


def _claim(gesture) -> None:
    if hasattr(gesture, "set_state"):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)


def _rect(x: float, y: float):
    from gi.repository import Gdk
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect


def dot_menu(dot: Dot, *, pinned: bool, chief: bool, can_call: bool,
             hidden: bool = False) -> Gio.Menu:
    """Right-click menu of a Dot (actions live on the window, with the Dot id as target).

    One section per kind of action, as in the Grok Bot app: talk to it, place it, change
    it, and hiding it (or bringing a hidden one back) last and on its own."""
    target = GLib.Variant("s", dot.id)
    menu = Gio.Menu()
    sections = (
        ((_("Open"), "win.dot-open", True),
         (_("New Chat"), "win.dot-new-chat", True),
         (_("Call"), "win.dot-call", can_call)),
        ((_("Unpin") if pinned else _("Pin to Top"), "win.dot-pin", not hidden),
         (_("Make Chief"), "win.dot-chief", not chief)),
        ((_("Change Pet…"), "win.dot-pet", True),
         (_("Edit Dot…"), "win.dot-edit", True)),
        ((_("Show in Sidebar"), "win.dot-unhide", True) if hidden else
         (_("Hide From Sidebar"), "win.dot-hide", True),),
    )
    for entries in sections:
        section = Gio.Menu()
        for label, action, enabled in entries:
            if not enabled:
                continue
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, target)
            section.append_item(item)
        menu.append_section(None, section)
    return menu
