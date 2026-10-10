"""Preferences: appearance and behaviour, servers, voice calls and global shortcuts."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from .. import config
from ..core import monitor, store, voice
from ..i18n import _
from .server_form import ServerForm
from .updates import UpdatesGroup

COLOR_SCHEMES = ["system", "light", "dark"]
MIC_CHOICES = [store.MIC_ASK, store.MIC_ALLOW, store.MIC_DENY]


def apply_color_scheme(value: str) -> None:
    Adw.StyleManager.get_default().set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(value, Adw.ColorScheme.DEFAULT))


def _scrolled(child: Gtk.Widget) -> Gtk.Widget:
    clamp = Adw.Clamp(maximum_size=560, child=child, margin_top=24, margin_bottom=24,
                      margin_start=12, margin_end=12)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)


def _subpage(title: str, child: Gtk.Widget) -> Adw.NavigationPage:
    view = Adw.ToolbarView(content=child)
    view.add_top_bar(Adw.HeaderBar())
    return Adw.NavigationPage(title=title, child=view)


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "DotdeckPreferencesDialog"

    def __init__(self, app, page: str = "general"):
        super().__init__(search_enabled=False)
        self._app = app
        self._settings = app.settings
        self.add(self._general_page())
        self._servers = Adw.PreferencesPage(title=_("Servers"), name="servers",
                                            icon_name="network-server-symbolic")
        self._servers_group: Adw.PreferencesGroup | None = None
        self.add(self._servers)
        self._fill_servers()
        self.add(self._voice_page())
        self.add(self._shortcuts_page())
        self.set_visible_page_name(page)
        self._handlers = [app.profiles.connect("changed", lambda *_: self._fill_servers())]
        self.connect("closed", self._on_closed)

    def _on_closed(self, _dialog) -> None:
        for handler in self._handlers:
            self._app.profiles.disconnect(handler)
        self._handlers = []

    # -- general ---------------------------------------------------------------------------

    def _switch(self, key: str, title: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self._settings.get(key))
        row.connect("notify::active", lambda r, _p: self._app.change_setting(key, r.get_active()))
        return row

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("General"), name="general",
                                   icon_name="preferences-system-symbolic")
        look = Adw.PreferencesGroup(title=_("Appearance"))
        scheme = Adw.ComboRow(title=_("Style"), model=Gtk.StringList.new(
            [_("Follow System"), _("Light"), _("Dark")]))
        current = self._settings.get("color-scheme")
        scheme.set_selected(COLOR_SCHEMES.index(current) if current in COLOR_SCHEMES else 0)
        scheme.connect("notify::selected", lambda r, _p: self._app.change_setting(
            "color-scheme", COLOR_SCHEMES[r.get_selected()]))
        look.add(scheme)
        page.add(look)

        behaviour = Adw.PreferencesGroup(title=_("Behavior"))
        behaviour.add(self._switch("run-in-background", _("Run in Background"),
                                   _("Closing the window keeps Dotdeck running to deliver "
                                     "notifications")))
        behaviour.add(self._switch("autostart", _("Start When You Log In"),
                                   _("Dotdeck opens hidden, in the background")))
        tray_subtitle = _("Status and quick actions in the top bar") if self._app.tray_host_present \
            else _("Your desktop shows no tray. On GNOME, Dotdeck appears under Background Apps "
                   "in the system menu instead.")
        behaviour.add(self._switch("tray-icon", _("Tray Icon"), tray_subtitle))
        behaviour.add(self._switch("notifications", _("Notifications"),
                                   _("When a Dot finishes or fails a task, and when a scheduled "
                                     "task starts")))
        page.add(behaviour)
        return page

    # -- servers ---------------------------------------------------------------------------

    def _fill_servers(self) -> None:
        if self._servers_group is not None:
            self._servers.remove(self._servers_group)
        group = Adw.PreferencesGroup(
            title=_("Servers"),
            description=_("Each server keeps its own sign-in, cookies and settings."))
        active = self._app.profiles.active
        for profile in self._app.profiles.list():
            row = Adw.ActionRow(title=profile.name, subtitle=profile.url, activatable=True)
            if active and profile.id == active.id:
                badge = Gtk.Label(label=_("Open"), valign=Gtk.Align.CENTER)
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, p=profile: self._edit_server(p))
            group.add(row)
        add = Adw.ButtonRow(title=_("Add Server…"), start_icon_name="list-add-symbolic")
        add.connect("activated", lambda _r: self._add_server())
        group.add(add)
        self._servers.add(group)
        self._servers_group = group

    def _add_server(self) -> None:
        form = ServerForm(_("Add Server"))

        def connected(_form, url, name, token, _result):
            profile = self._app.save_profile(None, url, name, token)
            self._app.profiles.set_active(profile.id)
            self.pop_subpage()

        form.connect("connected", connected)
        self.push_subpage(_subpage(_("Add Server"), _scrolled(form)))
        form.address.grab_focus()

    def _edit_server(self, profile: store.Profile) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        form = ServerForm(_("Save"), profile,
                          lambda cb: self._app.tokens.lookup(profile.id, profile.url, cb))

        def connected(_form, url, name, token, _result):
            self._app.save_profile(profile.id, url, name, token)
            self.add_toast(Adw.Toast(title=_("Server saved")))

        form.connect("connected", connected)
        box.append(form)

        options = Adw.PreferencesGroup(title=_("This Server"))
        mon = self._app.monitors.get(profile.id)
        dots = list(mon.dots) if mon else []
        automatic = monitor.chief_dot(store.Profile(profile.id, profile.name, profile.url), dots)
        labels = [_("Automatic ({name})").format(name=automatic) if automatic else _("Automatic")]
        labels += dots
        chief = Adw.ComboRow(title=_("Chief Dot"),
                             subtitle=_("Who answers “Talk to the Chief” in the tray"),
                             model=Gtk.StringList.new(labels))
        chief.set_selected(dots.index(profile.chief) + 1 if profile.chief in dots else 0)

        def chief_changed(row, _pspec):
            current = self._app.profiles.get(profile.id)
            if current:
                index = row.get_selected()
                current.chief = dots[index - 1] if index > 0 else ""
                self._app.profiles.update(current)

        chief.connect("notify::selected", chief_changed)
        options.add(chief)

        mic = Adw.ComboRow(title=_("Microphone"), subtitle=_("Used for voice calls with Dots"),
                           model=Gtk.StringList.new([_("Ask"), _("Allow"), _("Don't Allow")]))
        mic.set_selected(MIC_CHOICES.index(profile.microphone))

        def mic_changed(row, _pspec):
            current = self._app.profiles.get(profile.id)
            if current:
                current.microphone = MIC_CHOICES[row.get_selected()]
                self._app.profiles.update(current)

        mic.connect("notify::selected", mic_changed)
        options.add(mic)
        box.append(options)
        self._updates = UpdatesGroup(self._app, profile,
                                     lambda text: self.add_toast(Adw.Toast(title=text)))
        box.append(self._updates)

        danger = Adw.PreferencesGroup()
        clear = Adw.ButtonRow(title=_("Clear Website Data"),
                              start_icon_name="edit-clear-all-symbolic")
        clear.connect("activated", lambda _r: self._app.clear_website_data(
            profile.id, lambda: self.add_toast(Adw.Toast(title=_("Website data cleared")))))
        remove = Adw.ButtonRow(title=_("Remove Server"), start_icon_name="user-trash-symbolic")
        remove.add_css_class("destructive-action")
        remove.connect("activated", lambda _r: self._confirm_remove(profile))
        danger.add(clear)
        danger.add(remove)
        box.append(danger)
        self.push_subpage(_subpage(profile.name, _scrolled(box)))

    def _confirm_remove(self, profile: store.Profile) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove {name}?").format(name=profile.name),
            body=_("Dotdeck forgets this server, its access token and its website data. "
                   "Nothing changes on the server itself."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def chosen(_dialog, response):
            if response == "remove":
                self._app.remove_profile(profile.id)
                self.pop_subpage()

        dialog.connect("response", chosen)
        dialog.present(self)

    # -- voice and shortcuts ---------------------------------------------------------------

    def _voice_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Voice"), name="voice",
                                   icon_name="audio-input-microphone-symbolic")
        calls = Adw.PreferencesGroup(
            title=_("Voice Calls"),
            description=_("WebKitGTK has no WebRTC, so Dotdeck runs voice calls natively with "
                          "GStreamer, through PipeWire or PulseAudio."))
        missing = voice.missing_elements()
        engine = Adw.ActionRow(
            title=_("Voice Engine"),
            subtitle=_("Ready") if not missing else
            _("Missing GStreamer plugins: {names}").format(names=", ".join(missing)))
        engine.add_prefix(Gtk.Image(
            icon_name="object-select-symbolic" if not missing else "dialog-warning-symbolic"))
        calls.add(engine)
        calls.add(self._switch("echo-cancellation", _("Echo Cancellation"),
                               _("Recommended when you use speakers instead of headphones")))
        active = self._app.profiles.active
        mon = self._app.monitors.get(active.id) if active else None
        if mon is not None and mon.status == "online":
            server_voice = Adw.ActionRow(
                title=_("Calls on {name}").format(name=active.name),
                subtitle=_("Ready") if mon.voice_ready else
                _("Not configured on the server. With an OpenRouter key, install the voice "
                  "gateway (server/README.md)."))
            server_voice.add_prefix(Gtk.Image(
                icon_name="object-select-symbolic" if mon.voice_ready
                else "dialog-information-symbolic"))
            calls.add(server_voice)
        page.add(calls)
        return page

    def _shortcuts_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Shortcuts"), name="shortcuts",
                                   icon_name="preferences-desktop-keyboard-shortcuts-symbolic")
        shortcuts = self._app.shortcuts
        keys = Adw.PreferencesGroup(title=_("Global Shortcuts"))
        if shortcuts is not None and shortcuts.available:
            keys.set_description(_("They work while another app is focused."))
            keys.add(self._switch("global-shortcut", _("Use Global Shortcuts")))
            for shortcut_id, title in self._app.SHORTCUT_TITLES.items():
                row = Adw.ActionRow(title=title)
                label = Gtk.Label(label=shortcuts.triggers.get(shortcut_id) or _("Not set"),
                                  valign=Gtk.Align.CENTER)
                label.add_css_class("dim-label")
                row.add_suffix(label)
                keys.add(row)
            if shortcuts.can_configure:
                change = Adw.ButtonRow(title=_("Change Shortcuts…"))
                change.connect("activated", lambda _r: self._app.configure_shortcuts())
                keys.add(change)
        else:
            command = f"flatpak run {config.APP_ID} --toggle" if config.IN_FLATPAK else \
                "dotdeck --toggle"
            keys.set_description(_("Your desktop does not offer global shortcuts to apps. Add a "
                                   "custom shortcut in the system keyboard settings with this "
                                   "command:"))
            row = Adw.ActionRow(title=command, subtitle_selectable=True)
            row.add_css_class("monospace")
            keys.add(row)
        page.add(keys)
        return page
