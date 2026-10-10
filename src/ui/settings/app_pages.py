"""The APP section: General, Appearance, Notifications and Shortcuts."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from ... import config
from ...core import store
from ...i18n import _
from .common import SettingsPage, call_with_parent, suffix_label

# The same steps as the main window's zoom (src/ui/window.py), without importing WebKit.
ZOOM_STEPS = (0.5, 0.67, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)

# Languages by their own name (po/LINGUAS has the translations).
LANGUAGES = {"pt_BR": "Português (Brasil)", "pt": "Português", "es": "Español",
             "en": "English"}

# The main accelerators on the Shortcuts page: (action, title). Rows whose action has no
# accelerator are left out; the full list is in the keyboard shortcuts dialog.
IN_APP_SHORTCUTS = (
    ("win.search", "Search Dots and Spaces"),
    ("win.dot-1", "Open Dot 1–9"),
    ("win.new-chat", "New Chat"),
    ("win.new-dot", "New Dot"),
    ("win.talk-to-chief", "Talk to the Chief"),
    ("win.call", "Call or Hang Up"),
    ("win.toggle-mute", "Mute or Unmute the Microphone"),
    ("win.toggle-sidebar", "Show or Hide the Sidebar"),
    ("app.preferences", "Settings"),
    ("app.shortcuts", "Keyboard Shortcuts"),
)
# Used when the application cannot tell (tests, screenshots).
DEFAULT_ACCELS = {
    "win.search": "<Control>k", "win.dot-1": "<Control>1", "win.new-chat": "<Control>n",
    "win.new-dot": "<Control><Shift>n", "win.talk-to-chief": "<Control><Shift>c",
    "win.call": "<Control><Shift>l", "win.toggle-mute": "<Control><Shift>m",
    "win.toggle-sidebar": "<Control>b", "app.preferences": "<Control>comma",
    "app.shortcuts": "<Control>question",
}


def _shortcut_titles() -> dict[str, str]:
    # Spelled out so xgettext sees every title (the same ones as the shortcuts dialog).
    return {
        "Search Dots and Spaces": _("Search Dots and Spaces"),
        "Open Dot 1–9": _("Open Dot 1–9"),
        "New Chat": _("New Chat"), "New Dot": _("New Dot"),
        "Talk to the Chief": _("Talk to the Chief"), "Call or Hang Up": _("Call or Hang Up"),
        "Mute or Unmute the Microphone": _("Mute or Unmute the Microphone"),
        "Show or Hide the Sidebar": _("Show or Hide the Sidebar"), "Settings": _("Settings"),
        "Keyboard Shortcuts": _("Keyboard Shortcuts"),
    }


def current_language(translation=None) -> str:
    """The language Dotdeck shows now: that of the translation in use, else English.

    A system language without a Dotdeck translation (pt_PT, de…) shows English, so the
    name comes from the loaded catalog, not from the system's list of languages.
    """
    if translation is None:
        translation = getattr(_, "__self__", None)
    info = getattr(translation, "info", None)
    try:
        code = str((info() if callable(info) else {}).get("language") or "")
    except Exception:  # a catalog without a readable header
        code = ""
    code = code.replace("-", "_").split(".")[0]
    if not code:
        return LANGUAGES["en"]
    return LANGUAGES.get(code) or LANGUAGES.get(code.split("_")[0]) or code


class GeneralPage(SettingsPage):
    name = "general"
    title = _("General")
    icon = "preferences-system-symbolic"

    def build(self) -> None:
        startup = self.group(_("Startup"))
        self.switch(startup, "autostart", _("Start When You Log In"),
                    _("Dotdeck opens hidden in the background, so news from your Dots keeps "
                      "arriving"), ("autostart", "login", "boot", "session", "startup"))
        self.switch(startup, "run-in-background", _("Run in Background"),
                    _("Closing the window keeps Dotdeck running to deliver notifications"),
                    ("close", "quit", "minimize", "hide"))
        tray = self.switch(startup, "tray-icon", _("Tray Icon"), self._tray_subtitle(),
                           ("tray", "indicator", "status icon", "panel", "top bar"))
        self._tray_row = tray

        window = self.group(_("Window"))
        self.size_row = Adw.ActionRow(title=_("Window Size"))
        reset = Gtk.Button(label=_("Reset"), valign=Gtk.Align.CENTER,
                           tooltip_text=_("Go back to the default window size"))
        reset.add_css_class("flat")
        reset.connect("clicked", lambda _b: self._reset_size())
        self.size_row.add_suffix(reset)
        self.add(window, self.size_row, ("size", "geometry", "maximized", "restore"))
        for key in ("window-width", "window-height", "window-maximized"):
            self.win.bind(key, lambda _value: self._update_size())
        self._update_size()

        language = self.group(_("Language"))
        row = Adw.ActionRow(
            title=_("Language"),
            subtitle=_("Dotdeck follows the system language. Change it in the system "
                       "settings, under Region and Language."))
        row.add_suffix(suffix_label(current_language()))
        self.add(language, row, ("language", "translation", "locale", "idioma", "português",
                                 "español"))

    def _tray_subtitle(self) -> str:
        if getattr(self.app, "tray_host_present", False):
            return _("Status and quick actions in the top bar")
        return _("Your desktop shows no tray. On GNOME, Dotdeck appears under Background Apps "
                 "in the system menu instead.")

    def _update_size(self) -> None:
        settings = self.app.settings
        if settings.get("window-maximized"):
            text = _("Dotdeck remembers its size when it closes. Now: maximized")
        else:
            text = _("Dotdeck remembers its size when it closes. Now: {width} × {height}").format(
                width=settings.get("window-width"), height=settings.get("window-height"))
        self.size_row.set_subtitle(text)

    def _reset_size(self) -> None:
        defaults = store.Settings.DEFAULTS
        for key in ("window-maximized", "window-width", "window-height"):
            self.app.settings.set(key, defaults[key])
        window = getattr(self.app, "window", None)
        if isinstance(window, Gtk.Window):
            window.unmaximize()
            window.set_default_size(defaults["window-width"], defaults["window-height"])
        self.win.toast(_("Window size reset"))

    def refresh(self) -> None:
        self._tray_row.set_subtitle(self._tray_subtitle())


class AppearancePage(SettingsPage):
    name = "appearance"
    title = _("Appearance")
    icon = "preferences-desktop-appearance-symbolic"

    def build(self) -> None:
        style = self.group()
        self.choice(style, "color-scheme", _("Style"),
                    [("system", _("System")), ("light", _("Light")), ("dark", _("Dark"))],
                    _("Follow the system, or always use light or dark colors"),
                    ("theme", "dark mode", "light mode", "color scheme", "night"))
        self.choice(style, "density", _("Density"),
                    [("comfortable", _("Comfortable")), ("compact", _("Compact"))],
                    _("Compact fits more Dots in the sidebar"),
                    ("compact", "spacing", "size", "rows"))
        self.combo(style, "zoom", _("Page Zoom"),
                   [(step, f"{round(step * 100)}%") for step in ZOOM_STEPS],
                   _("Text size of the server's page. Also Ctrl+plus and Ctrl+minus."),
                   ("zoom", "text size", "font", "scale", "bigger", "smaller"))

        sidebar = self.group(_("Sidebar"))
        self.switch(sidebar, "native-sidebar", _("Native Sidebar"),
                    _("Dotdeck lists your Dots in its own sidebar and hides the one in the "
                      "page. If a server's page is not recognized, its own sidebar comes "
                      "back by itself."), ("focus mode", "web sidebar", "layout", "list"))
        self.switch(sidebar, "show-jobs", _("Show What Each Dot Does"),
                    _("A short line under each name, from the first line of its instructions"),
                    ("job", "role", "description", "subtitle"))

        pets = self.group(_("Pets"), _("Every Dot has its own plush pet. Choose them in Dots."))
        from ..pet import PetAvatar
        self.preview = PetAvatar("sky", size=44, state="working")
        self.preview.update_property([Gtk.AccessibleProperty.LABEL], [_("Animation preview")])
        row = self.choice(pets, "pet-animations", _("Animations"),
                          [("full", _("Full")), ("subtle", _("Subtle")), ("off", _("Off"))],
                          _("How much the pets move while their Dots work"),
                          ("animation", "motion", "pet", "mascot", "reduce"))
        row.add_prefix(self.preview)
        self.win.bind("pet-animations", lambda _value: self._restart_preview())
        self.switch(pets, "pets-in-page", _("Pets in the Page"),
                    _("The page shows each Dot's pet instead of the OpenDots mascots"),
                    ("mascot", "web", "avatar", "picture"))

    def _restart_preview(self) -> None:
        from .. import pet
        pet.set_animations(self.app.settings.get("pet-animations"))
        # The avatar picks the new setting up when its state is set again.
        self.preview.set_property("state", "idle")
        self.preview.set_property("state", "working")


class NotificationsPage(SettingsPage):
    name = "notifications"
    title = _("Notifications")
    icon = "preferences-system-notifications-symbolic"

    EVENTS = (
        ("notify-task-completed", ("done", "finished", "complete")),
        ("notify-task-failed", ("error", "failed", "problem")),
        ("notify-task-started", ("schedule", "recurring", "start")),
        ("notify-server", ("offline", "online", "down", "connection")),
        ("notify-updates", ("update", "news", "upgrade")),
    )

    def build(self) -> None:
        master = self.group()
        self.switch(master, "notifications", _("Desktop Notifications"),
                    _("Shown while the Dotdeck window is not in focus"),
                    ("notify", "alert", "banner", "popup"))

        titles = {
            "notify-task-completed": (_("A Dot Finishes a Task"),
                                      _("With the task's text, so you know which one")),
            "notify-task-failed": (_("A Task Fails"),
                                   _("With the error the server reported")),
            "notify-task-started": (_("A Scheduled Task Starts"),
                                    _("Recurring tasks, each time they run")),
            "notify-server": (_("A Server Goes Offline or Comes Back"),
                              _("When Dotdeck loses or finds the connection")),
            "notify-updates": (_("OpenDots Updates Are Available"),
                               _("News in the official project for your server")),
        }
        self.events = self.group(_("Notify Me When"))
        for key, keywords in self.EVENTS:
            title, subtitle = titles[key]
            self.switch(self.events, key, title, subtitle, keywords)

        content = self.group(_("Content"))
        self.choice(content, "notification-content", _("Notification Text"),
                    [("full", _("Full")), ("private", _("Private"))],
                    _("Private shows only “New activity”, without names or text, for shared "
                      "screens"), ("private", "hide", "lock screen", "privacy", "preview"))
        test = Adw.ButtonRow(title=_("Send Test Notification"),
                             start_icon_name="mail-unread-symbolic")
        test.connect("activated", lambda _r: self.send_test())
        self.add(content, test, ("test", "try", "check"))
        self.win.bind("notifications", lambda _value: self._sync_sensitive())
        self._sync_sensitive()

    def _sync_sensitive(self) -> None:
        self.events.set_sensitive(bool(self.app.settings.get("notifications")))

    def send_test(self) -> None:
        private = self.app.settings.get("notification-content") == "private"
        notification = Gio.Notification.new(_("New activity") if private
                                            else _("Notifications from Dotdeck work"))
        notification.set_body(_("Open Dotdeck to see it.") if private else
                              _("This is how news from your Dots will look."))
        send = getattr(self.app, "send_notification", None)
        if callable(send):
            send("settings-test", notification)
        if self.app.settings.get("notifications"):
            self.win.toast(_("Test notification sent"))
        else:
            self.win.toast(_("Test notification sent. Desktop notifications are off, so "
                             "Dotdeck sends no others."))


class ShortcutsPage(SettingsPage):
    name = "shortcuts"
    title = _("Shortcuts")
    icon = "preferences-desktop-keyboard-shortcuts-symbolic"

    def build(self) -> None:
        self._triggers: dict[str, Gtk.Label] = {}
        self._portal = getattr(self.app, "shortcuts", None)
        self._portal_handler = 0
        keys = self.group(_("Global Shortcuts"))
        if self._portal is not None and getattr(self._portal, "available", False):
            keys.set_description(_("They work even while another app is in focus."))
            self.switch(keys, "global-shortcut", _("Use Global Shortcuts"),
                        _("Show Dotdeck, mute and talk in calls from anywhere"),
                        ("global", "hotkey", "system"))
            titles = getattr(self.app, "SHORTCUT_TITLES", {})
            for shortcut_id, title in titles.items():
                row = Adw.ActionRow(title=title)
                if shortcut_id == "push-to-talk":
                    row.set_subtitle(_("Hold it while the microphone mode is Push to Talk"))
                label = suffix_label()
                row.add_suffix(label)
                self._triggers[shortcut_id] = label
                self.add(keys, row, ("global", shortcut_id))
            self.change = Adw.ButtonRow(title=_("Change in System Settings…"),
                                        end_icon_name="adw-external-link-symbolic")
            self.change.connect("activated", lambda _r: self.app.configure_shortcuts())
            self.add(keys, self.change, ("change", "configure", "edit", "bind"))
            # The portal says later (and again after a change in the system settings)
            # which keys were bound and whether they can be changed.
            if hasattr(self._portal, "connect"):
                try:
                    self._portal_handler = self._portal.connect(
                        "changed", lambda *_: self.refresh_triggers())
                except TypeError:          # an older portal object without the signal
                    self._portal_handler = 0
            self.win.bind("global-shortcut", lambda _value: GLib.idle_add(
                lambda: (self.refresh_triggers(), False)[1]))
            self.refresh_triggers()
        else:
            command = f"flatpak run {config.APP_ID} --toggle" if config.IN_FLATPAK else \
                "dotdeck --toggle"
            keys.set_description(_("Your desktop does not offer global shortcuts to apps. Add a "
                                   "custom shortcut in the system keyboard settings with this "
                                   "command:"))
            row = Adw.ActionRow(title=command, subtitle_selectable=True, title_selectable=True)
            row.add_css_class("monospace")
            self.add(keys, row, ("global", "custom shortcut", "command"))

        inside = self.group(_("In Dotdeck"),
                            _("The main ones. All Keyboard Shortcuts lists every one."))
        titles = _shortcut_titles()
        for action, english in IN_APP_SHORTCUTS:
            accel = self._accel(action)
            if not accel:
                continue
            row = Adw.ActionRow(title=titles.get(english, english))
            label = Adw.ShortcutLabel(accelerator=accel, valign=Gtk.Align.CENTER)
            row.add_suffix(label)
            self.add(inside, row, ("key", "keyboard", "accelerator"))
        row = Adw.ActionRow(title=_("Search Settings"))
        row.add_suffix(Adw.ShortcutLabel(accelerator="<Control>f", valign=Gtk.Align.CENTER))
        self.add(inside, row, ("find", "key"))
        everything = Adw.ButtonRow(
            title=_("All Keyboard Shortcuts"),
            start_icon_name="preferences-desktop-keyboard-shortcuts-symbolic")
        everything.connect("activated", lambda _r: self._show_all())
        self.add(inside, everything, ("cheat sheet", "list", "help"))

    def refresh_triggers(self) -> None:
        """Show the keys the desktop bound, and the change button once it can be used."""
        if self.win._closing or self._portal is None:
            return
        triggers = getattr(self._portal, "triggers", {}) or {}
        enabled = bool(self.app.settings.get("global-shortcut"))
        for shortcut_id, label in self._triggers.items():
            trigger = triggers.get(shortcut_id) if enabled else None
            label.set_label(trigger or (_("Not set") if enabled else _("Off")))
        self.change.set_visible(bool(getattr(self._portal, "can_configure", False)))

    def destroy(self) -> None:
        if self._portal_handler:
            self._portal.disconnect(self._portal_handler)
            self._portal_handler = 0

    def _accel(self, action: str) -> str:
        getter = getattr(self.app, "get_accels_for_action", None)
        if callable(getter):
            try:
                accels = getter(action)
            except (TypeError, GLib.Error):
                accels = []
            if accels:
                return accels[0]
            if isinstance(self.app, Gtk.Application):
                return ""
        return DEFAULT_ACCELS.get(action, "")

    def _show_all(self) -> None:
        show = getattr(self.app, "show_shortcuts", None)
        if callable(show):
            call_with_parent(show, self.win, getattr(self.app, "window", None))
