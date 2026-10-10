"""SERVER › Privacy: what every Dot may do, memories, this computer and telemetry."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk

from ...core import secrets
from ...i18n import _, ngettext
from .common import SettingsPage, confirm, escape, status_icon

# How long a change we made wins over an older server state (until the next poll).
PENDING_SECONDS = 15


class PrivacyPage(SettingsPage):
    name = "privacy"
    title = _("Privacy")
    icon = "preferences-system-privacy-symbolic"

    def build(self) -> None:
        self._syncing = False
        self._asking: set[str] = set()
        self._pending: dict[str, tuple[bool, float]] = {}

        top = self.group()
        self.selector = self.add(top, self.win.server_selector(), ("server",))
        top.set_visible(self.selector.get_visible())
        self.selector.connect("notify::visible",
                              lambda row, _p: top.set_visible(row.get_visible()))

        self.server = self.group(_("Every Dot"))
        self.research = Adw.SwitchRow(title=_("Web Research"),
                                      subtitle=_("Dots can search the web and read pages"))
        self.memory = Adw.SwitchRow(title=_("Memories"),
                                    subtitle=_("Dots can save what they learn about you and use "
                                               "it later"))
        self.paused = Adw.SwitchRow(title=_("Pause All Dots"),
                                    subtitle=_("Stops every Dot and scheduled task until you "
                                               "resume them"))
        for row, key, keywords in ((self.research, "researchAllowed", ("search", "web",
                                                                       "browse", "internet")),
                                   (self.memory, "memoryAllowed", ("remember", "memory",
                                                                   "learn")),
                                   (self.paused, "paused", ("pause", "stop", "resume",
                                                            "halt"))):
            row.connect("notify::active", self._on_switch, key)
            self.add(self.server, row, keywords)

        memories = self.group()
        self.memories_row = Adw.ActionRow(title=_("Saved Memories"))
        self.manage = Gtk.Button(label=_("Manage Memories…"), valign=Gtk.Align.CENTER)
        self.manage.add_css_class("flat")
        self.manage.connect("clicked", lambda _b: self._manage_memories())
        self.memories_row.add_suffix(self.manage)
        self.add(memories, self.memories_row, ("memories", "forget", "delete", "edit"))

        computer = self.group(_("This Computer"))
        available = secrets.keyring_available()
        keyring = Adw.ActionRow(
            title=_("Access Tokens"),
            subtitle=_("Kept in the system keyring, encrypted with your login") if available
            else _("No keyring available: tokens are kept only until Dotdeck closes"))
        keyring.add_prefix(status_icon("channel-secure-symbolic" if available
                                       else "dialog-warning-symbolic",
                                       "success" if available else "warning"))
        self.add(computer, keyring, ("keyring", "password", "token", "secret", "security"))
        self.clear = Adw.ButtonRow(title=_("Clear Website Data…"),
                                   start_icon_name="edit-clear-all-symbolic")
        self.clear.connect("activated", lambda _r: self._clear())
        self.add(computer, self.clear, ("cookies", "cache", "storage", "website data"))

        telemetry = self.group(_("Telemetry"))
        row = Adw.ActionRow(
            title=_("Usage Data From the Server"),
            subtitle=_("OpenDots sends anonymous usage data to CopilotKit by default. To turn "
                       "it off, add DO_NOT_TRACK=1 to the server's .env file and restart it. "
                       "Dotdeck itself collects nothing."))
        row.set_subtitle_lines(0)
        row.add_prefix(status_icon("dialog-information-symbolic"))
        self.add(telemetry, row, ("telemetry", "tracking", "analytics", "do_not_track"))
        self.refresh()

    # -- state -----------------------------------------------------------------------------

    def _value(self, key: str, server_value: bool) -> bool:
        pending = self._pending.get(key)
        if pending is None:
            return server_value
        value, since = pending
        if value == server_value or time.monotonic() - since > PENDING_SECONDS:
            self._pending.pop(key, None)
            return server_value
        return value

    def refresh(self) -> None:
        profile = self.win.profile
        mon = self.win.monitor
        online = mon is not None and mon.status == "online"
        name = profile.name if profile else ""
        self.server.set_title(_("Every Dot on {name}").format(name=escape(name)) if profile
                              else _("Every Dot"))
        self.server.set_description(
            _("Changing these stops the work in progress on the server.") if online else
            _("Available while the server is online.") if profile is not None else
            _("Add a server to change what its Dots may do."))
        self.server.set_sensitive(online)
        state = mon.state if mon is not None else None
        self._syncing = True
        try:
            if state is not None:
                for row, key, value in ((self.research, "researchAllowed",
                                         state.research_allowed),
                                        (self.memory, "memoryAllowed", state.memory_allowed),
                                        (self.paused, "paused", state.paused)):
                    if key not in self._asking:
                        row.set_active(self._value(key, value))
        finally:
            self._syncing = False
        count = len(state.memories) if state is not None and online else 0
        if profile is None:
            text = _("No server yet")
        elif not online:
            text = _("Unavailable while the server is offline")
        elif count:
            text = ngettext("{n} memory that every Dot can use",
                            "{n} memories that every Dot can use", count).format(n=count)
        else:
            text = _("No memories saved yet")
        view = self.win.view_for(profile.id if profile else None)
        can_open = view is not None and hasattr(view, "open_memories")
        if online and not can_open and profile is not None:
            text += " · " + _("open {name} in Dotdeck to manage them").format(name=profile.name)
        self.memories_row.set_subtitle(escape(text))
        self.manage.set_visible(can_open)
        self.clear.set_sensitive(profile is not None)

    # -- server-wide switches --------------------------------------------------------------

    def _on_switch(self, row: Adw.SwitchRow, _pspec, key: str) -> None:
        if self._syncing or key in self._asking:
            return
        # Ask outside the property notification: changing a switch row back from inside
        # its own notify::active handler makes it flip forever.
        self._asking.add(key)
        GLib.idle_add(lambda: (self._ask(row, key, row.get_active()), False)[1])

    def _ask(self, row: Adw.SwitchRow, key: str, wanted: bool) -> None:
        profile = self.win.profile
        if profile is None:
            self._asking.discard(key)
            self._revert(row, not wanted)
            return
        server = profile.name
        texts = {
            ("researchAllowed", False): (_("Turn Off Web Research?"),
                                         _("No Dot on {name} will search the web. The work in "
                                           "progress stops now."), _("Turn Off")),
            ("researchAllowed", True): (_("Turn On Web Research?"),
                                        _("Dots on {name} can search the web again. The work in "
                                          "progress stops now."), _("Turn On")),
            ("memoryAllowed", False): (_("Turn Off Memories?"),
                                       _("Dots on {name} stop saving and using memories. The "
                                         "work in progress stops now."), _("Turn Off")),
            ("memoryAllowed", True): (_("Turn On Memories?"),
                                      _("Dots on {name} can save and use memories again. The "
                                        "work in progress stops now."), _("Turn On")),
            ("paused", True): (_("Pause All Dots?"),
                               _("Every Dot on {name} stops, scheduled tasks too, until you "
                                 "resume them. A call in progress ends."), _("Pause")),
        }
        text = texts.get((key, wanted))
        if text is None:          # resuming interrupts nothing
            self._apply(row, key, wanted)
            return
        heading, body, action = text

        def answered(ok: bool) -> None:
            if ok:
                self._apply(row, key, wanted)
            else:
                self._asking.discard(key)
                self._revert(row, not wanted)

        confirm(self.win, heading, body.format(name=server), action, answered,
                destructive=not wanted or key == "paused")

    def _revert(self, row: Adw.SwitchRow, value: bool) -> None:
        self._syncing = True
        try:
            row.set_active(value)
        finally:
            self._syncing = False

    def _apply(self, row: Adw.SwitchRow, key: str, value: bool) -> None:
        profile = self.win.profile
        if profile is None:
            self._asking.discard(key)
            return
        row.set_sensitive(False)

        def done(data, error) -> None:
            self._asking.discard(key)
            row.set_sensitive(True)
            if error is not None:
                self._revert(row, not value)
                self.win.toast(_("Could not change it: {error}").format(error=error))
                return
            self._pending[key] = (value, time.monotonic())
            self._revert(row, value)
            mon = self.app.monitors.get(profile.id)
            if mon is not None:
                mon.poll_now()
            self.win.toast({
                ("researchAllowed", True): _("Web research is on"),
                ("researchAllowed", False): _("Web research is off"),
                ("memoryAllowed", True): _("Memories are on"),
                ("memoryAllowed", False): _("Memories are off"),
                ("paused", True): _("All Dots are paused"),
                ("paused", False): _("The Dots are back to work"),
            }[(key, value)])

        self.win.with_api(lambda client: client.update_settings(done, **{key: value}), profile)

    # -- other actions ---------------------------------------------------------------------

    def _manage_memories(self) -> None:
        profile = self.win.profile
        view = self.win.view_for(profile.id if profile else None)
        if view is None or not hasattr(view, "open_memories"):
            return
        # The main window's own action also selects Memories in its sidebar.
        window = getattr(self.app, "window", None)
        opener = getattr(window, "open_memories", None)
        if callable(opener):
            opener()
        else:
            view.open_memories()
        self.win.close()

    def _clear(self) -> None:
        profile = self.win.profile
        if profile is not None:
            self.win.pages["servers"].confirm_clear(profile)
