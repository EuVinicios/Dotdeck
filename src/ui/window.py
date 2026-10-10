"""The main window: header bar, server switcher, status banner and the server page."""

from __future__ import annotations

from typing import Callable

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import config
from ..core import monitor, server
from ..core.store import MIC_ALLOW, MIC_DENY, Profile
from ..i18n import _, ngettext
from ..web.webview import ServerView
from .onboarding import Onboarding

ZOOM_STEPS = (0.5, 0.67, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


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

        self.view: ServerView | None = None
        self._view_profile: str | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.pages)

        self.onboarding = Onboarding(app)
        self.onboarding.connect("finished", self._on_onboarding_finished)
        self.pages.add_named(self.onboarding, "onboarding")
        self.pages.add_named(self._build_workspace(), "workspace")

        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen)
        self.connect("notify::is-active", lambda *_: app.refresh_status())

        if app.profiles.active:
            self.show_profile(app.profiles.active)
        else:
            self.pages.set_visible_child_name("onboarding")

    # -- layout ----------------------------------------------------------------------------

    def _build_workspace(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        self.header = header

        self._status_dot = Gtk.Label(label="●")
        self._status_dot.add_css_class("status-dot")
        self._server_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=18)
        box = Gtk.Box(spacing=6)
        box.append(self._status_dot)
        box.append(self._server_label)
        self.server_button = Gtk.MenuButton(child=box, always_show_arrow=True,
                                            tooltip_text=_("Switch Server"))
        self.server_button.add_css_class("flat")
        header.pack_start(self.server_button)

        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload (F5)"),
                            action_name="win.reload")
        header.pack_start(reload)

        self.title = Adw.WindowTitle(title=config.APP_NAME)
        header.set_title_widget(self.title)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Zoom In"), "win.zoom-in")
        section.append(_("Zoom Out"), "win.zoom-out")
        section.append(_("Actual Size"), "win.zoom-reset")
        section.append(_("Fullscreen"), "win.fullscreen")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Preferences"), "app.preferences")
        section.append(_("Keyboard Shortcuts"), "app.shortcuts")
        section.append(_("About Dotdeck"), "app.about")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Quit"), "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text=_("Main Menu")))

        self.mic_button = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic",
                                           visible=False, action_name="win.toggle-mute",
                                           tooltip_text=_("Mute Microphone (Ctrl+Shift+M)"))
        header.pack_end(self.mic_button)

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
        return view

    def _install_actions(self) -> None:
        def add(name, callback, parameter=None):
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            return action

        add("reload", lambda *_: self.view and self.view.reload())
        add("zoom-in", lambda *_: self._zoom(+1))
        add("zoom-out", lambda *_: self._zoom(-1))
        add("zoom-reset", lambda *_: self._zoom(0))
        add("fullscreen", lambda *_: self.unfullscreen() if self.is_fullscreen()
            else self.fullscreen())
        add("activity", lambda *_: self.page_action("activity"))
        add("new-chat", lambda *_: self.page_action("new-chat"))
        add("talk-to-chief", lambda *_: self.talk_to_chief())
        add("go-back", lambda *_: self.view and self.view.webview.go_back())
        self._mute_action = Gio.SimpleAction.new_stateful("toggle-mute", None,
                                                          GLib.Variant("b", False))
        self._mute_action.connect("activate", lambda *_: self.toggle_mute())
        self._mute_action.set_enabled(False)
        self.add_action(self._mute_action)
        switch = Gio.SimpleAction.new_stateful("profile", GLib.VariantType("s"),
                                               GLib.Variant("s", ""))
        switch.connect("activate", lambda _a, value: self.app.profiles.set_active(value.unpack()))
        self.add_action(switch)
        self._profile_action = switch
        for index in range(1, 10):
            add(f"profile-{index}", lambda _a, _p, i=index: self._switch_index(i))

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
        active = self.app.profiles.active
        self._profile_action.set_state(GLib.Variant("s", active.id if active else ""))

    def _switch_index(self, index: int) -> None:
        profiles = self.app.profiles.list()
        if index <= len(profiles):
            self.app.profiles.set_active(profiles[index - 1].id)

    def show_onboarding(self) -> None:
        self._drop_view()
        self.onboarding.reset()
        self.pages.set_visible_child_name("onboarding")

    def _on_onboarding_finished(self, _onboarding, profile_id: str) -> None:
        self.app.onboarding_finished(profile_id)

    def show_profile(self, profile: Profile) -> None:
        self.pages.set_visible_child_name("workspace")
        self.rebuild_server_menu()
        if self.view is not None and self._view_profile == profile.id \
                and self.view.profile.url == profile.url:
            self.view.profile = profile
            self.refresh_status()
            return
        self._drop_view()
        self._view_profile = profile.id
        self.content.set_visible_child_name("loading")
        self.refresh_status()

        def with_token(token: str | None) -> None:
            if self._view_profile != profile.id or self.view is not None:
                return
            view = ServerView(profile, token, self, self.app.settings.get("zoom"))
            view.voice.connect("calls-changed", lambda *_: self._on_calls_changed())
            view.connect("state-changed", lambda *_: self.refresh_status())
            self.view = view
            self.content.add_named(view, f"view-{profile.id}")
            self.content.set_visible_child(view)

        self.app.tokens.lookup(profile.id, profile.url, with_token)

    def _drop_view(self) -> None:
        if self.view is not None:
            if self.view.voice.in_call:
                self.toast(_("The call was ended."))
            self.view.destroy()
            self.content.remove(self.view)
            self.view = None
        self._view_profile = None
        self._on_calls_changed()

    def reload_with_token(self, profile_id: str, token: str | None) -> None:
        if self.view is not None and self.view.profile.id == profile_id:
            self.view.set_token(token)

    # -- status ----------------------------------------------------------------------------

    def refresh_status(self) -> None:
        profile = self.app.profiles.active
        if profile is None:
            return
        mon = self.app.monitors.get(profile.id)
        status = mon.status if mon else "unknown"
        self._server_label.set_label(profile.name)
        for name in ("success", "warning", "error", "dim-label"):
            self._status_dot.remove_css_class(name)
        self._status_dot.add_css_class({"online": "success", "offline": "error",
                                        "token": "warning", "local-only": "warning"}.get(
                                            status, "dim-label"))
        self.title.set_title(mon.name if mon and mon.name else profile.name)
        self.title.set_subtitle(self.app.status_line(profile.id))
        self.set_title(f"{profile.name} – {config.APP_NAME}")

        active = mon.active_tasks if mon else 0
        self.tasks_button.set_visible(active > 0)
        self._tasks_label.set_label(ngettext("{n} task", "{n} tasks", active).format(n=active))

        if status == "token":
            self._set_banner(_("{name} needs a valid owner access token.").format(
                name=profile.name), _("Update Token"), lambda: self.app.edit_server(profile.id))
        elif status == "local-only":
            self._set_banner(server.describe(server.LOCAL_ONLY), _("Edit Server"),
                             lambda: self.app.edit_server(profile.id))
        elif status == "offline" and (self.view is None or not self.view.failed):
            self._set_banner(_("No connection to {name}. Trying again…").format(
                name=profile.name), _("Retry Now"), self._retry)
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

    def _on_calls_changed(self) -> None:
        in_call = bool(self.view and self.view.voice.in_call)
        muted = in_call and not self.view.voice.mic_enabled
        self.mic_button.set_visible(in_call)
        self._mute_action.set_enabled(in_call)
        self._mute_action.set_state(GLib.Variant("b", muted))
        self.mic_button.set_icon_name("microphone-disabled-symbolic" if muted
                                      else "audio-input-microphone-symbolic")
        self.mic_button.set_tooltip_text(_("Unmute Microphone (Ctrl+Shift+M)") if muted
                                         else _("Mute Microphone (Ctrl+Shift+M)"))
        if muted:
            self.mic_button.add_css_class("destructive-action")
        else:
            self.mic_button.remove_css_class("destructive-action")
        self.app.refresh_status()

    # -- actions on the page ---------------------------------------------------------------

    def page_action(self, action: str, argument: str = "") -> None:
        if self.view is not None:
            self.view.run_page_action(action, argument)

    def talk_to_chief(self) -> None:
        profile = self.app.profiles.active
        mon = self.app.monitors.get(profile.id) if profile else None
        name = monitor.chief_dot(profile, mon.dots if mon else []) if profile else None
        self.present()
        if name and self.view is not None:
            self.view.run_page_action("dot", name, lambda ok: ok or self.toast(
                _("Could not find {name} on the page.").format(name=name)))

    def toggle_mute(self) -> None:
        if self.view is not None:
            self.view.toggle_mute()

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

    def ask_microphone(self, profile: Profile, callback: Callable[[bool], None]) -> None:
        current = self.app.profiles.get(profile.id) or profile
        if current.microphone == MIC_ALLOW:
            callback(True)
            return
        if current.microphone == MIC_DENY:
            self.toast(_("The microphone is blocked for {name}.").format(name=current.name),
                       _("Preferences"), lambda: self.app.edit_server(current.id))
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
