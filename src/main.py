"""Dotdeck entry point: the application, its actions and the desktop integration."""

from __future__ import annotations

import json
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

from . import config  # noqa: E402
from .core import monitor, portals, secrets, server, store, tray, voice  # noqa: E402
from .i18n import _, ngettext  # noqa: E402

log = logging.getLogger("dotdeck")


class DotdeckApplication(Adw.Application):
    __gtype_name__ = "DotdeckApplication"

    SHORTCUT_TITLES = {
        "show-window": _("Show or Hide Dotdeck"),
        "toggle-mute": _("Mute or Unmute the Microphone in a Call"),
        "push-to-talk": _("Hold to Talk in a Call"),
    }
    SHORTCUT_TRIGGERS = {"show-window": "LOGO+SHIFT+d", "toggle-mute": "LOGO+SHIFT+m",
                         "push-to-talk": ""}

    def __init__(self):
        super().__init__(application_id=config.APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(config.APP_NAME)
        options = [
            ("version", ord("v"), _("Show the version and exit")),
            ("debug", ord("d"), _("Show debug messages")),
            ("background", 0, _("Start hidden, in the background")),
            ("toggle", 0, _("Show or hide the window")),
            ("toggle-mute", 0, _("Mute or unmute the microphone in a call")),
            ("self-test", 0, _("Check the components Dotdeck needs and exit")),
        ]
        for name, short, description in options:
            self.add_main_option(name, short, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                                 description, None)
        self.settings: store.Settings | None = None
        self.profiles: store.ProfileStore | None = None
        self.tokens = secrets.TokenStore()
        self.monitors: dict[str, monitor.ServerMonitor] = {}
        self.window = None
        self.tray: tray.TrayIcon | None = None
        self.portal: portals.Portal | None = None
        self.background: portals.Background | None = None
        self.shortcuts: portals.GlobalShortcuts | None = None
        self._background_requested = False
        self._told_background = False

    # -- lifecycle -------------------------------------------------------------------------

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{config.APP_NAME} {config.VERSION}")
            return 0
        debug = options.contains("debug") or config.DEVEL
        logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")
        if options.contains("self-test"):
            return self_test()
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gst.init(None)
        self.settings = store.Settings()
        self.profiles = store.ProfileStore()
        self._load_style()
        from .ui.preferences import apply_color_scheme
        apply_color_scheme(self.settings.get("color-scheme"))
        self.settings.connect("changed", self._on_setting_changed)
        self.profiles.connect("changed", lambda *_: self._sync_monitors())
        self.profiles.connect("active-changed", self._on_active_changed)
        self._install_actions()
        self._sync_monitors()

        connection = self.get_dbus_connection()
        if connection is not None:
            self.portal = portals.Portal(connection)
            self.portal.register_host_app()
            self.background = portals.Background(self.portal)
            self.shortcuts = portals.GlobalShortcuts(self.portal)
            self.shortcuts.connect("activated", self._on_shortcut, True)
            self.shortcuts.connect("deactivated", self._on_shortcut, False)
            self.tray = tray.TrayIcon(connection)
            self.tray.connect("activate", lambda *_: self.toggle_window())
            if self.settings.get("tray-icon"):
                self.tray.start()
        self.refresh_status()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        first = self.window is None
        if first:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
        if options.contains("toggle-mute"):
            self.window.toggle_mute()
        elif options.contains("toggle") and not first:
            self.toggle_window()
        elif options.contains("background") and first and self.profiles.active:
            self.went_to_background(quiet=True)
        else:
            self.show_window()
        if first:
            self._bind_shortcuts(interactive=False)
        return 0

    def do_activate(self) -> None:
        self.show_window()

    def do_shutdown(self) -> None:
        for mon in self.monitors.values():
            mon.stop()
        if self.window is not None and self.window.view is not None:
            self.window.view.voice.close_all()
        if self.tray is not None:
            self.tray.stop()
        if self.shortcuts is not None:
            self.shortcuts.close()
        Adw.Application.do_shutdown(self)

    def _load_style(self) -> None:
        display = Gdk.Display.get_default()
        provider = Gtk.CssProvider()
        provider.load_from_path(str(config.PKG_DIR / "ui" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(display)
        source = config.source_data_dir()
        if source:
            theme.add_search_path(str(source / "icons"))

    # -- actions ---------------------------------------------------------------------------

    def _install_actions(self) -> None:
        def add(name, callback, accels=(), parameter=None):
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", list(accels))

        add("quit", lambda *_: self.quit_app(), ["<Control>q"])
        add("preferences", lambda *_: self.show_preferences(), ["<Control>comma"])
        add("shortcuts", lambda *_: self.show_shortcuts(), ["<Control>question"])
        add("about", lambda *_: self.show_about())
        add("show", lambda *_: self.show_window())
        add("add-server", lambda *_: self.show_preferences("servers", add_server=True))
        add("manage-servers", lambda *_: self.show_preferences("servers"))
        add("open-activity", lambda _a, value: self._open_profile(value.unpack(), "activity"),
            parameter=GLib.VariantType("s"))
        add("open-profile", lambda _a, value: self._open_profile(value.unpack(), None),
            parameter=GLib.VariantType("s"))
        accels = {
            "win.reload": ["F5", "<Control>r"],
            "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
            "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
            "win.zoom-reset": ["<Control>0", "<Control>KP_0"],
            "win.fullscreen": ["F11"],
            "win.toggle-mute": ["<Control><Shift>m"],
            "win.activity": ["<Control><Shift>a"],
            "win.talk-to-chief": ["<Control><Shift>c"],
            "win.new-chat": ["<Control>n"],
            "win.go-back": ["<Alt>Left"],
            "window.close": ["<Control>w"],
        }
        for index in range(1, 10):
            accels[f"win.profile-{index}"] = [f"<Alt>{index}"]
        for action, keys in accels.items():
            self.set_accels_for_action(action, keys)

    def quit_app(self) -> None:
        if self.window is not None:
            self.window.save_geometry()
        self.quit()

    # -- window ----------------------------------------------------------------------------

    def show_window(self) -> None:
        if self.window is None:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
        self.window.set_visible(True)
        self.window.present()
        self.refresh_status()

    def toggle_window(self) -> None:
        if self.window is not None and self.window.get_visible() and self.window.is_active():
            self.window.close()
        else:
            self.show_window()

    def went_to_background(self, quiet: bool = False) -> None:
        if self.window is not None:
            self.window.set_visible(False)
        if not self._background_requested and self.background is not None:
            self._background_requested = True
            self._request_background()
        if not quiet and not self._told_background and not (
                self.tray is not None and self.tray.visible_in_host):
            self._told_background = True
            notification = Gio.Notification.new(_("Dotdeck is still running"))
            notification.set_body(_("You will be notified when your Dots finish their work. "
                                    "Open Dotdeck again from the app grid, or quit it from "
                                    "Background Apps."))
            self.send_notification("background", notification)
        self.refresh_status()

    def _request_background(self) -> None:
        def requested(parent: str) -> None:
            self.background.request(parent, _("Deliver notifications from your Dots"),
                                    bool(self.settings.get("autostart")))
        portals.window_identifier(self.window if self.window and self.window.get_visible()
                                  else None, requested)

    def _open_profile(self, profile_id: str, page_action: str | None) -> None:
        if self.profiles.get(profile_id):
            self.profiles.set_active(profile_id)
        self.show_window()
        if page_action:
            GLib.timeout_add(600, lambda: (self.window.page_action(page_action), False)[1])

    def show_preferences(self, page: str = "general", add_server: bool = False,
                         edit: str | None = None) -> None:
        from .ui.preferences import PreferencesDialog
        self.show_window()
        dialog = PreferencesDialog(self, page)
        dialog.present(self.window)
        if add_server:
            dialog._add_server()
        elif edit and self.profiles.get(edit):
            dialog._edit_server(self.profiles.get(edit))

    def edit_server(self, profile_id: str) -> None:
        self.show_preferences("servers", edit=profile_id)

    def show_shortcuts(self) -> None:
        self.show_window()
        dialog = Adw.ShortcutsDialog()
        groups = [
            (_("General"), [(_("Preferences"), "<Control>comma"),
                            (_("Keyboard Shortcuts"), "<Control>question"),
                            (_("Close Window"), "<Control>w"), (_("Quit"), "<Control>q")]),
            (_("Workspace"), [(_("Reload"), "F5"), (_("Fullscreen"), "F11"),
                              (_("Zoom In"), "<Control>plus"), (_("Zoom Out"), "<Control>minus"),
                              (_("Actual Size"), "<Control>0"), (_("Go Back"), "<Alt>Left"),
                              (_("Switch to Server 1–9"), "<Alt>1"),
                              (_("New Chat"), "<Control>n"), (_("Activity"), "<Control><Shift>a"),
                              (_("Talk to the Chief"), "<Control><Shift>c")]),
            (_("Voice Calls"), [(_("Mute or Unmute the Microphone"), "<Control><Shift>m")]),
        ]
        for title, items in groups:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self.window)

    def show_about(self) -> None:
        self.show_window()
        about = Adw.AboutDialog(
            application_name=config.APP_NAME, application_icon=config.APP_ID,
            developer_name=config.DEVELOPER, version=config.VERSION, website=config.WEBSITE,
            issue_url=config.ISSUE_URL, license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 EuVinicios",
            comments=_("A native desktop client for OpenDots servers: your AI coworkers with "
                       "notifications, tray status, global shortcuts and voice calls."),
            developers=["EuVinicios https://github.com/EuVinicios"],
            translator_credits=_("translator-credits"))
        about.add_acknowledgement_section(_("Works With"),
                                          [f"OpenDots by CopilotKit {config.OPENDOTS_URL}"])
        about.add_legal_section(
            "OpenDots", None, Gtk.License.CUSTOM,
            _("Dotdeck is an independent project and is not affiliated with or endorsed by "
              "CopilotKit. OpenDots is distributed by CopilotKit under the MIT license."))
        about.present(self.window)

    # -- profiles --------------------------------------------------------------------------

    def save_profile(self, profile_id: str | None, url: str, name: str, token: str):
        """Create or update a server; the token goes to the keyring, not the profile file."""
        if profile_id and self.profiles.get(profile_id):
            profile = self.profiles.get(profile_id)
            old_url = profile.url
            profile.url, profile.name = url, name or profile.name
            self.profiles.update(profile)
            if old_url != url:
                self.tokens.clear(profile.id, old_url)
        else:
            existing = self.profiles.find_by_url(url)
            profile = existing or self.profiles.add(name, url)
            if existing and name:
                existing.name = name
                self.profiles.update(existing)
        if token:
            self.tokens.store(profile.id, profile.url,
                              _("Dotdeck token for {name}").format(name=profile.name), token,
                              lambda ok: ok or self._warn_keyring())
        if self.window is not None and profile.id == (self.window.view.profile.id
                                                      if self.window.view else None):
            self.window.reload_with_token(profile.id, token or self.tokens.cached(profile.id))
        self._sync_monitors()
        return profile

    def remove_profile(self, profile_id: str) -> None:
        profile = self.profiles.get(profile_id)
        if profile is None:
            return
        self.tokens.clear(profile.id, profile.url)
        self.clear_website_data(profile.id, lambda: None)
        self.profiles.remove(profile_id)
        if self.profiles.active is None and self.window is not None:
            self.window.show_onboarding()

    def clear_website_data(self, profile_id: str, done) -> None:
        window = self.window
        if window is not None and window.view is not None and window.view.profile.id == profile_id:
            window.view.clear_website_data(done)
            return
        import shutil
        shutil.rmtree(config.profile_data_dir(profile_id), ignore_errors=True)
        shutil.rmtree(config.profile_cache_dir(profile_id), ignore_errors=True)
        done()

    def onboarding_finished(self, profile_id: str) -> None:
        self.profiles.set_active(profile_id)
        if self.window is not None:
            self.window.show_profile(self.profiles.active)
        self._apply_autostart()
        if self.settings.get("tray-icon") and self.tray is not None:
            self.tray.start()
        self._bind_shortcuts(interactive=True)

    def _on_active_changed(self, _store) -> None:
        profile = self.profiles.active
        if self.window is not None:
            if profile is None:
                self.window.show_onboarding()
            else:
                self.window.show_profile(profile)
        self.refresh_status()

    def token_from_page(self, profile, token: str) -> None:
        """The user unlocked the page with a token: keep it if the server accepts it."""
        def checked(result: server.ProbeResult) -> None:
            if not result.ok:
                return

            def saved(ok: bool) -> None:
                if ok and self.window is not None:
                    self.window.toast(_("Access token saved in the keyring"))
                elif not ok:
                    self._warn_keyring()
                mon = self.monitors.get(profile.id)
                if mon:
                    mon.poll_now()

            self.tokens.store(profile.id, profile.url,
                              _("Dotdeck token for {name}").format(name=profile.name), token,
                              saved)

        server.ServerClient(profile.url, token).probe(checked)

    def _warn_keyring(self) -> None:
        if self.window is not None:
            self.window.toast(_("The keyring is not available, so the token is kept only until "
                                "Dotdeck closes."))

    # -- monitoring, notifications, tray ---------------------------------------------------

    def _sync_monitors(self) -> None:
        wanted = {p.id: p for p in self.profiles.list()}
        for profile_id in list(self.monitors):
            if profile_id not in wanted:
                self.monitors.pop(profile_id).stop()
        for profile_id, profile in wanted.items():
            mon = self.monitors.get(profile_id)
            if mon is not None and mon.profile.url != profile.url:
                mon.stop()
                mon = None
            if mon is None:
                mon = monitor.ServerMonitor(
                    profile, lambda cb, p=profile: self.tokens.lookup(p.id, p.url, cb))
                mon.connect("changed", lambda *_: self.refresh_status())
                mon.connect("task-event", self._on_task_event)
                self.monitors[profile_id] = mon
                mon.start()
            else:
                mon.profile = profile
                mon.poll_now()
        if self.window is not None:
            self.window.rebuild_server_menu()
        self.refresh_status()

    def status_line(self, profile_id: str) -> str:
        mon = self.monitors.get(profile_id)
        if mon is None or mon.status == "unknown":
            return _("Connecting…")
        if mon.status == "offline":
            return _("Offline")
        if mon.status == "token":
            return _("Access token needed")
        if mon.status == "local-only":
            return _("Only reachable from its own computer")
        if mon.status != "online":
            return _("Not an OpenDots server")
        parts = [ngettext("{n} Dot", "{n} Dots", len(mon.dots)).format(n=len(mon.dots))]
        if mon.active_tasks:
            parts.append(ngettext("{n} task running", "{n} tasks running",
                                  mon.active_tasks).format(n=mon.active_tasks))
        if mon.active_calls:
            parts.append(_("in a call"))
        if mon.missing:
            parts.append(_("setup incomplete"))
        return " · ".join(parts)

    def _window_focused(self) -> bool:
        return bool(self.window and self.window.get_visible() and self.window.is_active())

    def _on_task_event(self, mon, kind, task_id, prompt, error) -> None:
        if not self.settings.get("notifications") or self._window_focused():
            return
        titles = {
            monitor.TASK_COMPLETED: _("A Dot finished a task"),
            monitor.TASK_FAILED: _("A task failed"),
            monitor.TASK_TRIGGERED: _("A scheduled task started"),
        }
        text = prompt if len(prompt) <= 140 else prompt[:139] + "…"
        if kind == monitor.TASK_FAILED and error:
            text = f"{text}\n{error[:120]}"
        notification = Gio.Notification.new(f"{titles.get(kind, kind)} · {mon.profile.name}")
        notification.set_body(text)
        notification.set_priority(Gio.NotificationPriority.HIGH if kind == monitor.TASK_FAILED
                                  else Gio.NotificationPriority.NORMAL)
        notification.set_default_action_and_target_value(
            "app.open-activity", GLib.Variant("s", mon.profile.id))
        self.send_notification(f"task-{mon.profile.id}-{task_id}-{kind}", notification)

    def web_notification(self, profile, title: str, body: str) -> None:
        if not self.settings.get("notifications"):
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_default_action_and_target_value("app.open-profile",
                                                         GLib.Variant("s", profile.id))
        self.send_notification(None, notification)

    def refresh_status(self) -> None:
        if self.profiles is None:
            return
        if self.window is not None:
            self.window.refresh_status()
        profile = self.profiles.active
        mon = self.monitors.get(profile.id) if profile else None
        line = self.status_line(profile.id) if profile else _("No server yet")
        if self.background is not None and self.window is not None \
                and not self.window.get_visible():
            self.background.set_status(f"{profile.name}: {line}" if profile else line)
        if self.tray is not None:
            self._update_tray(profile, mon, line)

    @property
    def tray_host_present(self) -> bool:
        return bool(self.tray and self.tray.visible_in_host)

    def _update_tray(self, profile, mon, line: str) -> None:
        visible = bool(self.window and self.window.get_visible())
        view = self.window.view if self.window else None
        in_call = bool(view and view.voice.in_call)
        chief = monitor.chief_dot(profile, mon.dots) if profile and mon else None
        items = [
            tray.MenuItem(profile.name if profile else config.APP_NAME, enabled=False),
            tray.MenuItem(line, enabled=False),
            tray.MenuItem(separator=True),
            tray.MenuItem(_("Hide Dotdeck") if visible else _("Show Dotdeck"),
                          action=self.toggle_window),
            tray.MenuItem(_("Talk to {name}").format(name=chief or ""), visible=bool(chief),
                          action=lambda: self.window and self.window.talk_to_chief()),
            tray.MenuItem(_("Activity"), visible=profile is not None,
                          action=lambda: self._open_profile(profile.id, "activity")),
            tray.MenuItem(_("Mute Microphone"), toggle="checkmark", visible=in_call,
                          checked=in_call and not view.voice.mic_enabled,
                          action=lambda: self.window and self.window.toggle_mute()),
            tray.MenuItem(separator=True),
        ]
        profiles = self.profiles.list()
        if len(profiles) > 1:
            items.append(tray.MenuItem(_("Servers"), children=[
                tray.MenuItem(p.name, toggle="radio", checked=profile is not None and
                              p.id == profile.id,
                              action=lambda p=p: self._open_profile(p.id, None))
                for p in profiles]))
        items += [
            tray.MenuItem(_("Preferences"), action=self.show_preferences),
            tray.MenuItem(separator=True),
            tray.MenuItem(_("Quit"), action=self.quit_app),
        ]
        status = mon.status if mon else "unknown"
        badge = "active" if mon and (mon.active_tasks or mon.active_calls) else \
            "alert" if status == "token" else None
        self.tray.update(f"{profile.name}: {line}" if profile else line, badge,
                         status in ("offline", "unknown") and profile is not None, items)

    # -- settings and shortcuts ------------------------------------------------------------

    def change_setting(self, key: str, value) -> None:
        self.settings.set(key, value)

    def _on_setting_changed(self, _settings, key: str) -> None:
        if key == "color-scheme":
            from .ui.preferences import apply_color_scheme
            apply_color_scheme(self.settings.get(key))
        elif key == "tray-icon" and self.tray is not None:
            if self.settings.get(key):
                self.tray.start()
                self.refresh_status()
            else:
                self.tray.stop()
        elif key == "autostart":
            self._apply_autostart()
        elif key == "global-shortcut":
            if self.settings.get(key):
                self._bind_shortcuts(interactive=True)
            elif self.shortcuts is not None:
                self.shortcuts.close()

    def _apply_autostart(self) -> None:
        if self.background is None:
            return

        def requested(parent: str) -> None:
            self.background.request(parent, _("Deliver notifications from your Dots"),
                                    bool(self.settings.get("autostart")))
        portals.window_identifier(self.window, requested)

    def _bind_shortcuts(self, interactive: bool) -> None:
        shortcuts = self.shortcuts
        if shortcuts is None or not shortcuts.available or not self.settings.get(
                "global-shortcut") or not self.profiles.active or shortcuts.session:
            return
        items = [(sid, title, self.SHORTCUT_TRIGGERS[sid])
                 for sid, title in self.SHORTCUT_TITLES.items()]

        def bound(ok: bool) -> None:
            if not ok and interactive and self.window is not None:
                self.window.toast(_("The global shortcuts were not set up."))

        portals.window_identifier(self.window, lambda parent: shortcuts.bind(items, parent,
                                                                                bound))

    def configure_shortcuts(self) -> None:
        if self.shortcuts is not None:
            portals.window_identifier(self.window, self.shortcuts.configure)

    def _on_shortcut(self, _shortcuts, shortcut_id: str, pressed: bool) -> None:
        if self.window is None:
            return
        if shortcut_id == "show-window" and pressed:
            self.toggle_window()
        elif shortcut_id == "toggle-mute" and pressed:
            self.window.toggle_mute()
        elif shortcut_id == "push-to-talk":
            self.window.set_mic(pressed)


def self_test() -> int:
    """Print what Dotdeck can use on this system, for bug reports and CI."""
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    Gst.init(None)
    missing = voice.missing_elements()
    report = {
        "version": config.VERSION,
        "python": sys.version.split()[0],
        "gtk": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        "libadwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}."
                      f"{Adw.get_micro_version()}",
        "webkitgtk": f"{WebKit.get_major_version()}.{WebKit.get_minor_version()}."
                     f"{WebKit.get_micro_version()}",
        "gstreamer": Gst.version_string(),
        "voice_engine": "ready" if not missing else f"missing {', '.join(missing)}",
        "keyring": secrets.keyring_available(),
        "sandbox": "flatpak" if config.IN_FLATPAK else "snap" if config.IN_SNAP else "none",
    }
    print(json.dumps(report, indent=2))
    return 0 if not missing else 1


def main() -> int:
    app = DotdeckApplication()
    return app.run(sys.argv)
