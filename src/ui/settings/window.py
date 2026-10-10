"""The Settings window: a sidebar of sections with search, and one page per topic.

One instance at a time (:func:`open_settings`). Pages live in an ``Adw.ViewStack``
shown by an ``Adw.ViewSwitcherSidebar``; subpages (a server, a Dot) are pushed on a
navigation view inside the content pane. Most changes apply at once; a Dot's identity
and instructions, the AI model and a server's address have explicit buttons.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Sequence

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk, Pango

from ... import config
from ...core import api
from ...i18n import _
from . import PAGES, search
from .common import SettingsPage, escape, string_model, toggle_group

log = logging.getLogger(__name__)

# Pages that show one server; they follow the server selector.
SERVER_PAGES = ("dots", "voice", "model", "privacy")
HIGHLIGHT_MS = 1800
# A server's model and voice settings are asked again when shown after this many seconds.
MODEL_MAX_AGE = 60
# Tall enough for the whole list of sections (at 680 px "Advanced" was cut and "About" hidden)
# and for the Privacy page, and shorter than the main window's 820 px.
DEFAULT_SIZE = (980, 760)

_instance: SettingsWindow | None = None
_css: Gtk.CssProvider | None = None


def _load_css() -> None:
    global _css
    display = Gdk.Display.get_default()
    if _css is not None or display is None:
        return
    _css = Gtk.CssProvider()
    _css.load_from_path(str(Path(__file__).with_name("settings.css")))
    Gtk.StyleContext.add_provider_for_display(display, _css,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _fit_screen(parent, height: int) -> int:
    """``height``, or less on a short screen (the one the main window is on)."""
    surface = parent.get_surface() if isinstance(parent, Gtk.Window) else None
    display = Gdk.Display.get_default()
    monitor = display.get_monitor_at_surface(surface) \
        if surface is not None and display is not None else None
    if monitor is None:
        return height
    # Room for the top bar and the window's own shadow.
    return max(420, min(height, monitor.get_geometry().height - 96))


def current() -> SettingsWindow | None:
    """The open Settings window, if any."""
    return _instance


def present(app, page: str | None = "general", *, add_server: bool = False,
            edit: str | None = None, dot: str | None = None) -> SettingsWindow:
    global _instance
    if _instance is None:
        _instance = SettingsWindow(app, page if page in PAGES else None)
    win = _instance
    if add_server:
        win.show_page("servers")
        win.pages["servers"].add_server()
    elif edit:
        win.show_page("servers")
        win.pages["servers"].edit_server(edit)
    elif dot:
        active = app.profiles.active
        if active is not None:
            win.select_profile(active.id)     # Dot ids come from the open server
        win.show_page("dots")
        win.pages["dots"].open_dot(dot)
    elif page in PAGES:
        win.show_page(page)
    win.present()
    return win


class SettingsWindow(Adw.Window):
    __gtype_name__ = "DotdeckSettingsWindow"

    def __init__(self, app, page: str | None = None):
        parent = getattr(app, "window", None)
        width, height = DEFAULT_SIZE
        super().__init__(title=_("Settings"), default_width=width,
                         default_height=_fit_screen(parent, height), icon_name=config.APP_ID)
        _load_css()
        self.app = app
        self.set_size_request(360, 420)
        if isinstance(app, Gtk.Application):
            self.set_application(app)
        if isinstance(parent, Gtk.Window):
            self.set_transient_for(parent)
        if config.DEVEL:
            self.add_css_class("devel")

        active = app.profiles.active or next(iter(app.profiles.list()), None)
        self.profile_id: str | None = active.id if active else None
        self.rows_by_key: dict[str, Gtk.Widget] = {}
        self.editors: dict[tuple[str, str], object] = {}
        self._entries: list[tuple[str, Adw.PreferencesGroup | None, Gtk.Widget, tuple]] = []
        self._bound: dict[str, list[Callable[[object], None]]] = {}
        self._selectors: list[Adw.ComboRow] = []
        self._selector_ids: list[str] = []
        self._model: dict[str, dict] = {}
        self._handlers: list[tuple[object, int]] = []
        self._monitor_handlers: list[tuple[object, int]] = []
        self._refresh_source = 0
        self._closing = False
        self._current: str | None = None
        self._syncing_selectors = False
        self._picked = False        # a server was chosen here; stop following the open one

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280,
                                             sidebar_width_fraction=0.26)
        self.toasts.set_child(self.split)

        # Each page as wide as it needs: a homogeneous stack made every page as wide as the
        # widest (Appearance, 398 px), so at 360 px all of them ran off the right edge.
        self.stack = Adw.ViewStack(vhomogeneous=False, hhomogeneous=False)
        self.pages: dict[str, SettingsPage] = {}
        self._build_pages()
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        breakpoint.add_setter(self.switcher, "mode", Adw.SidebarMode.PAGE)
        self.add_breakpoint(breakpoint)

        self._install_shortcuts()
        self._handlers.append((app.settings, app.settings.connect("changed",
                                                                  self._on_setting_changed)))
        self._handlers.append((app.profiles, app.profiles.connect("changed",
                                                                  self._on_profiles_changed)))
        self._handlers.append((app.profiles, app.profiles.connect("active-changed",
                                                                  self._on_active_changed)))
        self._watch_monitors()
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", lambda *_: self._shutdown())

        start = page or app.settings.get("settings-page")
        self.show_page(start if start in PAGES else "general")
        self._on_page_changed()
        self.request_model_config()

    # -- layout ----------------------------------------------------------------------------

    def _build_pages(self) -> None:
        from .app_pages import AppearancePage, GeneralPage, NotificationsPage, ShortcutsPage
        from .dots import DotsPage
        from .model import ModelPage
        from .privacy import PrivacyPage
        from .servers import ServersPage
        from .system import AboutPage, AdvancedPage
        from .voice_page import VoicePage
        sections = [
            (_("App"), (GeneralPage, AppearancePage, NotificationsPage, ShortcutsPage)),
            (_("Your Dots"), (DotsPage, VoicePage, ModelPage)),
            (_("Server"), (ServersPage, PrivacyPage)),
            (_("System"), (AdvancedPage, AboutPage)),
        ]
        for section, kinds in sections:
            for position, kind in enumerate(kinds):
                page = kind(self)
                self.pages[page.name] = page
                stack_page = self.stack.add_titled_with_icon(page.widget, page.name, page.title,
                                                             page.icon)
                if position == 0:
                    stack_page.set_starts_section(True)
                    stack_page.set_section_title(section)
                self._entries.append((page.name, None, page.widget, ()))

    def _build_sidebar(self) -> Adw.NavigationPage:
        self.switcher = Adw.ViewSwitcherSidebar(stack=self.stack)
        self.switcher.connect("activated", lambda *_: self.split.set_show_content(True))

        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search settings"),
                                            hexpand=True)
        self.search_entry.update_property([Gtk.AccessibleProperty.LABEL], [_("Search settings")])
        self.search_entry.connect("search-changed", lambda _e: self._on_search())
        self.search_entry.connect("activate", lambda _e: self._activate_first_result())
        self.search_entry.connect("stop-search", self._on_stop_search)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(keys)
        search_box = Gtk.Box(margin_start=12, margin_end=12, margin_bottom=6)
        search_box.append(self.search_entry)

        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.results.add_css_class("navigation-sidebar")
        self.results.add_css_class("settings-results")
        self.results.connect("row-activated", lambda _l, row: self.reveal(row.entry))
        self.results.update_property([Gtk.AccessibleProperty.LABEL], [_("Search results")])
        empty = Adw.StatusPage(icon_name="edit-find-symbolic", title=_("No Results"),
                               description=_("Try a different word."))
        empty.add_css_class("compact")

        self.sidebar_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                       vexpand=True)
        self.switcher.set_vexpand(True)
        self.sidebar_stack.add_named(self.switcher, "pages")      # it scrolls by itself
        self.sidebar_stack.add_named(Gtk.ScrolledWindow(
            child=self.results, hscrollbar_policy=Gtk.PolicyType.NEVER), "results")
        self.sidebar_stack.add_named(empty, "empty")

        view = Adw.ToolbarView(content=self.sidebar_stack)
        view.add_top_bar(Adw.HeaderBar())
        view.add_top_bar(search_box)
        return Adw.NavigationPage(title=_("Settings"), child=view, tag="sidebar")

    def _build_content(self) -> Adw.NavigationPage:
        self.title_widget = Adw.WindowTitle()
        header = Adw.HeaderBar(title_widget=self.title_widget)
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.root_page = Adw.NavigationPage(title=_("Settings"), child=view, tag="root")
        self.nav = Adw.NavigationView()
        self.nav.add(self.root_page)
        self.content_page = Adw.NavigationPage(title=_("Settings"), child=self.nav,
                                               tag="content")
        return self.content_page

    def _install_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()

        def add(trigger: str, callback: Callable[[], bool]) -> None:
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(lambda *_: callback())))

        add("<Control>f", self.focus_search)
        add("Escape", self._on_escape)
        add("<Control>s", self._on_save_shortcut)
        self.add_controller(controller)

    # -- navigation ------------------------------------------------------------------------

    def show_page(self, name: str) -> None:
        if name not in self.pages:
            name = "general"
        self.pop_to_root()
        self.stack.set_visible_child_name(name)
        if self.split.get_collapsed():
            self.split.set_show_content(True)     # narrow: the page asked for, not the list

    @property
    def visible_page(self) -> str:
        return self.stack.get_visible_child_name() or "general"

    def set_visible_page_name(self, name: str) -> None:
        """Compatibility with the old preferences dialog (tools/screenshots.py)."""
        self.show_page(name)

    def _on_page_changed(self, *_args) -> None:
        name = self.visible_page
        page = self.pages.get(name)
        if page is None or name == self._current:
            return
        previous = self.pages.get(self._current)
        self._current = name
        if previous is not None:
            previous.hidden()
        self.title_widget.set_title(page.title)
        self.root_page.set_title(page.title)
        self.content_page.set_title(page.title)
        self.app.settings.set("settings-page", name)
        if self.nav.get_visible_page() is not self.root_page:
            self.pop_to_root()
        if name in ("voice", "model"):
            self.request_model_config(max_age=MODEL_MAX_AGE)
        page.shown()

    def push(self, page: Adw.NavigationPage) -> None:
        self.nav.push(page)
        if self.split.get_collapsed():
            self.split.set_show_content(True)

    def pop_to_root(self) -> None:
        if self.nav.get_visible_page() is not self.root_page:
            self.nav.pop_to_page(self.root_page)

    def pop(self) -> None:
        self.nav.pop()

    def focus_search(self) -> bool:
        if self.split.get_collapsed():
            self.split.set_show_content(False)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)
        return True

    def _on_escape(self) -> bool:
        if self.nav.get_visible_page() is not self.root_page:
            self.nav.pop()
        elif self.split.get_collapsed() and self.split.get_show_content():
            self.split.set_show_content(False)
        elif self.search_entry.get_text():
            self.search_entry.set_text("")
        else:
            self.close()
        return True

    def _on_save_shortcut(self) -> bool:
        page = self.nav.get_visible_page()
        editor = getattr(page, "editor", None)
        if editor is not None and editor.dirty:
            editor.save()
            return True
        return False

    def _on_search_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        """Down from the search field goes to the results."""
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down) and \
                self.sidebar_stack.get_visible_child_name() == "results":
            first = self.results.get_row_at_index(0)
            if first is not None:
                first.grab_focus()
                return True
        return False

    def _on_stop_search(self, entry: Gtk.SearchEntry) -> None:
        if entry.get_text():
            entry.set_text("")
        else:
            self._on_escape()     # a subpage closes before the window does

    # -- search ----------------------------------------------------------------------------

    def index(self, page: str, group: Adw.PreferencesGroup | None, row: Gtk.Widget,
              keywords: Sequence[str] = ()) -> None:
        self._entries.append((page, group, row, tuple(keywords)))
        if len(self._entries) % 256 == 0:
            self._entries = [item for item in self._entries if item[2].get_parent() is not None]

    def _search_entries(self) -> list[search.Entry]:
        live = []
        entries = []
        for item in self._entries:
            page_name, group, row, keywords = item
            if row.get_parent() is None and row.get_root() is None:
                continue          # removed when its list was rebuilt
            live.append(item)
            page = self.pages.get(page_name)
            if page is None or not row.is_visible() or not self.stack.get_page(
                    page.widget).get_visible():
                continue
            if row is page.widget:
                entries.append(search.Entry(page=page_name, page_title=page.title,
                                            title=page.title, keywords=keywords,
                                            target=None, is_page=True))
                continue
            title = row.get_title() if hasattr(row, "get_title") else ""
            subtitle = row.get_subtitle() if hasattr(row, "get_subtitle") else ""
            if not title:
                continue
            entries.append(search.Entry(
                page=page_name, page_title=page.title, title=_plain(title),
                subtitle=_plain(subtitle or ""), group=_plain(group.get_title()) if group else "",
                keywords=keywords, target=row))
        self._entries = live
        return entries

    def _on_search(self) -> None:
        text = self.search_entry.get_text().strip()
        child = self.results.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.results.remove(child)
            child = following
        if not text:
            self.sidebar_stack.set_visible_child_name("pages")
            return
        found = search.search(self._search_entries(), text)
        for entry in found:
            self.results.append(_ResultRow(entry))
        self.sidebar_stack.set_visible_child_name("results" if found else "empty")

    def search_results(self) -> list[search.Entry]:
        """What the results list shows now (for tests)."""
        rows = []
        child = self.results.get_first_child()
        while child is not None:
            rows.append(child.entry)
            child = child.get_next_sibling()
        return rows

    def _activate_first_result(self) -> None:
        first = self.results.get_row_at_index(0)
        if first is not None:
            self.reveal(first.entry)

    def reveal(self, entry: search.Entry) -> None:
        """Show the page of a search result and point at its row."""
        self.show_page(entry.page)
        if self.split.get_collapsed():
            self.split.set_show_content(True)
        if entry.target is not None:
            GLib.timeout_add(80, lambda: (self.highlight(entry.target), False)[1])

    def highlight(self, row: Gtk.Widget) -> None:
        if row.get_root() is not self:
            return
        scrolled = row.get_ancestor(Gtk.ScrolledWindow)
        if scrolled is not None:
            viewport = scrolled.get_child()
            content = viewport.get_child() if isinstance(viewport, Gtk.Viewport) else viewport
            found, point = row.compute_point(content, Graphene.Point().init(0, 0))
            if found:
                adjustment = scrolled.get_vadjustment()
                target = point.y - (adjustment.get_page_size() - row.get_height()) / 2
                adjustment.set_value(max(adjustment.get_lower(),
                                         min(target, adjustment.get_upper()
                                             - adjustment.get_page_size())))
        if row.get_focusable() or isinstance(row, Gtk.ListBoxRow):
            row.grab_focus()
        row.add_css_class("settings-highlight")

        def fade() -> bool:
            row.remove_css_class("settings-highlight")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(HIGHLIGHT_MS, fade)

    # -- settings --------------------------------------------------------------------------

    def set_setting(self, key: str, value) -> None:
        change = getattr(self.app, "change_setting", None)
        if callable(change):
            change(key, value)
        else:
            self.app.settings.set(key, value)
        self._apply(key, value)

    def _apply(self, key: str, value) -> None:
        """Effects that happen here because the main window has no hook for them."""
        if key == "zoom":
            view = self.server_view()
            if view is not None and hasattr(view, "set_zoom"):
                view.set_zoom(float(value))
        elif key == "pet-animations":
            from .. import pet
            pet.set_animations(str(value))
        elif key == "verbose-logging":
            logging.getLogger().setLevel(logging.DEBUG if value or config.DEVEL
                                         else logging.INFO)

    def bind(self, key: str, update: Callable[[object], None]) -> None:
        self._bound.setdefault(key, []).append(update)

    def _on_setting_changed(self, settings, key: str) -> None:
        for update in self._bound.get(key, []):
            update(settings.get(key))

    def setting_switch(self, key: str, title: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle,
                            active=bool(self.app.settings.get(key)))
        row.connect("notify::active", lambda r, _p: self.set_setting(key, r.get_active()))
        self.bind(key, lambda value: row.get_active() != bool(value)
                  and row.set_active(bool(value)))
        self.rows_by_key[key] = row
        return row

    def setting_toggle_group(self, key: str,
                             choices: Sequence[tuple[str, str]]) -> Adw.ToggleGroup:
        group = toggle_group(choices, self.app.settings.get(key),
                             lambda name: self.set_setting(key, name))
        self.bind(key, lambda value: group.get_active_name() != value
                  and group.set_active_name(value))
        self.rows_by_key[key] = group
        return group

    def setting_combo(self, key: str, title: str, choices: Sequence[tuple[object, str]],
                      subtitle: str = "") -> Adw.ComboRow:
        values = [value for value, _label in choices]
        row = Adw.ComboRow(title=title, subtitle=subtitle,
                           model=string_model([label for _value, label in choices]))

        def position(value) -> int:
            for index, candidate in enumerate(values):
                if candidate == value or (isinstance(value, float) and isinstance(
                        candidate, float) and abs(candidate - value) < 0.005):
                    return index
            return -1

        start = position(self.app.settings.get(key))
        if start >= 0:
            row.set_selected(start)

        def changed(combo, _pspec):
            index = combo.get_selected()
            if 0 <= index < len(values):
                self.set_setting(key, values[index])

        row.connect("notify::selected", changed)

        def update(value) -> None:
            index = position(value)
            if index >= 0 and index != row.get_selected():
                row.set_selected(index)

        self.bind(key, update)
        self.rows_by_key[key] = row
        return row

    # -- servers ---------------------------------------------------------------------------

    @property
    def profile(self):
        profiles = self.app.profiles
        found = profiles.get(self.profile_id) or profiles.active
        if found is None:
            listed = profiles.list()
            found = listed[0] if listed else None
        return found

    @property
    def monitor(self):
        profile = self.profile
        return self.app.monitors.get(profile.id) if profile else None

    def server_view(self):
        window = getattr(self.app, "window", None)
        return getattr(window, "view", None) if window is not None else None

    def view_for(self, profile_id: str | None):
        """The main window's page when it shows ``profile_id``."""
        view = self.server_view()
        profile = getattr(view, "profile", None)
        return view if view is not None and profile is not None and \
            profile.id == profile_id else None

    def with_api(self, callback: Callable[[api.OpenDotsAPI], None], profile=None) -> None:
        profile = profile or self.profile
        if profile is not None:
            api.for_profile(self.app, profile, callback)

    def server_selector(self) -> Adw.ComboRow:
        """A "Server" row for the pages about one server (shown with two or more)."""
        row = Adw.ComboRow(title=_("Server"), subtitle=_("The settings below are for this "
                                                         "server"))
        row.connect("notify::selected", self._on_selector)
        self._selectors.append(row)
        self._sync_selector(row)
        return row

    def _sync_selector(self, row: Adw.ComboRow, names: bool = True) -> None:
        """Show the servers (``names``) and the chosen one in a "Server" row."""
        profiles = self.app.profiles.list()
        self._syncing_selectors = True
        try:
            if names:
                self._selector_ids = [p.id for p in profiles]
                row.set_model(string_model([p.name for p in profiles]))
            if self.profile is not None and self.profile.id in self._selector_ids:
                index = self._selector_ids.index(self.profile.id)
                if row.get_selected() != index:
                    row.set_selected(index)
            row.set_visible(len(profiles) > 1)
        finally:
            self._syncing_selectors = False

    def _on_selector(self, row: Adw.ComboRow, _pspec) -> None:
        if self._syncing_selectors:
            return
        index = row.get_selected()
        if 0 <= index < len(self._selector_ids):
            self._picked = True
            profile_id = self._selector_ids[index]
            # Not from inside the row's own notification: the other rows change too.
            GLib.idle_add(lambda: (self.select_profile(profile_id), False)[1])

    def select_profile(self, profile_id: str) -> None:
        if self._closing or profile_id == self.profile_id or \
                not self.app.profiles.get(profile_id):
            return
        self.profile_id = profile_id
        for row in self._selectors:
            self._sync_selector(row, names=False)
        self._watch_monitors()
        self.request_model_config()
        for name in SERVER_PAGES:
            self.pages[name].refresh()

    def _on_profiles_changed(self, _store) -> None:
        if self.profile_id and not self.app.profiles.get(self.profile_id):
            active = self.app.profiles.active
            self.profile_id = active.id if active else None
        elif self.profile_id is None and self.app.profiles.active:
            self.profile_id = self.app.profiles.active.id
        for row in self._selectors:
            self._sync_selector(row)
        # The app makes monitors for new servers right after this signal.
        GLib.idle_add(lambda: (self._watch_monitors(), self.schedule_refresh(), False)[2])

    def _on_active_changed(self, _store) -> None:
        active = self.app.profiles.active
        if active is not None and (not self._picked or len(self.app.profiles.list()) == 1):
            self.select_profile(active.id)
        self.schedule_refresh()

    def _watch_monitors(self) -> None:
        for mon, handler in self._monitor_handlers:
            mon.disconnect(handler)
        self._monitor_handlers = []
        if self._closing:
            return
        for profile_id, mon in list(self.app.monitors.items()):
            self._monitor_handlers.append(
                (mon, mon.connect("changed", lambda _m: self.schedule_refresh())))
            try:
                handler = mon.connect("workspace-changed", lambda _m: self.schedule_refresh())
            except TypeError:       # an older monitor without the signal
                continue
            self._monitor_handlers.append((mon, handler))

    def schedule_refresh(self) -> None:
        if not self._refresh_source and not self._closing:
            self._refresh_source = GLib.idle_add(self._refresh)

    def _refresh(self) -> bool:
        self._refresh_source = 0
        if self._closing:
            return GLib.SOURCE_REMOVE
        for page in self.pages.values():
            try:
                page.refresh()
            except Exception:      # one broken page must not take the window down
                log.exception("Could not refresh the %s settings", page.name)
        for editor in list(self.editors.values()):
            try:
                editor.refresh()
            except Exception:
                log.exception("Could not refresh the editor of %s", editor.title)
        return GLib.SOURCE_REMOVE

    # -- AI model and voice settings of the server (fork servers only) ---------------------

    def model_config(self, profile_id: str | None = None) -> dict:
        """{"state": unknown|loading|ok|unsupported|error, "data": dict|None, "error": str}."""
        return self._model.get(profile_id or self.profile_id or "", {"state": "unknown",
                                                                      "data": None,
                                                                      "error": ""})

    def request_model_config(self, force: bool = False, max_age: float | None = None) -> None:
        """Ask the selected server again unless its answer is still good.

        ``max_age`` (seconds) also refreshes an answer that is older than that, so a model
        changed in the browser shows up when the page is opened again.
        """
        profile = self.profile
        if profile is None:
            return
        current = self._model.get(profile.id)
        if current and not force:
            state = current["state"]
            stale = max_age is not None and state == "ok" and \
                time.monotonic() - current.get("at", 0.0) > max_age
            if state in ("loading", "ok", "unsupported") and not stale:
                return
        self._model[profile.id] = {"state": "loading", "data": current and current.get("data"),
                                   "error": "", "at": current.get("at", 0.0) if current else 0.0}
        self._model_changed()
        profile_id = profile.id

        def got(data, error) -> None:
            if self._closing:
                return
            if error == "unsupported":
                state = "unsupported"
            elif error is not None or not isinstance(data, dict):
                state = "error"
            else:
                state = "ok"
            self._model[profile_id] = {"state": state,
                                       "data": data if isinstance(data, dict) else None,
                                       "error": error or "", "at": time.monotonic()}
            self._model_changed()
            mon = self.app.monitors.get(profile_id)
            if state == "ok" and hasattr(mon, "set_model_config"):
                mon.set_model_config(data)       # the sidebar names the same model

        self.with_api(lambda client: client.model_config(got), profile)

    def change_model_config(self, done: Callable[[bool, str | None], None], **changes) -> None:
        """POST /api/model-config for the selected server, then keep the answer."""
        profile = self.profile
        if profile is None:
            done(False, _("No server"))
            return
        profile_id = profile.id

        def got(data, error) -> None:
            if self._closing:
                return
            if error is None:
                cached = self._model.get(profile_id) or {"state": "ok", "data": {}, "error": ""}
                merged = dict(cached.get("data") or {})
                if "model" in changes:
                    # The old name and price belong to the old model (asked again below).
                    merged.pop("modelName", None)
                    merged.pop("price", None)
                if isinstance(data, dict):
                    merged.update({k: v for k, v in data.items() if k not in ("ok", "persisted")})
                    if data.get("persisted") is False:
                        self.toast(_("Changed, but the server could not save it in its .env "
                                     "file: it lasts until OpenDots restarts."))
                for key in ("provider", "model", "voiceName", "voiceModel", "baseUrl"):
                    if key in changes and key not in (data or {}):
                        merged[key] = changes[key]
                if changes.get("apiKey"):
                    keyed = changes.get("provider") or merged.get("provider")
                    keys = dict(merged.get("keys")) if isinstance(merged.get("keys"), dict) \
                        else None
                    if keys is not None and keyed:
                        keys[keyed] = True
                        merged["keys"] = keys
                    merged["hasApiKey"] = True
                if changes.get("voiceKey"):
                    merged["hasVoiceKey"] = True
                self._model[profile_id] = {"state": "ok", "data": merged, "error": "",
                                           "at": time.monotonic()}
                self._model_changed()
                mon = self.app.monitors.get(profile_id)
                if mon is not None:
                    mon.poll_now()
                # Names, prices, keys and the call models follow from the change: ask again.
                if profile_id == self.profile_id:
                    self.request_model_config(force=True)
            done(error is None, error)

        self.with_api(lambda client: client.set_model_config(got, **changes), profile)

    def try_model(self, done: Callable[[dict | None, str | None], None], **body) -> None:
        """POST /api/model-config/test: ``done({"ok", "ms", "error"?} | None, error)``."""
        profile = self.profile
        if profile is None:
            done(None, _("No server"))
            return

        def got(data, error) -> None:
            if not self._closing:
                done(data, error)

        self.with_api(lambda client: client.try_model(got, **body), profile)

    def _model_changed(self) -> None:
        for name in ("voice", "model"):
            page = self.pages.get(name)
            if page is not None:
                page.model_changed()

    # -- feedback --------------------------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=escape(text), timeout=3))

    def open_url(self, uri: str) -> None:
        window = getattr(self.app, "window", None)
        opener = getattr(window, "open_external", None)
        if callable(opener):
            opener(uri)
        else:
            Gtk.UriLauncher(uri=uri).launch(self, None, None)

    # -- closing ---------------------------------------------------------------------------

    def dirty_editors(self) -> list:
        return [editor for editor in self.editors.values() if editor.dirty]

    def _on_close_request(self, _window) -> bool:
        dirty = self.dirty_editors()
        if dirty and not self._closing:
            names = ", ".join(editor.title for editor in dirty)
            dialog = Adw.AlertDialog(
                heading=_("Save Changes?"),
                body=_("Your changes to {names} are not saved yet.").format(names=names))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("discard", _("Discard"))
            dialog.add_response("save", _("Save"))
            dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")

            state = {"close": False, "dialog_closed": False}

            def maybe_close() -> None:
                # Only once the dialog is gone: closing with a dialog open closes the dialog.
                if state["close"] and state["dialog_closed"]:
                    GLib.idle_add(lambda: (self.close(), False)[1])

            def dialog_closed(_dialog) -> None:
                state["dialog_closed"] = True
                maybe_close()

            def close_later() -> None:
                state["close"] = True
                maybe_close()

            dialog.connect("closed", dialog_closed)

            def chosen(_dialog, response: str) -> None:
                if response == "discard":
                    for editor in dirty:
                        editor.discard()
                    close_later()
                elif response == "save":
                    pending = {"n": len(dirty), "ok": True}

                    def saved(ok: bool) -> None:
                        pending["n"] -= 1
                        pending["ok"] = pending["ok"] and ok
                        if pending["n"] == 0 and pending["ok"]:
                            close_later()

                    for editor in dirty:
                        editor.save(saved)

            dialog.connect("response", chosen)
            dialog.present(self)
            return True
        self._shutdown()
        return False

    def _shutdown(self) -> None:
        global _instance
        if self._closing:
            return
        self._closing = True
        for page in self.pages.values():
            try:
                page.destroy()
            except Exception:
                log.exception("Could not close the %s settings", page.name)
        for editor in list(self.editors.values()):
            editor.close()
        self.editors.clear()
        for obj, handler in self._handlers + self._monitor_handlers:
            try:
                obj.disconnect(handler)
            except TypeError:
                pass
        self._handlers, self._monitor_handlers = [], []
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = 0
        if _instance is self:
            _instance = None


def _plain(text: str) -> str:
    """Row titles may carry markup (escaped server names); search the visible text."""
    if "&" not in text and "<" not in text:
        return text
    try:
        # PyGObject returns (ok, attributes, text, accelerator).
        result = Pango.parse_markup(text, -1, "\0")
    except GLib.Error:
        return text
    plain = result[-2] if isinstance(result, tuple) and len(result) >= 3 else None
    return plain if isinstance(plain, str) else text


class _ResultRow(Gtk.ListBoxRow):
    def __init__(self, entry: search.Entry):
        super().__init__()
        self.entry = entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        title = Gtk.Label(label=entry.title, xalign=0, wrap=True, lines=2,
                          ellipsize=Pango.EllipsizeMode.END,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        box.append(title)
        where = entry.page_title if entry.is_page or not entry.group else \
            f"{entry.page_title} › {entry.group}"
        location = Gtk.Label(label=where if not entry.is_page else _("Page"), xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
        location.add_css_class("dim-label")
        location.add_css_class("caption")
        box.append(location)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{entry.title}, {where}"])
