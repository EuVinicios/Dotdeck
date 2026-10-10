"""SERVER › Servers: the servers Dotdeck knows, each with its own page."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ... import config
from ...core import monitor, store
from ...i18n import _
from ..server_form import ServerForm
from ..updates import UpdatesGroup
from .common import SettingsPage, confirm, escape, suffix_label

MIC_CHOICES = [store.MIC_ASK, store.MIC_ALLOW, store.MIC_DENY]
STATUS_CSS = {"online": "success", "offline": "error", "token": "warning",
              "local-only": "warning", "other": "error"}


def packaging() -> str:
    if config.IN_SNAP:
        return _("Snap")
    if config.IN_FLATPAK:
        return _("Flatpak")
    if str(config.PKG_DIR).startswith(("/usr/", "/opt/")):
        return _("System package")
    return _("Source checkout")


def status_text(app, profile_id: str) -> str:
    line = getattr(app, "status_line", None)
    if callable(line):
        return line(profile_id)
    mon = app.monitors.get(profile_id)
    return {"online": _("Online"), "offline": _("Offline"),
            "token": _("Access token needed")}.get(mon.status if mon else "", _("Connecting…"))


def status_dot(status: str) -> Gtk.Widget:
    dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    dot.add_css_class("settings-status")
    css = STATUS_CSS.get(status)
    if css:
        dot.add_css_class(css)
    return dot


def subpage(title: str, children: list[Gtk.Widget], tag: str) -> Adw.NavigationPage:
    """A pushed page laid out like the others (an Adw.PreferencesPage)."""
    page = Adw.PreferencesPage()
    for child in children:
        if not isinstance(child, Adw.PreferencesGroup):
            group = Adw.PreferencesGroup()
            group.add(child)
            child = group
        page.add(child)
    view = Adw.ToolbarView(content=page)
    view.add_top_bar(Adw.HeaderBar())
    return Adw.NavigationPage(title=title, child=view, tag=tag)


class ServersPage(SettingsPage):
    name = "servers"
    title = _("Servers")
    icon = "network-server-symbolic"

    def build(self) -> None:
        self._signature = None
        self._rows: list[Gtk.Widget] = []
        self._form: ServerForm | None = None
        self.list = self.group(_("Servers"),
                               _("Each server keeps its own sign-in, cookies and settings."))
        self.add_row = Adw.ButtonRow(title=_("Add Server…"), start_icon_name="list-add-symbolic")
        self.add_row.connect("activated", lambda _r: self.add_server())
        self.add(self.list, self.add_row, ("new", "connect", "opendots", "address"))

        about = self.group(_("Dotdeck"))
        version = Adw.ActionRow(title=_("Version"))
        version.add_suffix(suffix_label(config.VERSION))
        self.add(about, version, ("version", "release", "build"))
        package = Adw.ActionRow(title=_("Installed As"))
        package.add_suffix(suffix_label(packaging()))
        self.add(about, package, ("snap", "flatpak", "deb", "package", "sandbox"))
        self.refresh()

    def refresh(self) -> None:
        profiles = self.app.profiles.list()
        active = self.app.profiles.active
        signature = tuple((p.id, p.name, p.url, active is not None and p.id == active.id,
                           self.app.monitors[p.id].status if p.id in self.app.monitors
                           else "unknown", status_text(self.app, p.id)) for p in profiles)
        if signature == self._signature:
            return
        self._signature = signature
        for row in self._rows:
            self.list.remove(row)
        self._rows = []
        for profile, (*_info, status, line) in zip(profiles, signature):
            row = Adw.ActionRow(title=escape(profile.name), activatable=True,
                                subtitle=escape(f"{profile.url} · {line}"))
            row.set_subtitle_lines(1)
            row.add_prefix(status_dot(status))
            if active is not None and profile.id == active.id:
                badge = Gtk.Label(label=_("Open"), valign=Gtk.Align.CENTER)
                badge.add_css_class("settings-badge")
                badge.add_css_class("accent")
                row.add_suffix(badge)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, pid=profile.id: self.edit_server(pid))
            self.list.remove(self.add_row)
            self.add(self.list, row, ("server", profile.url))
            self.list.add(self.add_row)
            self._rows.append(row)

    # -- adding ----------------------------------------------------------------------------

    def add_server(self) -> None:
        if self.win.nav.find_page("add-server") is not None:
            return
        form = ServerForm(_("Add Server"))

        def connected(_form, url, name, token, _result) -> None:
            profile = self.app.save_profile(None, url, name, token)
            self.app.profiles.set_active(profile.id)
            self.win.pop_to_root()
            self.win.toast(_("{name} added").format(name=profile.name))

        form.connect("connected", connected)
        page = subpage(_("Add Server"), [form], "add-server")
        page.connect("hidden", lambda _p: form.cancel())
        self.win.push(page)
        form.address.grab_focus()

    # -- one server ------------------------------------------------------------------------

    def edit_server(self, profile_id: str) -> None:
        profile = self.app.profiles.get(profile_id)
        if profile is None:
            return
        tag = f"server-{profile.id}"
        if self.win.nav.find_page(tag) is not None:
            return
        form = ServerForm(_("Save"), profile,
                          lambda cb: self.app.tokens.lookup(profile.id, profile.url, cb))

        def connected(_form, url, name, token, _result) -> None:
            self.app.save_profile(profile.id, url, name, token)
            self.win.toast(_("Server saved"))

        form.connect("connected", connected)

        danger = Adw.PreferencesGroup()
        clear = Adw.ButtonRow(title=_("Clear Website Data…"),
                              start_icon_name="edit-clear-all-symbolic")
        clear.connect("activated", lambda _r: self.confirm_clear(profile))
        remove = Adw.ButtonRow(title=_("Remove Server…"), start_icon_name="user-trash-symbolic")
        remove.add_css_class("destructive-action")
        remove.connect("activated", lambda _r: self._confirm_remove(profile))
        danger.add(clear)
        danger.add(remove)
        page = subpage(profile.name, [form, self._this_server(profile),
                                      UpdatesGroup(self.app, profile, self.win.toast), danger],
                       tag)
        page.connect("hidden", lambda _p: form.cancel())
        self.win.push(page)

    def _this_server(self, profile: store.Profile) -> Adw.PreferencesGroup:
        options = Adw.PreferencesGroup(title=_("This Server"))
        mon = self.app.monitors.get(profile.id)
        dots = list(mon.dots) if mon else []
        automatic = monitor.chief_dot(store.Profile(profile.id, profile.name, profile.url), dots)
        labels = [_("Automatic ({name})").format(name=automatic) if automatic else _("Automatic")]
        labels += dots
        chief = Adw.ComboRow(title=_("Chief Dot"),
                             subtitle=_("Who answers “Talk to the Chief” in the tray"),
                             model=Gtk.StringList.new(labels))
        chief.set_selected(dots.index(profile.chief) + 1 if profile.chief in dots else 0)

        def chief_changed(row, _pspec) -> None:
            current = self.app.profiles.get(profile.id)
            if current:
                index = row.get_selected()
                current.chief = dots[index - 1] if 0 < index <= len(dots) else ""
                self.app.profiles.update(current)

        chief.connect("notify::selected", chief_changed)
        options.add(chief)

        mic = Adw.ComboRow(title=_("Microphone Access"),
                           subtitle=_("For voice calls with the Dots on this server"),
                           model=Gtk.StringList.new([_("Ask"), _("Allow"), _("Don't Allow")]))
        mic.set_selected(MIC_CHOICES.index(profile.microphone)
                         if profile.microphone in MIC_CHOICES else 0)

        def mic_changed(row, _pspec) -> None:
            current = self.app.profiles.get(profile.id)
            if current:
                current.microphone = MIC_CHOICES[row.get_selected()]
                self.app.profiles.update(current)

        mic.connect("notify::selected", mic_changed)
        options.add(mic)
        return options

    def confirm_clear(self, profile: store.Profile) -> None:
        confirm(self.win, _("Clear Website Data?"),
                _("Dotdeck forgets the cookies and storage of {name}. The page reloads and may "
                  "ask for the access token again.").format(name=profile.name),
                _("Clear"), lambda ok: ok and self.app.clear_website_data(
                    profile.id, lambda: self.win.toast(_("Website data cleared"))),
                destructive=True)

    def _confirm_remove(self, profile: store.Profile) -> None:
        def chosen(ok: bool) -> None:
            if ok:
                self.app.remove_profile(profile.id)
                self.win.pop_to_root()
                self.win.toast(_("{name} removed").format(name=profile.name))

        confirm(self.win, _("Remove {name}?").format(name=profile.name),
                _("Dotdeck forgets this server, its access token and its website data. "
                  "Nothing changes on the server itself."), _("Remove"), chosen,
                destructive=True)
