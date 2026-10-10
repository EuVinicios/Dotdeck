"""The form that connects to an OpenDots server: address, name, token and a live test."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..core import server
from ..core.store import Profile
from ..i18n import _, ngettext


class ServerForm(Gtk.Box):
    """Emits ``connected(url, name, token, result)`` once a working address was found."""

    __gtype_name__ = "DotdeckServerForm"
    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, object)),
    }

    def __init__(self, button_label: str, profile: Profile | None = None,
                 saved_token: Callable[[Callable[[str | None], None]], None] | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._profile = profile
        self._saved_token = saved_token
        self._cancellable: Gio.Cancellable | None = None

        where = Adw.PreferencesGroup(
            title=_("Server"),
            description=_("For example http://localhost:4310, http://192.168.0.10:4310 or "
                          "https://homelab.tail1234.ts.net"))
        self.address = Adw.EntryRow(title=_("Server address"), input_purpose=Gtk.InputPurpose.URL)
        self.address.set_activates_default(True)
        self.name = Adw.EntryRow(title=_("Name (optional)"))
        self.name.set_activates_default(True)
        where.add(self.address)
        where.add(self.name)
        self.append(where)

        access = Adw.PreferencesGroup(
            title=_("Access"),
            description=_("Leave it empty to keep the saved token.") if profile else
            _("Needed when the server can be reached from other devices (OWNER_TOKEN in its "
              ".env file). Dotdeck keeps it in your keyring."))
        self.token = Adw.PasswordEntryRow(title=_("Owner access token"))
        self.token.set_activates_default(True)
        access.add(self.token)
        self.append(access)

        self.result = Adw.ActionRow(visible=False, title_lines=0)
        self.result.add_css_class("property")
        self._result_icon = Gtk.Image(valign=Gtk.Align.CENTER)
        self._spinner = Adw.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.result.add_prefix(self._spinner)
        self.result.add_prefix(self._result_icon)
        status = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        status.add_css_class("boxed-list")
        status.append(self.result)
        self._status_box = status
        status.set_visible(False)
        self.append(status)

        self.button = Gtk.Button(label=button_label, halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", lambda _b: self.submit())
        self.append(self.button)

        if profile:
            self.address.set_text(profile.url)
            self.name.set_text(profile.name)
        for row in (self.address, self.name, self.token):
            row.connect("entry-activated", lambda _r: self.submit())
        self.address.connect("changed", lambda _r: self.address.remove_css_class("error"))

    def prefill(self, address: str) -> None:
        self.address.set_text(address)
        self.address.grab_focus()

    def cancel(self) -> None:
        if self._cancellable:
            self._cancellable.cancel()
            self._cancellable = None

    # -- testing ---------------------------------------------------------------------------

    def _show(self, icon: str | None, message: str, css: str = "") -> None:
        self._status_box.set_visible(True)
        self.result.set_visible(True)
        self.result.set_title(GLib.markup_escape_text(message))
        self._spinner.set_visible(icon is None)
        self._result_icon.set_visible(icon is not None)
        if icon:
            self._result_icon.set_from_icon_name(icon)
        for name in ("success", "warning", "error"):
            self._result_icon.remove_css_class(name)
        if css:
            self._result_icon.add_css_class(css)

    def submit(self) -> None:
        if self._cancellable is not None:
            return
        text = self.address.get_text()
        try:
            candidates = server.candidate_urls(text)
        except ValueError as error:
            self.address.add_css_class("error")
            self._show("dialog-error-symbolic", str(error), "error")
            self.address.grab_focus()
            return
        typed = self.token.get_text().strip()
        if typed or not self._saved_token:
            self._probe(candidates, typed)
        else:
            self._saved_token(lambda saved: self._probe(candidates, saved or ""))

    def _probe(self, candidates: list[str], token: str) -> None:
        self._cancellable = Gio.Cancellable()
        cancellable = self._cancellable
        self.button.set_sensitive(False)
        self._show(None, _("Connecting to {host}…").format(host=server.host_label(candidates[0])))
        best: list[tuple[str, server.ProbeResult]] = []

        def next_candidate(index: int) -> None:
            if cancellable.is_cancelled():
                return
            if index >= len(candidates):
                finish(*best[0])
                return
            url = candidates[index]
            server.ServerClient(url, token or None).probe(
                lambda result: got(index, url, result), cancellable)

        def got(index: int, url: str, result: server.ProbeResult) -> None:
            if cancellable.is_cancelled():
                return
            best.append((url, result))
            decisive = result.kind not in (server.UNREACHABLE, server.TLS_ERROR,
                                           server.NOT_OPENDOTS)
            if decisive:
                finish(url, result)
            else:
                best.sort(key=lambda item: [server.NOT_OPENDOTS, server.TLS_ERROR,
                                            server.UNREACHABLE].index(item[1].kind))
                next_candidate(index + 1)

        def finish(url: str, result: server.ProbeResult) -> None:
            self._cancellable = None
            self.button.set_sensitive(True)
            if result.ok:
                if url != candidates[0]:
                    self.address.set_text(url)
                dots = len(result.dots)
                detail = _("Connected to {name}.").format(name=result.name or server.host_label(url))
                if dots:
                    detail += " " + ngettext("{count} Dot is ready.", "{count} Dots are ready.",
                                             dots).format(count=dots)
                if server.is_cleartext_remote(url):
                    detail += " " + _("This connection is not encrypted; prefer HTTPS or "
                                      "Tailscale outside your home network.")
                self._show("object-select-symbolic", detail, "success")
                name = self.name.get_text().strip() or result.name or server.host_label(url)
                self.emit("connected", url, name, token, result)
                return
            self._show("dialog-warning-symbolic", result.message or server.describe(result.kind),
                       "warning")
            if result.kind in (server.TOKEN_REQUIRED, server.TOKEN_INVALID):
                self.token.grab_focus()
            else:
                self.address.grab_focus()

        next_candidate(0)
