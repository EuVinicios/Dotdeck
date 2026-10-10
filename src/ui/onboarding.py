"""First run: welcome, connect to a server, choose desktop integration."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from .. import config
from ..core import server
from ..i18n import _, ngettext
from .server_form import ServerForm


def _page(title: str, tag: str, child: Gtk.Widget, show_title: bool = True) -> Adw.NavigationPage:
    header = Adw.HeaderBar(show_title=show_title)
    view = Adw.ToolbarView(content=child)
    view.add_top_bar(header)
    return Adw.NavigationPage(title=title, tag=tag, child=view)


def _scrolled(child: Gtk.Widget) -> Gtk.Widget:
    clamp = Adw.Clamp(maximum_size=520, child=child, margin_top=24, margin_bottom=24,
                      margin_start=12, margin_end=12)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER,
                              propagate_natural_height=True)


class Onboarding(Adw.Bin):
    """Emits ``finished(profile_id)`` when the user opens the workspace."""

    __gtype_name__ = "DotdeckOnboarding"
    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._profile_id = ""
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.nav.add(self._welcome_page())

    def reset(self) -> None:
        self.nav.pop_to_tag("welcome")

    # -- pages -----------------------------------------------------------------------------

    def _welcome_page(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name=config.APP_ID, title=_("Welcome to Dotdeck"),
            description=_("Your OpenDots coworkers on the desktop. Connect to an OpenDots "
                          "server on this computer, your home network, Tailscale or the cloud."))
        status.add_css_class("icon-dropshadow")
        connect = Gtk.Button(label=_("Connect to a Server"))
        connect.add_css_class("pill")
        connect.add_css_class("suggested-action")
        connect.connect("clicked", lambda _b: self._open_server(""))
        local = Gtk.Button(label=_("Use This Computer"))
        local.add_css_class("pill")
        local.set_tooltip_text(f"http://localhost:{config.DEFAULT_PORT}")
        local.connect("clicked", lambda _b: self._open_server(
            f"http://localhost:{config.DEFAULT_PORT}"))
        about = Gtk.LinkButton(uri=config.OPENDOTS_URL, label=_("What is OpenDots?"))
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        for widget in (connect, local, about):
            buttons.append(widget)
        status.set_child(buttons)
        return _page(_("Welcome"), "welcome", status, show_title=False)

    def _open_server(self, address: str) -> None:
        form = ServerForm(_("Connect"))
        form.connect("connected", self._on_connected)
        page = _page(_("Connect to a Server"), "server", _scrolled(form))
        page.connect("hidden", lambda _p: form.cancel())
        self.nav.push(page)
        if address:
            form.prefill(address)
        else:
            form.address.grab_focus()

    def _on_connected(self, _form, url: str, name: str, token: str, result) -> None:
        profile = self._app.save_profile(None, url, name, token)
        self._profile_id = profile.id
        self.nav.push(self._ready_page(profile.name, len(result.dots), url))

    def _ready_page(self, name: str, dots: int, url: str) -> Adw.NavigationPage:
        settings = self._app.settings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        status = Adw.StatusPage(icon_name=config.APP_ID,
                                title=_("Connected to {name}").format(name=name))
        if dots:
            status.set_description(ngettext(
                "{count} Dot is ready. Here is how Dotdeck can fit into your desktop.",
                "{count} Dots are ready. Here is how Dotdeck can fit into your desktop.",
                dots).format(count=dots))
        else:
            status.set_description(_("Here is how Dotdeck can fit into your desktop."))
        status.add_css_class("compact")
        box.append(status)

        group = Adw.PreferencesGroup()
        rows = [
            ("global-shortcut", _("Show Dotdeck With Super+Shift+D"),
             _("Works from any app. Your desktop asks to confirm the shortcut.")),
            ("run-in-background", _("Keep Running in the Background"),
             _("Closing the window keeps notifications from your Dots coming.")),
            ("autostart", _("Start When You Log In"), _("Opens hidden, ready for notifications.")),
        ]
        switches = {}
        for key, title, subtitle in rows:
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=settings.get(key))
            switches[key] = row
            group.add(row)
        box.append(group)
        if server.is_cleartext_remote(url):
            warning = Adw.Banner(title=_("This connection is not encrypted. Prefer HTTPS or "
                                         "Tailscale outside your home network."), revealed=True)
            box.append(warning)

        start = Gtk.Button(label=_("Open Workspace"), halign=Gtk.Align.CENTER)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")

        def finish(_button):
            for key, row in switches.items():
                settings.set(key, row.get_active())
            self.emit("finished", self._profile_id)

        start.connect("clicked", finish)
        box.append(start)
        page = _page(_("Ready"), "ready", _scrolled(box), show_title=False)
        page.set_can_pop(False)
        return page
