"""The main window: a native sidebar with the Dots, and the server's page next to it.

The OpenDots page runs in "focus mode" (its own sidebar and top bar hidden, see
src/web/focus.js); every click in the native sidebar is turned into a page action.
When a page is not recognized (an OpenDots version with a different layout), the
native sidebar steps aside and the page shows its own navigation, as in Dotdeck 0.2.
"""

from __future__ import annotations

import re
import time
from typing import Callable

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from .. import config
from ..core import api, costs, dotprefs, models, monitor, pets, server, store, voice
from ..core.call_sounds import CallSounds
from ..core.store import MIC_ALLOW, MIC_DENY, Profile
from ..core.workspace import Dot, Workspace
from ..i18n import N_, _, ngettext
from ..web.webview import ServerView
from . import pet as pet_ui
from .call_panel import CallPanel
from .onboarding import Onboarding
from .pet import PetAvatar
from .sidebar import Sidebar, dot_menu

ZOOM_STEPS = (0.5, 0.67, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
DEFAULT_THREAD_TITLE = "A new thought"
# From the click on Call until the page's call connects (creating the conversation, then the
# page's own call button, which can take up to 10 s): after this long the button is freed.
CALL_PENDING_SECONDS = 25
# What the Dot is doing in a call: shown by its pet in the header, and said in its tooltip.
PHASE_WORDS = {"connecting": N_("Connecting…"), "listening": N_("Listening"),
               "thinking": N_("Thinking…"), "speaking": N_("Speaking")}


def call_subtitle(phase: str, elapsed: str, *, connected: bool, compact: bool,
                  cost: float | None = None) -> str:
    """The header's second line during a call with the open Dot.

    One width for the whole call ("In a call · 01:23", tabular digits): the pet shows
    whether the Dot listens, thinks or speaks, so the centred title stays put. Before the
    call connects there is no time yet; ``compact`` windows keep just the time. The cost
    so far follows the time when the voice gateway reports it ("· US$ 0,004").
    """
    if phase == "ended":
        return _("Saving the call…")
    if phase == "connecting":
        return _("Reconnecting…") if connected else _("Calling…")
    if compact:
        return elapsed
    if cost is not None:
        return _("In a call · {time} · {cost}").format(time=elapsed, cost=costs.usd(cost))
    return _("In a call · {time}").format(time=elapsed)


def ago(timestamp: float) -> str:
    """"3 min ago" style times for the conversation list (timestamps in s or ms)."""
    if timestamp > 1e12:
        timestamp /= 1000
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return _("Just now")
    if delta < 3600:
        n = int(delta // 60)
        return ngettext("{n} minute ago", "{n} minutes ago", n).format(n=n)
    if delta < 86400:
        n = int(delta // 3600)
        return ngettext("{n} hour ago", "{n} hours ago", n).format(n=n)
    if delta < 2 * 86400:
        return _("Yesterday")
    return GLib.DateTime.new_from_unix_local(int(timestamp)).format("%x")


RAIL_WIDTH = 88               # the sidebar as a rail of pets (Ctrl+B)
SNAP_TO_RAIL = 210            # dragged narrower than this, it snaps to the rail
WIDTH_RANGE = (240, 400)      # the widths it can be dragged to


def dragged_width(start: float, offset: float, rtl: bool = False) -> tuple[int, bool]:
    """The sidebar's width, and whether it is the rail, after dragging its edge ``offset``
    px from ``start`` px (in right-to-left languages the edge is on the left)."""
    width = start + (-offset if rtl else offset)
    if width < SNAP_TO_RAIL:
        return RAIL_WIDTH, True
    low, high = WIDTH_RANGE
    return int(max(low, min(high, width))), False


def step_through(order: list[str], at: str | None, back: bool = False) -> str:
    """The next Dot in ``order`` after ``at`` (the previous one with ``back``), round."""
    position = order.index(at) if at in order else (0 if back else -1)
    return order[(position + (-1 if back else 1)) % len(order)]


def latest_conversations(workspace: Workspace) -> dict[str, tuple[float, str]]:
    """Each Dot's latest conversation as ``(when, title)``, ``when`` in seconds."""
    latest: dict[str, tuple[float, str]] = {}
    for conversation in workspace.conversations:
        when = conversation.created_at
        when = when / 1000 if when > 1e12 else when
        if when > latest.get(conversation.dot_id, (0.0, ""))[0]:
            latest[conversation.dot_id] = (when, conversation.title)
    return latest


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "DotdeckMainWindow"

    def __init__(self, app):
        super().__init__(application=app, title=config.APP_NAME, icon_name=config.APP_ID)
        self.app = app
        settings = app.settings
        self.set_default_size(settings.get("window-width"), settings.get("window-height"))
        self.set_size_request(360, 420)
        if settings.get("window-maximized"):
            self.maximize()
        if config.DEVEL:
            self.add_css_class("devel")
        pet_ui.set_animations(settings.get("pet-animations"))

        self.view: ServerView | None = None
        self._view_profile: str | None = None
        self._seen_updates: str | None = None
        self._dot_id: str | None = None        # the Dot shown on the page
        self._place = "home"                    # home | dot | activity | memories | space
        self._space_id: str | None = None
        self._focus_ok: bool | None = None      # None until the page answers
        self._told_fallback = False
        self._call_dot: str | None = None
        self._call_started = 0.0
        self._call_timer = 0
        self._call_connected = False            # the call reached the Dot at least once
        self._call_pending: str | None = None   # a call being set up with this Dot
        self._call_pending_timer = 0
        self._call_focus_back = False           # Call had the keyboard focus when it dimmed
        # Costs (servers set up with the Dotdeck guide): the open conversation's, and the
        # call's (its voice from the gateway, plus what its conversation spent meanwhile).
        self._page_thread = ""                  # the conversation the page shows
        self._thread_costs: dict[str, dict] = {}
        self._costs_asked = 0.0
        self._call_id: str | None = None
        self._call_thread: str | None = None
        self._call_voice_cost: float | None = None
        self._call_base_cost: float | None = None   # its conversation's when it started
        self._very_narrow = False               # phone width: fewer buttons in the header
        self._prefs: dotprefs.DotPrefs | None = None
        self._prefs_handler = 0
        self._mon: monitor.ServerMonitor | None = None
        self._mon_handlers: list[int] = []
        self._pet_map: dict[str, str] = {}
        self._last_status: dict[str, str] = {}

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.pages)

        self.onboarding = Onboarding(app)
        self.onboarding.connect("finished", self._on_onboarding_finished)
        self.pages.add_named(self.onboarding, "onboarding")
        self.pages.add_named(self._build_workspace(), "workspace")

        self._install_actions()
        # Ctrl+Tab steps through the Dots in the sidebar's order, frozen while Ctrl is
        # held; letting go opens the one chosen (ADR-029). Before the page sees the keys.
        self._switch: dict | None = None
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_switch_key)
        keys.connect("key-released", self._on_switch_release)
        self.add_controller(keys)
        self.connect("notify::is-active", lambda *_: self.is_active() or self._end_switch(False))
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", lambda *_: self.call_panel.unparent())
        self.connect("notify::fullscreened", self._on_fullscreen)
        # Back in front: the open Dot's news count as seen (_dot_activity).
        self.connect("notify::is-active", lambda *_: (app.refresh_status(), self._sync_fast(),
                                                      self._refresh_workspace()))
        self.connect("notify::visible", lambda *_: self._sync_fast())
        settings.connect("changed", self._on_setting_changed)
        # Ringing while a call connects, a chime when it is live, a tone when it ends (on
        # the call's speaker). Silenced on quit before the app hangs up its calls.
        self.call_sounds = CallSounds.for_settings(settings)
        app.connect("shutdown", lambda *_: self.call_sounds.close())

        if app.profiles.active:
            self.show_profile(app.profiles.active)
        else:
            self.pages.set_visible_child_name("onboarding")

    # -- layout ----------------------------------------------------------------------------

    def _server_switcher(self) -> tuple[Gtk.MenuButton, Gtk.Label, Gtk.Label]:
        dot = Gtk.Label(label="●")
        dot.add_css_class("status-dot")
        name = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=18)
        name.add_css_class("heading")
        box = Gtk.Box(spacing=6)
        box.append(dot)
        box.append(name)
        button = Gtk.MenuButton(child=box, always_show_arrow=True, tooltip_text=_("Switch Server"))
        button.add_css_class("flat")
        return button, dot, name

    def _build_workspace(self) -> Gtk.Widget:
        # Sidebar: server switcher, search and the lists.
        self.server_button, dot1, name1 = self._server_switcher()
        sidebar_header = Adw.HeaderBar()
        new_menu = Gio.Menu()
        new_menu.append(_("New Chat"), "win.new-chat")
        new_menu.append(_("New Dot…"), "win.new-dot")
        sidebar_header.pack_start(Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=new_menu,
                                                 tooltip_text=_("New")))
        sidebar_header.set_title_widget(self.server_button)
        search = Gtk.Button(icon_name="system-search-symbolic", action_name="win.search",
                            tooltip_text=_("Search (Ctrl+K)"))
        sidebar_header.pack_end(search)
        self.search_button = search
        self.sidebar = Sidebar()
        self.sidebar.connect("dot-activated", lambda _s, dot_id: self.open_dot(dot_id))
        self.sidebar.connect("space-activated", lambda _s, space_id: self.open_space(space_id))
        self.sidebar.connect("nav-activated", lambda _s, key: self._on_nav(key))
        self.sidebar.connect("new-dot", lambda *_: self.new_dot())
        self.sidebar.menu_for_dot = self._menu_for_dot
        sidebar_view = Adw.ToolbarView(content=self.sidebar)
        sidebar_view.add_top_bar(sidebar_header)
        # Its edge can be dragged (240–400 px), and below 210 px it snaps to the rail of
        # pets, as the Grok Bot's sidebar does (ADR-029).
        self.resize_handle = Gtk.Box(width_request=6, halign=Gtk.Align.END,
                                     cursor=Gdk.Cursor.new_from_name("col-resize"))
        self.resize_handle.add_css_class("sidebar-resize-handle")
        self.resize_handle.set_tooltip_text(_("Drag to resize the sidebar (Ctrl+B: pets only)"))
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_resize_begin)
        drag.connect("drag-update", self._on_resize_update)
        drag.connect("drag-end", self._on_resize_end)
        self.resize_handle.add_controller(drag)
        twice = Gtk.GestureClick()          # a double click puts the width back
        twice.connect("pressed", lambda _g, n, *_: n == 2 and self._reset_sidebar_width())
        self.resize_handle.add_controller(twice)
        sidebar_overlay = Gtk.Overlay(child=sidebar_view)
        sidebar_overlay.add_overlay(self.resize_handle)
        sidebar_overlay.set_clip_overlay(self.resize_handle, False)

        # Content: the open Dot in the header, then the page.
        header = Adw.HeaderBar()
        self.header = header
        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                               tooltip_text=_("Show Sidebar (F9)"),
                                               action_name="win.toggle-sidebar")
        header.pack_start(self.sidebar_button)
        # Used when the native sidebar is off or the page was not recognized.
        self.server_button_fallback, dot2, name2 = self._server_switcher()
        header.pack_start(self.server_button_fallback)
        self._status_dots = (dot1, dot2)
        self._server_labels = (name1, name2)
        self.reload_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                        tooltip_text=_("Reload (F5)"), action_name="win.reload")
        header.pack_start(self.reload_button)

        self.title_avatar = PetAvatar(None, size=30, visible=False)
        self.title = Adw.WindowTitle(title=config.APP_NAME)
        self.title.add_css_class("dotdeck-title")
        title_box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.title_box = title_box
        title_box.append(self.title_avatar)
        title_box.append(self.title)
        header.set_title_widget(title_box)
        # The call panel floats under the title during a call; a click on the title brings
        # it back after it was hidden (ADR-029).
        self.call_panel = CallPanel(on_settings=lambda: self.app.show_preferences("voice"))
        self.call_panel.set_parent(header)
        self.call_panel.connect("closed", lambda *_: self._on_call_panel_closed())
        self._call_panel_hidden = False
        reopen = Gtk.GestureClick()
        reopen.connect("released", lambda *_: self._show_call_panel(by_owner=True))
        title_box.add_controller(reopen)

        menu = Gio.Menu()
        section = Gio.Menu()
        # Only in very narrow windows, which have no room for the Conversations button.
        item = Gio.MenuItem.new(_("Conversations"), "win.conversations")
        item.set_attribute_value("hidden-when", GLib.Variant("s", "action-disabled"))
        section.append_item(item)
        section.append(_("Reload"), "win.reload")
        section.append(_("Show Computer"), "win.toggle-computer")
        section.append(_("Pause All Dots"), "win.pause-all")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Zoom In"), "win.zoom-in")
        section.append(_("Zoom Out"), "win.zoom-out")
        section.append(_("Actual Size"), "win.zoom-reset")
        section.append(_("Fullscreen"), "win.fullscreen")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Settings"), "app.preferences")
        section.append(_("Keyboard Shortcuts"), "app.shortcuts")
        section.append(_("About Dotdeck"), "app.about")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Quit"), "app.quit")
        menu.append_section(None, section)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                          primary=True, tooltip_text=_("Main Menu"))
        header.pack_end(self.menu_button)

        self.call_content = Adw.ButtonContent(icon_name="call-start-symbolic", label=_("Call"))
        # While a call is being set up: a spinner and "Calling…", and no second click.
        self.call_pending_label = Gtk.Label(label=_("Calling…"))
        calling = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        calling.append(Adw.Spinner())
        calling.append(self.call_pending_label)
        self.call_stack = Gtk.Stack(hhomogeneous=False)
        self.call_stack.add_named(self.call_content, "call")
        self.call_stack.add_named(calling, "calling")
        self.call_button = Gtk.Button(child=self.call_stack, action_name="win.call",
                                      visible=False)
        self.call_button.add_css_class("call-button")
        header.pack_end(self.call_button)

        self.mic_button = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic",
                                           visible=False, action_name="win.toggle-mute",
                                           tooltip_text=_("Mute Microphone (Ctrl+Shift+M)"))
        header.pack_end(self.mic_button)
        # In push-to-talk mode the same button talks while it is held down.
        hold = Gtk.GestureClick()
        hold.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        hold.connect("pressed", lambda g, *_: self._hold_to_talk(g, True))
        hold.connect("released", lambda g, *_: self._hold_to_talk(g, False))
        hold.connect("cancel", lambda g, *_: self._hold_to_talk(g, False))
        self.mic_button.add_controller(hold)

        popover, self.history_list = self._history_popover()
        self.history_button = Gtk.MenuButton(icon_name="document-open-recent-symbolic",
                                             popover=popover, visible=False,
                                             tooltip_text=_("Conversations"))
        header.pack_end(self.history_button)
        self.new_chat_button = Gtk.Button(icon_name="chat-message-new-symbolic",
                                          action_name="win.new-chat", visible=False,
                                          tooltip_text=_("New Chat (Ctrl+N)"))
        header.pack_end(self.new_chat_button)

        self._tasks_label = Gtk.Label()
        tasks = Gtk.Box(spacing=6)
        tasks.append(Adw.Spinner())
        tasks.append(self._tasks_label)
        self.tasks_button = Gtk.Button(child=tasks, visible=False, action_name="win.activity",
                                       tooltip_text=_("Show Activity"))
        self.tasks_button.add_css_class("flat")
        header.pack_end(self.tasks_button)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_clicked)
        self._banner_action: Callable[[], None] | None = None

        self.content = Gtk.Stack()
        self._loading = Adw.StatusPage(child=Adw.Spinner(width_request=32, height_request=32))
        self.content.add_named(self._loading, "loading")

        view = Adw.ToolbarView(content=self.content, top_bar_style=Adw.ToolbarStyle.RAISED)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        self.toolbar = view

        # pin_sidebar: un-collapsing must not bring back a sidebar that is off or hidden;
        # _apply_layout decides.
        self.split = Adw.OverlaySplitView(sidebar=sidebar_overlay, content=view,
                                          min_sidebar_width=264, max_sidebar_width=320,
                                          sidebar_width_fraction=1.0,
                                          sidebar_width_unit=Adw.LengthUnit.PX,
                                          pin_sidebar=True)
        self.split.connect("notify::show-sidebar", lambda *_: self._sync_sidebar_button())
        self.split.connect("notify::collapsed", lambda *_: self._apply_layout())
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        # Phone width: Conversations moves into the main menu so the Dot's name fits. The
        # last matching breakpoint wins, so this one collapses the sidebar too.
        very_narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 480sp"))
        very_narrow.add_setter(self.split, "collapsed", True)
        very_narrow.connect("apply", lambda *_: self._set_very_narrow(True))
        very_narrow.connect("unapply", lambda *_: self._set_very_narrow(False))
        self.add_breakpoint(very_narrow)
        self._apply_layout()
        return self.split

    def _set_very_narrow(self, value: bool) -> None:
        self._very_narrow = value
        self._refresh_header()

    def _install_actions(self) -> None:
        def add(name, callback, parameter=None):
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            return action

        text = GLib.VariantType("s")
        add("reload", lambda *_: self.view and self.view.reload())
        add("zoom-in", lambda *_: self._zoom(+1))
        add("zoom-out", lambda *_: self._zoom(-1))
        add("zoom-reset", lambda *_: self._zoom(0))
        add("fullscreen", lambda *_: self.unfullscreen() if self.is_fullscreen()
            else self.fullscreen())
        add("activity", lambda *_: self.open_activity())
        add("memories", lambda *_: self.open_memories())
        add("new-chat", lambda *_: self.new_chat())
        add("new-dot", lambda *_: self.new_dot())
        add("talk-to-chief", lambda *_: self.talk_to_chief())
        add("go-back", lambda *_: self.view and self.view.webview.go_back())
        add("search", lambda *_: self._search())
        add("toggle-sidebar", lambda *_: self._toggle_sidebar())
        add("toggle-rail", lambda *_: self._toggle_rail())
        add("toggle-computer", lambda *_: self.view and self.view.toggle_computer())
        self._conversations_action = add("conversations", lambda *_: self._show_conversations())
        self._conversations_action.set_enabled(False)
        self._pause_action = Gio.SimpleAction.new_stateful("pause-all", None,
                                                           GLib.Variant("b", False))
        self._pause_action.connect("activate", lambda *_: self._pause_all())
        self.add_action(self._pause_action)
        add("call", lambda *_: self.call_current())
        add("previous-dot", lambda *_: self._step_dot(-1))
        add("next-dot", lambda *_: self._step_dot(+1))
        add("dot-open", lambda _a, v: self.open_dot(v.unpack()), text)
        add("dot-new-chat", lambda _a, v: self.new_chat(v.unpack()), text)
        add("dot-call", lambda _a, v: self.call_dot(v.unpack()), text)
        add("dot-pet", lambda _a, v: self.change_pet(v.unpack()), text)
        add("dot-edit", lambda _a, v: self.app.show_preferences("dots", dot=v.unpack()), text)
        add("dot-pin", lambda _a, v: self._toggle_pin(v.unpack()), text)
        add("dot-hide", lambda _a, v: self._hide_dot(v.unpack()), text)
        add("dot-unhide", lambda _a, v: self._prefs and self._prefs.set_hidden(v.unpack(), False),
            text)
        add("dot-chief", lambda _a, v: self._make_chief(v.unpack()), text)
        self._mute_action = Gio.SimpleAction.new_stateful("toggle-mute", None,
                                                          GLib.Variant("b", False))
        self._mute_action.connect("activate", lambda *_: self.toggle_mute())
        self._mute_action.set_enabled(False)
        self.add_action(self._mute_action)
        switch = Gio.SimpleAction.new_stateful("profile", text, GLib.Variant("s", ""))
        switch.connect("activate", lambda _a, value: self.switch_to(value.unpack()))
        self.add_action(switch)
        self._profile_action = switch
        for index in range(1, 10):
            add(f"profile-{index}", lambda _a, _p, i=index: self._switch_index(i))
            add(f"dot-{index}", lambda _a, _p, i=index: self._open_dot_index(i))

    # -- servers ---------------------------------------------------------------------------

    def rebuild_server_menu(self) -> None:
        menu = Gio.Menu()
        profiles = Gio.Menu()
        for profile in self.app.profiles.list():
            item = Gio.MenuItem.new(profile.name, None)
            item.set_action_and_target_value("win.profile", GLib.Variant("s", profile.id))
            profiles.append_item(item)
        menu.append_section(None, profiles)
        manage = Gio.Menu()
        manage.append(_("Add Server…"), "app.add-server")
        manage.append(_("Manage Servers…"), "app.manage-servers")
        menu.append_section(None, manage)
        self.server_button.set_menu_model(menu)
        self.server_button_fallback.set_menu_model(menu)
        active = self.app.profiles.active
        self._profile_action.set_state(GLib.Variant("s", active.id if active else ""))

    def _switch_index(self, index: int) -> None:
        profiles = self.app.profiles.list()
        if index <= len(profiles):
            self.switch_to(profiles[index - 1].id)

    def switch_to(self, profile_id: str) -> None:
        """Show another server; switching ends a call in progress, so ask first."""
        active = self.app.profiles.active
        if active is not None and active.id == profile_id:
            return
        self._confirm_leave_call(None, lambda: self.app.profiles.set_active(profile_id))

    def show_onboarding(self) -> None:
        self._drop_view()
        self.onboarding.reset()
        self.pages.set_visible_child_name("onboarding")

    def _on_onboarding_finished(self, _onboarding, profile_id: str) -> None:
        self.app.onboarding_finished(profile_id)

    def show_profile(self, profile: Profile) -> None:
        self.pages.set_visible_child_name("workspace")
        self.rebuild_server_menu()
        self._watch(profile)
        if self.view is not None and self._view_profile == profile.id \
                and self.view.profile.url == profile.url:
            self.view.profile = profile
            self.refresh_status()
            return
        self._drop_view()
        self._view_profile = profile.id
        self._dot_id, self._place, self._focus_ok = None, "home", None
        self._told_fallback = False
        self._apply_layout()
        self.content.set_visible_child_name("loading")
        self.refresh_status()

        def with_token(token: str | None) -> None:
            if self._view_profile != profile.id or self.view is not None:
                return
            view = ServerView(profile, token, self, self.app.settings.get("zoom"))
            view.voice.connect("calls-changed", lambda *_: self._on_calls_changed())
            self.call_sounds.watch(view.voice)
            if GObject.signal_lookup("call-phase", type(view.voice)):
                view.voice.connect("call-phase", self._on_call_phase)
                view.voice.connect("call-error", lambda _v, message: self.toast(message))
            if GObject.signal_lookup("call-cost", type(view.voice)):
                view.voice.connect("call-cost", self._on_call_cost)
            if GObject.signal_lookup("audio-level", type(view.voice)):
                view.voice.connect("audio-level", self._on_audio_level)
            if GObject.signal_lookup("caption", type(view.voice)):
                view.voice.connect("caption", lambda _v, role, text, final:
                                   self.call_panel.add_caption(role, text, final))
            view.connect("state-changed", lambda *_: self.refresh_status())
            if hasattr(view, "set_focus_mode"):
                view.connect("focus-changed", self._on_focus_changed)
                view.connect("page-selection", self._on_page_selection)
                if GObject.signal_lookup("page-thread", type(view)):
                    view.connect("page-thread", self._on_page_thread)
                view.connect("dot-state", self._on_dot_state)
                view.set_focus_mode(bool(self.app.settings.get("native-sidebar")))
                view.sync_color_scheme(self.app.settings.get("color-scheme"))
            self.view = view
            self.content.add_named(view, f"view-{profile.id}")
            self.content.set_visible_child(view)
            self._push_pets()

        self.app.tokens.lookup(profile.id, profile.url, with_token)

    def _drop_view(self) -> None:
        if self.view is not None:
            if self.view.voice.in_call:
                self.toast(_("The call was ended."))
            self.view.destroy()
            self.content.remove(self.view)
            self.view = None
        self._view_profile = None
        self._set_call_pending(None)
        self._on_calls_changed()

    def reload_with_token(self, profile_id: str, token: str | None) -> None:
        if self.view is not None and self.view.profile.id == profile_id:
            self.view.set_token(token)

    def _watch(self, profile: Profile) -> None:
        """Follow the active server's monitor and the owner's choices for its Dots."""
        mon = self.app.monitors.get(profile.id)
        if mon is not self._mon:
            if self._mon is not None:
                for handler in self._mon_handlers:
                    self._mon.disconnect(handler)
                self._mon.set_fast(False)
            self._mon_handlers = []
            self._mon = mon
            if mon is not None:
                self._mon_handlers = [
                    mon.connect("workspace-changed", lambda *_: self._refresh_workspace()),
                    mon.connect("status-changed", lambda *_: self._refresh_workspace()),
                    mon.connect("spend-changed", lambda *_: self._refresh_spend()),
                    mon.connect("changed", lambda *_: self._refresh_costs()),
                ]
        prefs = dotprefs.for_profile(profile.id)
        if prefs is not self._prefs:
            if self._prefs is not None and self._prefs_handler:
                self._prefs.disconnect(self._prefs_handler)
            self._prefs = prefs
            self._prefs_handler = prefs.connect("changed", lambda *_: self._refresh_workspace())
        self._sync_fast()
        self._refresh_workspace()

    def _sync_fast(self) -> None:
        if self._mon is not None:
            self._mon.set_fast(bool(self.get_visible()))

    # -- layout state ----------------------------------------------------------------------

    def _native(self) -> bool:
        """The native sidebar is in charge (setting on and the page recognized)."""
        return bool(self.app.settings.get("native-sidebar")) and self._focus_ok is not False

    def _apply_layout(self) -> None:
        native = self._native()
        self.split.set_show_sidebar(native and bool(self.app.settings.get("sidebar-visible"))
                                    and not self.split.get_collapsed())
        self.split.set_enable_show_gesture(native)
        self.split.set_enable_hide_gesture(native)
        self.server_button_fallback.set_visible(not native)
        self.reload_button.set_visible(not native)
        self._apply_sidebar_width()
        self._sync_sidebar_button()
        self._refresh_header()

    def _rail(self) -> bool:
        """Pets only: the setting, and room for the sidebar beside the page."""
        return bool(self.app.settings.get("sidebar-rail")) and not self.split.get_collapsed()

    def _apply_sidebar_width(self, width: int | None = None, rail: bool | None = None) -> None:
        """The sidebar's width: the dragged one (or ``width`` while dragging), the rail's,
        or, over the page in a narrow window, at most 320 px."""
        rail = self._rail() if rail is None else rail
        if rail:
            width = RAIL_WIDTH
        else:
            low, high = WIDTH_RANGE
            width = max(low, min(high, int(width or self.app.settings.get("sidebar-width"))))
            if self.split.get_collapsed():
                width = min(width, 320)
        # Equal limits pin the width (the fraction is 1.0, so the limits decide).
        self.split.set_min_sidebar_width(width)
        self.split.set_max_sidebar_width(width)
        self.sidebar.set_rail(rail)
        self.server_button.set_visible(not rail)
        self.search_button.set_visible(not rail)
        self.resize_handle.set_visible(not self.split.get_collapsed())
        if rail and self._native():
            self.server_button_fallback.set_visible(True)   # the switcher moves to the page

    def _toggle_rail(self) -> None:
        if not self._native() or self.split.get_collapsed():
            return
        self.app.settings.set("sidebar-rail", not self.app.settings.get("sidebar-rail"))
        self.split.set_show_sidebar(True)
        self._apply_layout()

    def _reset_sidebar_width(self) -> None:
        self.app.settings.set("sidebar-width", store.Settings.DEFAULTS["sidebar-width"])
        self.app.settings.set("sidebar-rail", False)
        self._apply_layout()

    def _on_resize_begin(self, gesture, _x, _y) -> None:
        self._resize_from = RAIL_WIDTH if self._rail() else self.split.get_min_sidebar_width()
        self._resize_start = self._pointer_x(gesture)

    def _pointer_x(self, gesture: Gtk.GestureDrag) -> float:
        """Where the pointer is across the window. The drag's own offsets are in the
        handle's coordinates, and the handle moves with the edge: the sidebar followed the
        pointer at half speed and, with events arriving between frames, shook."""
        _ok, x, y = gesture.get_point(None)
        _ok, point = self.resize_handle.compute_point(self, Graphene.Point().init(x, y))
        return point.x

    def _dragged_width(self, gesture: Gtk.GestureDrag) -> tuple[int, bool]:
        moved = self._pointer_x(gesture) - getattr(self, "_resize_start", 0.0)
        return dragged_width(getattr(self, "_resize_from", RAIL_WIDTH), moved,
                             self.get_direction() == Gtk.TextDirection.RTL)

    def _on_resize_update(self, gesture, _offset_x: float, _offset_y: float) -> None:
        self._apply_sidebar_width(*self._dragged_width(gesture))

    def _on_resize_end(self, gesture, _offset_x: float, _offset_y: float) -> None:
        width, rail = self._dragged_width(gesture)
        settings = self.app.settings
        if not rail:
            settings.set("sidebar-width", width)
        settings.set("sidebar-rail", rail)
        self._apply_layout()

    # -- Ctrl+Tab --------------------------------------------------------------------------

    def _on_switch_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if keyval not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab) or \
                not state & Gdk.ModifierType.CONTROL_MASK or not self._native():
            return False
        if self._switch is None:
            order = self.sidebar.visible_dot_ids()
            if not order:
                return False
            self._switch = {"order": order, "at": self._dot_id}
        back = keyval == Gdk.KEY_ISO_Left_Tab or bool(state & Gdk.ModifierType.SHIFT_MASK)
        self._switch["at"] = step_through(self._switch["order"], self._switch["at"], back)
        self.split.set_show_sidebar(True)
        self.sidebar.select_dot(self._switch["at"])    # shown, not opened yet
        return True

    def _on_switch_release(self, _controller, keyval: int, _keycode: int, _state) -> None:
        if keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R):
            self._end_switch(True)

    def _end_switch(self, open_it: bool) -> None:
        switch, self._switch = self._switch, None
        if switch is None:
            return
        if open_it and switch["at"] and switch["at"] != self._dot_id:
            self.open_dot(switch["at"])
        else:
            self._select_current()

    def _sync_sidebar_button(self) -> None:
        native = self._native()
        shown = self.split.get_show_sidebar()
        self.sidebar_button.set_visible(native and (self.split.get_collapsed() or not shown))
        self.sidebar_button.set_active(shown)

    def _toggle_sidebar(self) -> None:
        if not self._native():
            return
        shown = not self.split.get_show_sidebar()
        self.split.set_show_sidebar(shown)
        if not self.split.get_collapsed():
            self.app.settings.set("sidebar-visible", shown)

    def _search(self) -> None:
        if not self._native():
            return
        self.split.set_show_sidebar(True)
        self.sidebar.show_search()

    def _on_focus_changed(self, _view, active: bool) -> None:
        was = self._focus_ok
        self._focus_ok = bool(active)
        if not active and self.app.settings.get("native-sidebar") and was is not False \
                and not self._told_fallback:
            self._told_fallback = True
            self.toast(_("This server's page has a different layout, so it shows its own "
                         "navigation."))
        self._apply_layout()

    def _on_setting_changed(self, _settings, key: str) -> None:
        if key in ("density", "show-jobs"):
            self._refresh_workspace()
        elif key in ("sidebar-width", "sidebar-rail"):
            self._apply_layout()
        elif key == "pet-animations":
            pet_ui.set_animations(self.app.settings.get(key))
            self.queue_draw()
        elif key == "pets-in-page":
            self._push_pets()
        elif key == "native-sidebar":
            if self.view is not None and hasattr(self.view, "set_focus_mode"):
                self.view.set_focus_mode(bool(self.app.settings.get(key)))
            self._apply_layout()
        elif key == "color-scheme" and self.view is not None and \
                hasattr(self.view, "sync_color_scheme"):
            self.view.sync_color_scheme(self.app.settings.get(key))

    # -- workspace data --------------------------------------------------------------------

    @property
    def workspace(self) -> Workspace:
        return self._mon.workspace if self._mon is not None else Workspace()

    def current_dot(self) -> Dot | None:
        return self.workspace.dot(self._dot_id)

    def _refresh_workspace(self) -> None:
        profile = self.app.profiles.active
        if profile is None or self._prefs is None:
            return
        workspace = self.workspace
        settings = self.app.settings
        self._pet_map = self._prefs.pets_for(workspace.dots) if workspace.dots else {}
        visible = self._prefs.ordered(workspace.dots)
        hidden = self._prefs.hidden(workspace.dots)
        pinned = {d.id for d in workspace.dots if self._prefs.is_pinned(d.id)}
        call_dot = workspace.dot_in_call()
        if self.view is not None and self.view.voice.in_call and self._call_dot:
            call_dot = workspace.dot(self._call_dot) or call_dot
        status = self._mon.status if self._mon is not None else "unknown"
        activity, unread = self._dot_activity(workspace)
        chief = workspace.dot_named(monitor.chief_dot(profile, [d.name for d in workspace.dots]))
        self.sidebar.set_placeholder({
            "unknown": _("Connecting…"), "offline": _("No connection to the server"),
            "token": _("An access token is needed to see the Dots"),
            "local-only": _("This server only accepts connections from its own computer"),
            "other": _("This address is not an OpenDots server"),
        }.get(status, _("No Dots yet") if status == "online" else ""))
        self.sidebar.set_dots(workspace, visible, hidden, self._pet_map,
                              density=settings.get("density"), show_job=settings.get("show-jobs"),
                              pinned=pinned, in_call=call_dot.id if call_dot else None,
                              offline=bool(self._mon and self._mon.status != "online"),
                              chief=chief.id if chief else None, unread=unread,
                              activity=activity)
        self.sidebar.set_model(models.at_a_glance(
            workspace.active_model, workspace.active_provider,
            getattr(self._mon, "model_config", None)))
        self._refresh_spend()
        if self.view is not None and self.view.voice.in_call and self._call_id is None:
            call = workspace.active_call()
            if call is not None:
                self._call_id, self._call_thread = call.id, call.thread_id
                self._refresh_costs(force=True)
        if self._place == "dot" and self._dot_id:
            self.sidebar.select_dot(self._dot_id)
        pending = getattr(self, "_pending_dot", None)
        if pending and workspace.dot(pending) is not None:
            self._pending_dot = None
            GLib.idle_add(lambda: (self.open_dot(pending), False)[1])
        self._push_pets()
        self._refresh_header()

    def _dot_activity(self, workspace: Workspace) -> tuple[dict[str, tuple[float, str]],
                                                             set[str]]:
        """Each Dot's latest conversation as ``(when, title)``, and the Dots with one the
        owner has not seen: it began after they last looked at that Dot (ADR-029).

        The open Dot, with the window in front, is being looked at, so what appears there
        is seen at once. Times stay in the server's clock (conversations' ``createdAt``)."""
        activity = latest_conversations(workspace)
        prefs = self._prefs
        if prefs is None:
            return activity, set()
        # What was there before Dotdeck first saw a Dot is not news, and a Dot first seen
        # without conversations gets its first one as news.
        now = time.time()
        prefs.seed_seen({dot.id: activity.get(dot.id, (now, ""))[0] for dot in workspace.dots})
        looking = self._place == "dot" and self.get_visible() and self.is_active()
        unread = set()
        for dot_id, (when, _title) in activity.items():
            if when <= prefs.last_seen(dot_id):
                continue
            if looking and dot_id == self._dot_id:
                prefs.mark_seen(dot_id, when, notify=False)
            else:
                unread.add(dot_id)
        return activity, unread

    def _push_pets(self) -> None:
        if self.view is None or not hasattr(self.view, "set_pets"):
            return
        names = {d.name: self._pet_map[d.id] for d in self.workspace.dots if d.id in self._pet_map}
        self.view.set_pets(names, bool(self.app.settings.get("pets-in-page")))

    # -- navigation ------------------------------------------------------------------------

    def _confirm_leave_call(self, dot_id: str | None, then: Callable[[], None]) -> None:
        """Opening another Dot unmounts the page's call: ask first."""
        saving = getattr(self, "_call_phase", "") == "ended"   # already hung up
        if not (self.view and self.view.voice.in_call) or saving or \
                dot_id == self._call_dot or not self.app.settings.get("confirm-call-end"):
            then()
            return
        current = self.workspace.dot(self._call_dot)
        dialog = Adw.AlertDialog(
            heading=_("End the Call?"),
            body=_("This ends the call with {name}.").format(
                name=current.name if current else _("this Dot")))
        dialog.add_response("stay", _("Stay"))
        dialog.add_response("leave", _("End Call and Continue"))
        dialog.set_response_appearance("leave", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("stay")
        dialog.set_close_response("stay")

        def chosen(_dialog, response):
            if response == "leave":
                then()
            else:
                self._select_current()
        dialog.connect("response", chosen)
        dialog.present(self)

    def _select_current(self) -> None:
        if self._place == "dot":
            self.sidebar.select_dot(self._dot_id)
        elif self._place in ("activity", "memories"):
            self.sidebar.select_nav(self._place)
        elif self._place == "space":
            self.sidebar.select_space(self._space_id)

    def _close_overlay(self) -> None:
        """In a narrow window the sidebar covers the page: hide it after choosing."""
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def open_dot(self, dot_id: str, conversation_id: str | None = None) -> None:
        dot = self.workspace.dot(dot_id)
        if dot is None:
            return

        def go():
            self._dot_id, self._place = dot.id, "dot"
            self.sidebar.select_dot(dot.id)
            self._close_overlay()
            if self._prefs is not None:
                # In the server's clock, like the conversations it is compared with.
                latest = self.workspace.latest_conversation(dot.id)
                when = latest.created_at if latest is not None else 0.0
                self._prefs.mark_seen(dot.id, max(time.time(),
                                                  when / 1000 if when > 1e12 else when))
            self._refresh_header()
            if self.view is None:
                return
            index = self.workspace.dot_index(dot.id)
            done = self._page_done(_("Could not open {name} on the page.").format(name=dot.name))
            if conversation_id and hasattr(self.view, "open_conversation"):
                self.view.open_conversation(conversation_id, index, dot.name, done)
            elif hasattr(self.view, "open_dot"):
                self.view.open_dot(index, dot.name, done)
            else:
                self.view.run_page_action("dot", dot.name, done)
        in_call = bool(self.view and self.view.voice.in_call)
        self._confirm_leave_call(None if conversation_id and in_call else dot.id, go)

    def _page_done(self, message: str) -> Callable[[bool], None]:
        def done(ok: bool) -> None:
            if not ok:
                self.toast(message)
        return done

    def open_space(self, space_id: str) -> None:
        def go():
            self._place, self._space_id = "space", space_id
            self.sidebar.select_space(space_id)
            self._close_overlay()
            self._refresh_header()
            if self.view is not None and hasattr(self.view, "open_space"):
                self.view.open_space(space_id)
        self._confirm_leave_call(None, go)

    def open_activity(self) -> None:
        def go():
            self._place = "activity"
            self.sidebar.select_nav("activity")
            self._close_overlay()
            self._refresh_header()
            if self.view is not None:
                if hasattr(self.view, "open_activity"):
                    self.view.open_activity()
                else:
                    self.view.run_page_action("activity")
        self._confirm_leave_call(None, go)

    def open_memories(self) -> None:
        def go():
            self._place = "memories"
            self.sidebar.select_nav("memories")
            self._close_overlay()
            self._refresh_header()
            if self.view is not None and hasattr(self.view, "open_memories"):
                self.view.open_memories()
        self._confirm_leave_call(None, go)

    def _on_nav(self, key: str) -> None:
        if key == "activity":
            self.open_activity()
        elif key == "memories":
            self.open_memories()
        elif key == "settings":
            self.sidebar.select_nav(None)
            self._select_current()
            self.app.show_preferences()
        elif key == "model":
            self.app.show_preferences("model")

    def _open_dot_index(self, index: int) -> None:
        ids = self.sidebar.visible_dot_ids()
        if index <= len(ids):
            self.open_dot(ids[index - 1])

    def _step_dot(self, step: int) -> None:
        ids = self.sidebar.visible_dot_ids()
        if not ids:
            return
        self.open_dot(step_through(ids, self._dot_id, back=step < 0))

    def _on_page_selection(self, _view, dot_name: str, place: str) -> None:
        """The page changed by itself (a link, a notification): follow it."""
        if place in ("chat", "home") and dot_name:
            dot = self.workspace.dot_named(dot_name)
            if dot is not None:
                self._dot_id, self._place = dot.id, "dot"
                self.sidebar.select_dot(dot.id)
        elif place == "tasks":
            self._place = "activity"
            self.sidebar.select_nav("activity")
        elif place == "memories":
            self._place = "memories"
            self.sidebar.select_nav("memories")
        elif place == "space":
            uri = self.view.webview.get_uri() if self.view is not None else ""
            found = re.search(r"#/spaces/([^/?#]+)", uri or "")
            self._place, self._space_id = "space", found.group(1) if found else None
            self.sidebar.select_space(self._space_id)
        self._refresh_header()

    def _on_dot_state(self, _view, dot_name: str, state: str) -> None:
        dot = self.workspace.dot_named(dot_name)
        before, self._page_state = getattr(self, "_page_state", None), (dot_name, state)
        if dot is not None and before == (dot_name, "working") and state == "idle":
            # The same Dot stopped working: it finished replying (not another Dot opened).
            conversation = next((c for c in self.workspace.conversations
                                 if c.id == self._page_thread), None)
            profile = self.app.profiles.active
            if profile is not None and hasattr(self.app, "notify_dot_done"):
                self.app.notify_dot_done(profile, dot, conversation.title if conversation
                                         else "")
        self.sidebar.set_dot_state(dot.id if dot else None, state)
        self._refresh_workspace()

    def showing_dot(self, dot_id: str) -> bool:
        """The page shows this Dot (its home or a conversation of it)."""
        return self._place == "dot" and self._dot_id == dot_id

    def new_chat(self, dot_id: str | None = None) -> None:
        dot = self.workspace.dot(dot_id) if dot_id else self.current_dot()
        if self.view is None:
            return
        if dot is None or not hasattr(self.view, "new_chat"):
            self.view.run_page_action("new-chat")
            return

        def go():
            self._dot_id, self._place = dot.id, "dot"
            self.sidebar.select_dot(dot.id)
            self._close_overlay()
            self._refresh_header()
            self.view.new_chat(self.workspace.dot_index(dot.id), dot.name,
                               self._page_done(_("Could not start a new chat.")))
        # Even for the Dot in the call: its empty composer drops the call's conversation.
        self._confirm_leave_call(None if self.view.voice.in_call else dot.id, go)

    def new_dot(self) -> None:
        profile = self.app.profiles.active
        if profile is None:
            return
        try:
            from .new_dot import create_dot
        except ImportError:
            self.app.show_preferences("dots")
            return

        def created(dot_id):
            if dot_id and self._mon is not None:
                self._pending_dot = dot_id   # opened as soon as the monitor sees it
                self._mon.poll_now()
        create_dot(self.app, profile, self, created)

    def change_pet(self, dot_id: str) -> None:
        profile, dot = self.app.profiles.active, self.workspace.dot(dot_id)
        if profile is None or dot is None:
            return
        try:
            from .pet_gallery import choose_pet
        except ImportError:
            return
        choose_pet(self.app, profile, dot, self)

    def _toggle_pin(self, dot_id: str) -> None:
        if self._prefs is not None:
            self._prefs.set_pinned(dot_id, not self._prefs.is_pinned(dot_id))

    def _hide_dot(self, dot_id: str) -> None:
        if self._prefs is None:
            return
        dot, prefs = self.workspace.dot(dot_id), self._prefs
        prefs.set_hidden(dot_id, True)
        if dot is not None:
            self.toast(_("{name} is hidden from the sidebar.").format(name=dot.name), _("Undo"),
                       lambda: prefs.set_hidden(dot_id, False))

    def _make_chief(self, dot_id: str) -> None:
        profile, dot = self.app.profiles.active, self.workspace.dot(dot_id)
        if profile is None or dot is None:
            return
        profile.chief = dot.name
        self.app.profiles.update(profile)
        self.toast(_("{name} is now the Chief.").format(name=dot.name))
        self.app.refresh_status()

    def _menu_for_dot(self, dot_id: str):
        dot = self.workspace.dot(dot_id)
        profile = self.app.profiles.active
        if dot is None or profile is None:
            return None
        chief = monitor.chief_dot(profile, [d.name for d in self.workspace.dots])
        return dot_menu(dot, pinned=bool(self._prefs and self._prefs.is_pinned(dot_id)),
                        chief=chief == dot.name, can_call=self._call_blocker(dot) is None,
                        hidden=bool(self._prefs and self._prefs.is_hidden(dot_id)))

    # -- conversations ---------------------------------------------------------------------

    def _history_popover(self) -> tuple[Gtk.Popover, Gtk.ListBox]:
        """The Conversations popover (filled each time it opens) and its list."""
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        listbox.connect("row-activated", self._on_history_row)
        scroller = Gtk.ScrolledWindow(child=listbox, propagate_natural_height=True,
                                      max_content_height=460, min_content_width=320,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        popover = Gtk.Popover(child=scroller)
        popover.add_css_class("menu")
        popover.connect("show", lambda *_: self._fill_history(listbox))
        return popover, listbox

    def _show_conversations(self) -> None:
        """Very narrow windows have no Conversations button: the list opens from the menu."""
        if self.current_dot() is None:
            return
        popover, _list = self._history_popover()
        # On the header bar, which re-sizes its popovers; pointing at the main menu button.
        popover.set_parent(self.header)
        _ok, bounds = self.menu_button.compute_bounds(self.header)
        area = Gdk.Rectangle()
        area.x, area.y = int(bounds.get_x()), int(bounds.get_y())
        area.width, area.height = int(bounds.get_width()), int(bounds.get_height())
        popover.set_pointing_to(area)

        def closed(popover):
            # GTK hands the focus back to the header bar, which cannot take it (it fell into
            # the page): it goes back to the menu the list was opened from.
            focus = self.get_focus()
            if focus is None or focus.is_ancestor(popover):
                self.menu_button.grab_focus()
            GLib.idle_add(popover.unparent)
        popover.connect("closed", closed)
        popover.popup()

    def _fill_history(self, listbox: Gtk.ListBox | None = None) -> None:
        if listbox is None:
            listbox = self.history_list
        while (child := listbox.get_first_child()) is not None:
            listbox.remove(child)
        dot = self.current_dot()
        if dot is None:
            return
        new = Gtk.ListBoxRow()
        new._conversation = None
        box = Gtk.Box(spacing=10)
        box.append(Gtk.Image(icon_name="chat-message-new-symbolic"))
        box.append(Gtk.Label(label=_("New Chat"), xalign=0))
        new.set_child(box)
        listbox.append(new)
        conversations = self.workspace.conversations_of(dot.id)
        # The page's chat header is hidden in focus mode: its two actions live here. Both
        # work on a conversation, so a Dot without any offers them dimmed.
        for icon, label, action in (("document-save-symbolic", _("Save Conversation as Page…"),
                                     "save-page"),
                                    ("alarm-symbolic", _("Schedule a Task…"), "schedule")):
            row = Gtk.ListBoxRow(sensitive=bool(conversations))
            row._conversation, row._page_action = None, action
            box = Gtk.Box(spacing=10)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0))
            row.set_child(box)
            listbox.append(row)
        divider = Gtk.ListBoxRow(activatable=False, selectable=False,
                                 child=Gtk.Separator(margin_top=4, margin_bottom=4))
        listbox.append(divider)
        rows = {}
        for conversation in conversations[:40]:
            row = Gtk.ListBoxRow()
            row._conversation = conversation.id
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            title = conversation.title if conversation.title != DEFAULT_THREAD_TITLE \
                else _("Conversation")
            label = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                              max_width_chars=40)
            when = Gtk.Label(label=ago(conversation.created_at), xalign=0)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            texts.append(label)
            texts.append(when)
            row.set_child(texts)
            listbox.append(row)
            rows[conversation.id] = (label, when)
        if not conversations:
            empty = Gtk.Label(label=_("No conversations yet"), margin_top=8, margin_bottom=8)
            empty.add_css_class("dim-label")
            listbox.append(Gtk.ListBoxRow(activatable=False, selectable=False, child=empty))
        profile = self.app.profiles.active
        if profile is None or not rows:
            return

        def got(data, error):
            if error or not isinstance(data, dict):
                return
            for thread in data.get("threads", []):
                if not isinstance(thread, dict) or thread.get("id") not in rows:
                    continue
                label, when = rows[thread["id"]]
                name = thread.get("name")
                if isinstance(name, str) and name and name != DEFAULT_THREAD_TITLE:
                    label.set_label(name)
                updated = thread.get("updatedAt")
                if isinstance(updated, str):
                    parsed = GLib.DateTime.new_from_iso8601(updated, None)
                    if parsed is not None:
                        when.set_label(ago(parsed.to_unix()))
        api.for_profile(self.app, profile, lambda client: client.threads(dot.id, got))

    def _on_history_row(self, listbox, row) -> None:
        popover = listbox.get_ancestor(Gtk.Popover)
        if popover is not None:
            popover.popdown()
        dot = self.current_dot()
        if dot is None:
            return
        action = getattr(row, "_page_action", None)
        if action and self.view is not None:
            self.view.run_page_action(action, done=self._page_done(
                _("Open a conversation first, and wait until the Dot is not working.")))
            return
        conversation = getattr(row, "_conversation", None)
        if conversation is None:
            self.new_chat(dot.id)
        else:
            self.open_dot(dot.id, conversation)

    # -- calls -----------------------------------------------------------------------------

    def _call_blocker(self, dot: Dot | None) -> str | None:
        """Why a call cannot start now, in words for people (None = it can)."""
        profile = self.app.profiles.active
        if dot is None or profile is None or self.view is None:
            return _("Open a Dot first.")
        status = self._mon.status if self._mon is not None else "unknown"
        if status == "offline":
            return _("No connection to {name}.").format(name=profile.name)
        if status == "token":
            return _("{name} needs a valid owner access token.").format(name=profile.name)
        if self._call_pending is not None and not self.view.voice.in_call:
            return _("A call is already starting.")
        if not self.workspace.voice_ready:
            return _("Voice calls are not set up on {name}.").format(name=profile.name)
        if self._mon is not None and self._mon.state.paused:
            return _("Your Dots are paused.")
        if not voice.available():
            return _("The voice engine is missing on this computer: {parts}.").format(
                parts=", ".join(voice.missing_elements()))
        call = self.workspace.active_call()
        if call is not None and not self.view.voice.in_call:
            return _("Another call is in progress on this server.")
        return None

    def call_current(self) -> None:
        if self.view is not None and self.view.voice.in_call:
            self.end_call()
            return
        dot = self.current_dot()
        if dot is not None:
            self.call_dot(dot.id)

    def call_dot(self, dot_id: str) -> None:
        dot = self.workspace.dot(dot_id)
        reason = self._call_blocker(dot)
        if reason:
            self.toast(reason)
            return
        if voice.snap_microphone_blocked():
            self.toast(_("Allow the microphone first: snap connect dotdeck:audio-record"))
            return
        if not getattr(self, "_mic_checked", False) and self._should_check_microphone():
            # The device scan runs off the main thread; the call continues when it is done.
            def scanned(_devices) -> None:
                self._mic_checked = True
                if self._no_microphone():
                    self._ask_about_microphone(dot_id)
                else:
                    self.call_dot(dot_id)
            voice.list_devices_async("source", scanned)
            return
        self._mic_checked = False
        profile = self.app.profiles.active
        view, profile_id = self.view, self._view_profile

        def begin(then: Callable[[], None]) -> None:
            # From here until the page's call connects the button says "Calling…".
            self._set_call_pending(dot.id)
            then()

        def start(conversation_id: str | None) -> None:
            if self.view is not view or self._view_profile != profile_id:
                self._set_call_pending(None)
                return  # the server changed meanwhile
            self._dot_id, self._place, self._call_dot = dot.id, "dot", dot.id
            self._close_overlay()
            self.sidebar.select_dot(dot.id)
            self._refresh_header()
            index = self.workspace.dot_index(dot.id)

            def done(ok: bool) -> None:
                if not ok:
                    self._call_dot = None
                    self._set_call_pending(None)
                    self.toast(_("The call with {name} could not start.").format(name=dot.name))
                    self._refresh_header()
            view.start_call(conversation_id, index, dot.name, done)

        latest = self.workspace.latest_conversation(dot.id)
        if latest is not None:
            self._confirm_leave_call(dot.id, lambda: begin(lambda: start(latest.id)))
            return

        def created(data, error):
            if error or not isinstance(data, dict) or not data.get("id"):
                self._set_call_pending(None)
                self.toast(_("Could not start a conversation with {name}: {error}").format(
                    name=dot.name, error=error or "?"))
                return
            if self._mon is not None:
                self._mon.poll_now()
            start(str(data["id"]))
        self._confirm_leave_call(dot.id, lambda: begin(lambda: api.for_profile(
            self.app, profile, lambda client: client.create_conversation(
                dot.id, created, title=_("Voice call")))))

    def _set_call_pending(self, dot_id: str | None) -> None:
        """A call with ``dot_id`` is being set up (None: it connected, failed or gave up)."""
        if self._call_pending_timer:
            GLib.source_remove(self._call_pending_timer)
            self._call_pending_timer = 0
        self._call_pending = dot_id
        if dot_id is not None:
            self._call_pending_timer = GLib.timeout_add_seconds(CALL_PENDING_SECONDS,
                                                                self._call_pending_expired)
        self._refresh_header()
        if dot_id is None and self._call_focus_back:
            # Enter on Call: the focus comes back to it (now End, or Call again after a
            # failure) unless it went somewhere else meanwhile.
            self._call_focus_back = False
            if self.get_focus() is None and self.call_button.get_visible() \
                    and self.call_button.get_sensitive():
                self.call_button.grab_focus()

    def _call_pending_expired(self) -> bool:
        self._call_pending_timer = 0
        self._set_call_pending(None)
        return GLib.SOURCE_REMOVE

    def _should_check_microphone(self) -> bool:
        return not self.app.settings.get("microphone-device") and \
            hasattr(voice, "default_device_name") and hasattr(voice, "list_devices_async")

    def _no_microphone(self) -> bool:
        """No microphone chosen and the system has no default one (only speaker monitors)."""
        if self.app.settings.get("microphone-device") or not hasattr(voice, "default_device_name"):
            return False
        try:
            return voice.default_device_name("source") is None and not voice.list_devices("source")
        except Exception:  # a sound server problem must not block the call
            return False

    def _ask_about_microphone(self, dot_id: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("No Microphone Found"),
            body=_("This computer has no default microphone, so the Dot would not hear you. "
                   "Plug in a headset, or choose a microphone in Settings."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("anyway", _("Call Anyway"))
        dialog.add_response("choose", _("Choose Microphone…"))
        dialog.set_response_appearance("choose", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("choose")
        dialog.set_close_response("cancel")

        def chosen(_dialog, response):
            if response == "choose":
                self._mic_checked = False
                self.app.show_preferences("voice")
            elif response == "anyway":
                self._mic_checked = True
                self.call_dot(dot_id)
            else:
                self._mic_checked = False
        dialog.connect("response", chosen)
        dialog.present(self)

    def end_call(self) -> None:
        if self.view is None:
            return
        view = self.view
        if hasattr(view, "end_call"):
            view.end_call(lambda ok: ok or view.voice.close_all())
        else:
            view.voice.close_all()

    def _on_calls_changed(self) -> None:
        in_call = bool(self.view and self.view.voice.in_call)
        muted = in_call and not self.view.voice.mic_enabled
        self.mic_button.set_visible(in_call)
        self._mute_action.set_enabled(in_call)
        self._mute_action.set_state(GLib.Variant("b", muted))
        self.mic_button.set_icon_name("microphone-disabled-symbolic" if muted
                                      else "audio-input-microphone-symbolic")
        if self._push_to_talk():
            self.mic_button.set_tooltip_text(_("Hold to Talk"))
        else:
            self.mic_button.set_tooltip_text(_("Unmute Microphone (Ctrl+Shift+M)") if muted
                                             else _("Mute Microphone (Ctrl+Shift+M)"))
        if muted:
            self.mic_button.add_css_class("destructive-action")
        else:
            self.mic_button.remove_css_class("destructive-action")
        if in_call and not self._call_timer:
            self._call_started = time.monotonic()
            self._call_connected = False
            self._call_id = self._call_thread = None
            self._call_voice_cost = self._call_base_cost = None
            if self._call_dot is None:
                dot = self.workspace.dot_in_call()
                self._call_dot = dot.id if dot else self._dot_id
            self._call_timer = GLib.timeout_add_seconds(1, self._tick_call)
        elif not in_call:
            if self._call_timer:
                GLib.source_remove(self._call_timer)
                self._call_timer = 0
                self._call_ended_costs()
            self._call_dot = None
            self._call_phase = ""
            self._call_connected = False
        if in_call and self._call_pending is not None:
            self._set_call_pending(None)    # the page's call is here: no more "Calling…"
        self._refresh_header()
        self._refresh_workspace()
        self.app.refresh_status()

    def _on_call_phase(self, _voice, phase: str) -> None:
        self._call_phase = phase
        if phase in ("listening", "thinking", "speaking") and not self._call_connected:
            # The time counts from when the Dot can hear, not from the first ring.
            self._call_connected = True
            self._call_started = time.monotonic()
        self._refresh_header()

    def _on_audio_level(self, _voice, role: str, level: float) -> None:
        self.call_panel.set_level(role, level)
        if role == "dot":
            self.sidebar.set_call_level(level)

    # -- the call panel --------------------------------------------------------------------

    def _call_panel_dot(self) -> Dot | None:
        """The Dot of the call in progress, or being set up."""
        in_call = bool(self.view and self.view.voice.in_call)
        dot_id = self._call_dot if in_call else self._call_pending
        return self.workspace.dot(dot_id) if dot_id else None

    def _show_call_panel(self, by_owner: bool = False) -> None:
        """Under the title, without taking the keyboard. On its own only until the owner
        hides it during this call."""
        if self._call_panel_dot() is None or (self._call_panel_hidden and not by_owner):
            return
        self._call_panel_hidden = False
        self._place_call_panel()
        if not self.call_panel.get_visible():
            self.call_panel.popup()

    def _place_call_panel(self) -> None:
        ok, bounds = self.title_box.compute_bounds(self.header)
        if ok:
            area = Gdk.Rectangle()
            area.x, area.y = int(bounds.get_x()), int(bounds.get_y())
            area.width, area.height = int(bounds.get_width()), int(bounds.get_height())
            self.call_panel.set_pointing_to(area)

    def _on_call_panel_closed(self) -> None:
        if self._call_panel_dot() is not None:
            self._call_panel_hidden = True       # the owner hid it: it stays hidden

    def _refresh_call_panel(self) -> None:
        dot = self._call_panel_dot()
        if dot is None:
            if self.call_panel.get_visible():
                self.call_panel.popdown()
            self._call_panel_hidden = False
            self.call_panel.reset()
            return
        in_call = bool(self.view and self.view.voice.in_call)
        phase = getattr(self, "_call_phase", "") if in_call else "connecting"
        connected = self._call_connected
        if phase == "ended":
            status = _("Hanging up…")
        elif not in_call or (phase == "connecting" and not connected):
            status = _("Connecting…")
        elif phase == "connecting":
            status = _("Reconnecting…")
        else:
            words = _(PHASE_WORDS[phase]) if phase in PHASE_WORDS else _("In a call")
            status = f"{words} · {self._call_elapsed()}"
        self.call_panel.update(
            dot_name=dot.name, pet_id=self._pet_map.get(dot.id, pets.DEFAULT.id), phase=phase,
            status=status, connecting=not in_call or phase == "connecting",
            ending=phase == "ended", muted=in_call and not self.view.voice.mic_enabled)
        if self.call_panel.get_visible():
            self._place_call_panel()
        else:
            self._show_call_panel()

    def _on_call_cost(self, _voice, cost: float) -> None:
        self._call_voice_cost = cost
        self._refresh_header()

    def _tick_call(self) -> bool:
        if not (self.view and self.view.voice.in_call):
            self._call_timer = 0
            return GLib.SOURCE_REMOVE
        self._refresh_header()
        return GLib.SOURCE_CONTINUE

    def _call_elapsed(self) -> str:
        seconds = int(time.monotonic() - self._call_started)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    # -- costs -----------------------------------------------------------------------------

    COSTS_INTERVAL = 10       # seconds between asks for the open conversation's cost

    def _refresh_spend(self) -> None:
        spend = getattr(self._mon, "spend", None)
        self.sidebar.set_spend(costs.spend_text(spend), costs.spend_tooltip(spend))

    def _on_page_thread(self, _view, thread_id: str) -> None:
        self._page_thread = thread_id
        self._refresh_costs(force=True)
        self._refresh_header()

    def _refresh_costs(self, force: bool = False) -> None:
        """Ask for the costs of the open conversation and of the call's, at most every
        ``COSTS_INTERVAL`` seconds and only while the window is on screen."""
        profile = self.app.profiles.active
        wanted = [t for t in dict.fromkeys((self._page_thread, self._call_thread)) if t]
        if profile is None or not wanted or not self.get_mapped():
            return
        now = time.monotonic()
        if not force and now - self._costs_asked < self.COSTS_INTERVAL:
            return
        self._costs_asked = now

        def ask(client) -> None:
            for thread in wanted:
                client.thread_cost(thread, lambda data, error, thread=thread:
                                   self._got_thread_cost(profile.id, thread, data, error))
        api.for_profile(self.app, profile, ask)

    def _got_thread_cost(self, profile_id: str, thread: str, data, error) -> None:
        if error is not None or not isinstance(data, dict) or \
                getattr(self.app.profiles.active, "id", None) != profile_id:
            return
        self._thread_costs[thread] = data
        if thread == self._call_thread and self._call_base_cost is None:
            self._call_base_cost = costs.conversation_cost(data)
        self._refresh_header()

    def _call_compute_cost(self) -> float | None:
        """What the call's conversation spent since the call began (its research)."""
        now = costs.conversation_cost(self._thread_costs.get(self._call_thread or ""))
        if now is None or self._call_base_cost is None:
            return None
        return max(0.0, now - self._call_base_cost)

    def _call_ended_costs(self) -> None:
        """The call is over: its voice joins its conversation's cost on the server."""
        call_id, voice_cost = self._call_id, self._call_voice_cost
        profile = self.app.profiles.active
        if self._mon is not None:
            self._mon.refresh_spend()
        if profile is None or not call_id or voice_cost is None:
            return

        def report(client) -> None:
            client.report_call_cost(call_id, voice_cost, lambda data, error: (
                self._got_thread_cost(profile.id, data.get("threadId", ""), data, None)
                if error is None and isinstance(data, dict) else None))
        api.for_profile(self.app, profile, report)

    # -- header ----------------------------------------------------------------------------

    def _refresh_header(self) -> None:
        profile = self.app.profiles.active
        if profile is None:
            return
        native = self._native()
        dot = self.current_dot() if self._place == "dot" else None
        in_call = bool(self.view and self.view.voice.in_call)
        show_dot = native and dot is not None
        narrow = self.split.get_collapsed()
        call_phase = getattr(self, "_call_phase", "")
        pending = self._call_pending is not None and not in_call
        # A narrow window cannot fit everything: New Chat stays in the conversations menu,
        # and at phone width the conversations move into the main menu.
        self.title_avatar.set_visible(show_dot and not narrow)
        self.new_chat_button.set_visible(show_dot and not narrow)
        self.history_button.set_visible(show_dot and not self._very_narrow
                                        and not (narrow and in_call))
        conversations = getattr(self, "_conversations_action", None)
        if conversations is not None:
            conversations.set_enabled(show_dot and self._very_narrow and not in_call)
        self.title_box.set_tooltip_text(None)
        if show_dot:
            self.title_avatar.pet = self._pet_map.get(dot.id, pets.DEFAULT.id)
            calling_here = in_call and self._call_dot == dot.id
            phase = call_phase if calling_here else ""
            self.title_avatar.state = (phase if phase in ("listening", "thinking", "speaking")
                                       else "call") if calling_here else "idle"
            self.title.set_title(dot.name)
            if calling_here:
                compute = self._call_compute_cost()
                self.title.set_subtitle(call_subtitle(
                    phase, self._call_elapsed(), connected=self._call_connected,
                    compact=self._very_narrow,
                    cost=costs.call_cost(self._call_voice_cost, compute)))
                tooltip = [_(PHASE_WORDS[phase])] if phase in PHASE_WORDS else []
                spent = costs.call_tooltip(self._call_voice_cost, compute)
                self.title_box.set_tooltip_text("\n\n".join(tooltip + ([spent] if spent
                                                                          else [])) or None)
            elif pending and self._call_pending == dot.id:
                self.title.set_subtitle(_("Calling…"))
            else:
                cost = self._thread_costs.get(self._page_thread) if self._page_thread else None
                spent = costs.conversation_text(cost)
                self.title.set_subtitle(" · ".join(p for p in (spent, dot.job) if p))
                if spent:
                    self.title_box.set_tooltip_text(costs.conversation_tooltip(cost))
        elif native and self._place in ("activity", "memories", "space"):
            space = self.workspace.space(self._space_id) if self._place == "space" else None
            self.title.set_title({"activity": _("Activity"), "memories": _("Memories")}.get(
                self._place, space.name if space else _("Spaces")))
            self.title.set_subtitle(profile.name)
        else:
            # The name the owner gave the server, as in the switcher and the window title.
            self.title.set_title(profile.name)
            self.title.set_subtitle(self.app.status_line(profile.id))

        # The call button: "Call" for the open Dot, "Calling…" while the call is being set
        # up, "End" during a call (its time is in the title). After hanging up, OpenDots
        # keeps the line for ~40 s while it saves the call's summary.
        saving = in_call and call_phase == "ended"
        self.call_stack.set_visible_child_name("calling" if pending else "call")
        if saving:
            self.call_button.set_visible(True)
            self.call_content.set_icon_name("call-stop-symbolic")
            self.call_content.set_label(_("Saving…"))
            self.call_button.remove_css_class("destructive-action")
            self.call_button.remove_css_class("suggested-action")
            self.call_button.set_sensitive(False)
            self.call_button.set_tooltip_text(_("Saving the call's summary"))
        elif in_call:
            self.call_button.set_visible(True)
            self.call_content.set_icon_name("call-stop-symbolic")
            # TRANSLATORS: the button that hangs up a voice call.
            self.call_content.set_label(_("End"))
            self.call_button.remove_css_class("suggested-action")
            self.call_button.add_css_class("destructive-action")
            self.call_button.set_tooltip_text(_("End the Call (Ctrl+Shift+L)"))
            self.call_button.set_sensitive(True)
        elif pending:
            self.call_button.set_visible(show_dot and self.workspace.voice_ready)
            self.call_button.remove_css_class("destructive-action")
            self.call_button.add_css_class("suggested-action")
            if self.call_button.has_focus():   # dimmed, it drops the keyboard focus
                self._call_focus_back = True
            self.call_button.set_sensitive(False)
            self.call_button.set_tooltip_text(_("Calling…"))
        else:
            reason = self._call_blocker(dot) if show_dot else None
            self.call_button.set_visible(show_dot and self.workspace.voice_ready)
            self.call_content.set_icon_name("call-start-symbolic")
            self.call_content.set_label(_("Call"))
            self.call_button.remove_css_class("destructive-action")
            self.call_button.add_css_class("suggested-action")
            self.call_button.set_sensitive(reason is None)
            self.call_button.set_tooltip_text(reason or _("Call {name} (Ctrl+Shift+L)").format(
                name=dot.name if dot else ""))
        self.call_pending_label.set_visible(not narrow)
        self._refresh_call_panel()
        if narrow:  # icon only; the words stay in the tooltip
            self.call_content.set_label("")
            self.call_button.remove_css_class("call-button")
            self.call_button.add_css_class("circular")
        else:
            self.call_button.add_css_class("call-button")
            self.call_button.remove_css_class("circular")

    # -- status ----------------------------------------------------------------------------

    def refresh_status(self) -> None:
        profile = self.app.profiles.active
        if profile is None:
            return
        mon = self.app.monitors.get(profile.id)
        if mon is not self._mon:
            self._watch(profile)
        status = mon.status if mon else "unknown"
        for label in self._server_labels:
            label.set_label(profile.name)
        for dot in self._status_dots:
            for name in ("success", "warning", "error", "dim-label"):
                dot.remove_css_class(name)
            dot.add_css_class({"online": "success", "offline": "error", "token": "warning",
                               "local-only": "warning"}.get(status, "dim-label"))
        self.set_title(f"{profile.name} – {config.APP_NAME}")
        self._refresh_header()
        if self.view is not None and hasattr(self.view, "set_offline"):
            # The banner below says it in the owner's language: the page's own error bar
            # ("Load failed") would repeat it in English.
            self.view.set_offline(status == "offline")

        active = mon.active_tasks if mon else 0
        self.tasks_button.set_visible(active > 0 and not self._native())
        self._tasks_label.set_label(ngettext("{n} task", "{n} tasks", active).format(n=active))
        self.sidebar.set_counts(active, len(mon.state.memories) if mon else 0)
        self._pause_action.set_state(GLib.Variant("b", bool(mon and mon.state.paused)))

        if status == "token":
            self._set_banner(_("{name} needs a valid owner access token.").format(
                name=profile.name), _("Update Token"), lambda: self.app.edit_server(profile.id))
        elif status == "local-only":
            self._set_banner(server.describe(server.LOCAL_ONLY), _("Edit Server"),
                             lambda: self.app.edit_server(profile.id))
        elif status == "offline" and (self.view is None or not self.view.failed):
            self._set_banner(_("No connection to {name}. Trying again…").format(
                name=profile.name), _("Retry Now"), self._retry)
        elif mon and mon.state.paused:
            self._set_banner(_("Your Dots are paused."), _("Resume"),
                             lambda: self._set_paused(False))
        elif mon and mon.updates and int(mon.updates.get("behind") or 0) \
                and self._seen_updates != (mon.updates.get("upstream") or {}).get("short"):
            behind = int(mon.updates["behind"])
            upstream = (mon.updates.get("upstream") or {}).get("short")

            def show_updates():
                self._seen_updates = upstream
                self.app.edit_server(profile.id)
                self.refresh_status()

            self._set_banner(ngettext("{n} change in the official OpenDots",
                                      "{n} changes in the official OpenDots", behind).format(
                                          n=behind), _("See Updates"), show_updates)
        elif server.is_cleartext_remote(profile.url):
            self._set_banner(_("The connection to {name} is not encrypted.").format(
                name=profile.name), _("Edit Server"), lambda: self.app.edit_server(profile.id))
        else:
            self._set_banner(None)

    def _retry(self) -> None:
        mon = self.app.monitors.get(self._view_profile or "")
        if mon:
            mon.poll_now()
        if self.view:
            self.view.reload()

    def _pause_all(self) -> None:
        mon = self._mon
        if mon is None:
            return
        if mon.state.paused:
            self._set_paused(False)
            return
        dialog = Adw.AlertDialog(
            heading=_("Pause All Dots?"),
            body=_("Work in progress stops and scheduled tasks wait until you resume."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("pause", _("Pause"))
        dialog.set_response_appearance("pause", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "pause" and self._set_paused(True))
        dialog.present(self)

    def _set_paused(self, paused: bool) -> None:
        profile = self.app.profiles.active
        if profile is None:
            return

        def done(_data, error):
            if error:
                self.toast(_("Could not change it: {error}").format(error=error))
            if self._mon is not None:
                self._mon.poll_now()
        api.for_profile(self.app, profile,
                        lambda client: client.update_settings(done, paused=paused))

    def _set_banner(self, text: str | None, button: str = "",
                    action: Callable[[], None] | None = None) -> None:
        self._banner_action = action
        if text is None:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(text)
        self.banner.set_button_label(button or None)
        self.banner.set_revealed(True)

    def _on_banner_clicked(self, _banner) -> None:
        if self._banner_action:
            self._banner_action()

    # -- actions on the page ---------------------------------------------------------------

    def page_action(self, action: str, argument: str = "") -> None:
        if action == "activity":
            self.open_activity()
        elif self.view is not None:
            self.view.run_page_action(action, argument)

    def talk_to_chief(self) -> None:
        profile = self.app.profiles.active
        mon = self.app.monitors.get(profile.id) if profile else None
        name = monitor.chief_dot(profile, mon.dots if mon else []) if profile else None
        self.present()
        dot = self.workspace.dot_named(name)
        if dot is not None and self._native():
            self.open_dot(dot.id)
        elif name and self.view is not None:
            self.view.run_page_action("dot", name, lambda ok: ok or self.toast(
                _("Could not find {name} on the page.").format(name=name)))

    def toggle_mute(self) -> None:
        if self.view is not None:
            self.view.toggle_mute()

    def _push_to_talk(self) -> bool:
        return bool(self.view and getattr(self.view.voice, "push_to_talk", False))

    def _hold_to_talk(self, gesture: Gtk.GestureClick, pressed: bool) -> None:
        if not self._push_to_talk():
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.set_mic(pressed)

    def set_mic(self, enabled: bool) -> None:
        if self.view is not None and self.view.voice.in_call:
            self.view.voice.set_mic_enabled(enabled)
            self._on_calls_changed()

    def _zoom(self, direction: int) -> None:
        current = self.app.settings.get("zoom")
        if direction == 0:
            value = 1.0
        else:
            steps = [z for z in ZOOM_STEPS if (z > current + 0.01 if direction > 0
                                               else z < current - 0.01)]
            value = (min(steps) if direction > 0 else max(steps)) if steps else current
        self.app.settings.set("zoom", value)
        if self.view is not None:
            self.view.set_zoom(value)
        self.toast(_("Zoom {percent}%").format(percent=round(value * 100)))

    # -- window behaviour ------------------------------------------------------------------

    def _on_fullscreen(self, *_args) -> None:
        fullscreen = self.is_fullscreen()
        self.toolbar.set_reveal_top_bars(not fullscreen)
        if fullscreen:
            self.toast(_("Press F11 to leave fullscreen"))

    def save_geometry(self) -> None:
        settings = self.app.settings
        settings.set("window-maximized", self.is_maximized())
        if not self.is_maximized() and not self.is_fullscreen():
            width, height = self.get_default_size()
            settings.set("window-width", width)
            settings.set("window-height", height)

    def _on_close_request(self, _window) -> bool:
        self.save_geometry()
        if self.app.settings.get("run-in-background") and self.app.profiles.active:
            self.set_visible(False)
            self.app.went_to_background()
            return True
        return False

    # -- ServerView host -------------------------------------------------------------------

    def toast(self, text: str, button: str | None = None,
              action: Callable[[], None] | None = None) -> None:
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        if button and action:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda _t: action())
        self.toasts.add_toast(toast)

    def open_external(self, uri: str) -> None:
        Gtk.UriLauncher(uri=uri).launch(self, None, None)

    def web_notification(self, profile: Profile, title: str, body: str) -> None:
        self.app.web_notification(profile, title, body)

    def token_from_page(self, profile: Profile, token: str) -> None:
        self.app.token_from_page(profile, token)

    def edit_profile(self, profile: Profile) -> None:
        self.app.edit_server(profile.id)

    def echo_cancellation(self) -> bool:
        return bool(self.app.settings.get("echo-cancellation"))

    def voice_options(self) -> dict:
        settings = self.app.settings
        return {key: settings.get(key) for key in (
            "microphone-device", "speaker-device", "echo-cancellation", "noise-suppression",
            "gain-control", "mic-mode", "join-muted")}

    def ask_microphone(self, profile: Profile, callback: Callable[[bool], None]) -> None:
        current = self.app.profiles.get(profile.id) or profile
        if current.microphone == MIC_ALLOW:
            callback(True)
            return
        if current.microphone == MIC_DENY:
            self.toast(_("The microphone is blocked for {name}.").format(name=current.name),
                       _("Settings"), lambda: self.app.edit_server(current.id))
            callback(False)
            return
        self.present()
        dialog = Adw.AlertDialog(
            heading=_("Use the Microphone?"),
            body=_("{name} wants to start a voice call with a Dot. Dotdeck only listens while "
                   "the call is open.").format(name=current.name))
        remember = Gtk.CheckButton(label=_("Remember for this server"), active=True,
                                   halign=Gtk.Align.CENTER)
        dialog.set_extra_child(remember)
        dialog.add_response("deny", _("Don't Allow"))
        dialog.add_response("allow", _("Allow"))
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("allow")
        dialog.set_close_response("deny")

        def chosen(_dialog, response):
            allowed = response == "allow"
            if remember.get_active():
                saved = self.app.profiles.get(current.id)
                if saved:
                    saved.microphone = MIC_ALLOW if allowed else MIC_DENY
                    self.app.profiles.update(saved)
            callback(allowed)

        dialog.connect("response", chosen)
        dialog.present(self)
