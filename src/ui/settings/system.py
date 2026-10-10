"""SYSTEM: Advanced (diagnostics and reset) and About."""

from __future__ import annotations

import json
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, GLib, Gst, Gtk  # noqa: E402

from ... import config  # noqa: E402
from ...core import secrets, store, voice  # noqa: E402
from ...i18n import _  # noqa: E402
from .common import SettingsPage, call_with_parent, confirm  # noqa: E402

log = logging.getLogger(__name__)

GEOMETRY = ("window-width", "window-height", "window-maximized")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def system_report() -> dict:
    """The same facts as ``dotdeck --self-test``, gathered in this process."""
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
        webkit = f"{WebKit.get_major_version()}.{WebKit.get_minor_version()}." \
                 f"{WebKit.get_micro_version()}"
    except (ImportError, ValueError):
        webkit = "unavailable"
    missing = voice.missing_elements()
    return {
        "version": config.VERSION,
        "python": sys.version.split()[0],
        "gtk": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        "libadwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}."
                      f"{Adw.get_micro_version()}",
        "webkitgtk": webkit,
        "gstreamer": Gst.version_string(),
        "voice_engine": "ready" if not missing else f"missing {', '.join(missing)}",
        "keyring": secrets.keyring_available(),
        "sandbox": "flatpak" if config.IN_FLATPAK else "snap" if config.IN_SNAP else "none",
    }


def metainfo_path() -> Path | None:
    name = f"{config.APP_ID}.metainfo.xml"
    candidates = []
    source = config.source_data_dir()
    if source is not None:
        candidates.append(source / name)
    for root in (GLib.get_user_data_dir(), *GLib.get_system_data_dirs()):
        candidates.append(Path(root) / "metainfo" / name)
    return next((path for path in candidates if path.is_file()), None)


def release_notes(path: Path | None = None) -> list[dict]:
    """[{version, date, items: [("p"|"li", text)]}] from the AppStream file, newest first."""
    path = path or metainfo_path()
    if path is None:
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        log.info("Could not read the release notes: %s", error)
        return []
    releases = []
    for release in root.iter("release"):
        items = []
        description = release.find("description")
        if description is not None:
            for element in description:
                if element.get(XML_LANG):
                    continue
                if element.tag == "p":
                    items.append(("p", " ".join("".join(element.itertext()).split())))
                elif element.tag in ("ul", "ol"):
                    for item in element:
                        if item.tag == "li" and not item.get(XML_LANG):
                            items.append(("li", " ".join("".join(item.itertext()).split())))
        releases.append({"version": release.get("version", ""), "date": release.get("date", ""),
                         "items": items})
    return releases


class AdvancedPage(SettingsPage):
    name = "advanced"
    title = _("Advanced")
    icon = "applications-engineering-symbolic"

    def build(self) -> None:
        diagnostics = self.group(_("Diagnostics"))
        self.switch(diagnostics, "verbose-logging", _("Detailed Logs"),
                    _("Write debug messages to the system log, for bug reports"),
                    ("debug", "verbose", "logging", "log"))
        report = Adw.ButtonRow(title=_("Copy System Report"), start_icon_name="edit-copy-symbolic")
        report.connect("activated", lambda _r: self.copy_report())
        self.add(diagnostics, report, ("self-test", "versions", "bug report", "support"))

        page = self.group(_("Server Page"))
        reload = Adw.ButtonRow(title=_("Reload Without Cache"),
                               start_icon_name="view-refresh-symbolic")
        reload.connect("activated", lambda _r: self._reload())
        self.add(page, reload, ("refresh", "cache", "hard reload", "force"))
        if config.DEVEL:
            inspector = Adw.ButtonRow(title=_("Open Web Inspector"),
                                      start_icon_name="system-run-symbolic")
            inspector.connect("activated", lambda _r: self._inspector())
            self.add(page, inspector, ("devtools", "inspector", "developer", "console"))

        reset = self.group(_("Reset"))
        button = Adw.ButtonRow(title=_("Reset All Settings…"))
        button.add_css_class("destructive-action")
        button.connect("activated", lambda _r: self._confirm_reset())
        self.add(reset, button, ("defaults", "restore", "factory"))

    def copy_report(self) -> str:
        text = json.dumps(system_report(), indent=2)
        clipboard = self.win.get_clipboard() or Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        self.win.toast(_("System report copied"))
        return text

    def _reload(self) -> None:
        view = self.win.server_view()
        webview = getattr(view, "webview", None)
        if webview is None:
            self.win.toast(_("No server page is open"))
            return
        webview.reload_bypass_cache()
        self.win.toast(_("Reloading without cache"))

    def _inspector(self) -> None:
        view = self.win.server_view()
        webview = getattr(view, "webview", None)
        if webview is not None:
            webview.get_inspector().show()

    def _confirm_reset(self) -> None:
        confirm(self.win, _("Reset All Settings?"),
                _("Every Dotdeck setting goes back to its default. Your servers, tokens, Dots "
                  "and pets stay as they are."), _("Reset"),
                lambda ok: ok and self.reset(), destructive=True)

    def reset(self) -> None:
        for key, default in store.Settings.DEFAULTS.items():
            if key not in GEOMETRY:
                self.win.set_setting(key, default)
        self.win.toast(_("Settings reset to their defaults"))


class AboutPage(SettingsPage):
    name = "about"
    title = _("About")
    icon = "help-about-symbolic"

    def build(self) -> None:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       halign=Gtk.Align.CENTER)
        hero.add_css_class("about-hero")
        icon = Gtk.Image(icon_name=config.APP_ID, pixel_size=128)
        icon.add_css_class("icon-dropshadow")
        hero.append(icon)
        name = Gtk.Label(label=config.APP_NAME, margin_top=6)
        name.add_css_class("title-1")
        hero.append(name)
        version = Gtk.Label(label=config.VERSION, halign=Gtk.Align.CENTER)
        version.add_css_class("version")
        hero.append(version)
        summary = Gtk.Label(
            label=_("A native desktop home for your OpenDots coworkers: notifications, voice "
                    "calls, global shortcuts and a pet for every Dot."),
            wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=52, margin_top=6)
        summary.add_css_class("dim-label")
        hero.append(summary)
        top = self.group()
        top.add(hero)

        links = self.group()
        about = self._link(_("About Dotdeck"), _("Credits, license and acknowledgements"),
                           "go-next-symbolic", self._about)
        self.add(links, about, ("credits", "license", "authors"))
        self.notes = release_notes()
        if self.notes:
            news = self._link(_("What's New"),
                              _("Changes in version {version}").format(
                                  version=self.notes[0]["version"]),
                              "go-next-symbolic", self.show_notes)
            self.add(links, news, ("release notes", "changelog", "news"))

        web = self.group()
        report = self._link(_("Report a Problem"), _("Open an issue on GitHub"),
                            "adw-external-link-symbolic",
                            lambda: self.win.open_url(config.ISSUE_URL))
        self.add(web, report, ("bug", "issue", "feedback", "help"))
        source = self._link(_("Source Code"), _("Dotdeck is free software (GPL-3.0)"),
                            "adw-external-link-symbolic",
                            lambda: self.win.open_url(config.REPOSITORY))
        self.add(web, source, ("github", "repository", "code", "license"))

    @staticmethod
    def _link(title: str, subtitle: str, icon: str, callback) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
        row.add_suffix(Gtk.Image(icon_name=icon))
        row.connect("activated", lambda _r: callback())
        return row

    def _about(self) -> None:
        show = getattr(self.app, "show_about", None)
        if callable(show):
            call_with_parent(show, self.win, getattr(self.app, "window", None))

    def show_notes(self) -> Adw.Dialog:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12,
                      margin_bottom=24, margin_start=24, margin_end=24)
        for release in self.notes:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            heading = Gtk.Label(label=_("Version {version}").format(version=release["version"]),
                                xalign=0)
            heading.add_css_class("title-4")
            section.append(heading)
            if release["date"]:
                date = GLib.DateTime.new_from_iso8601(f"{release['date']}T12:00:00Z", None)
                when = Gtk.Label(label=date.format("%x") if date else release["date"], xalign=0)
                when.add_css_class("dim-label")
                when.add_css_class("caption")
                section.append(when)
            for kind, text in release["items"]:
                label = Gtk.Label(label=f"•  {text}" if kind == "li" else text, wrap=True,
                                  xalign=0, margin_start=6 if kind == "li" else 0)
                section.append(label)
            box.append(section)
        scrolled = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=520)
        view = Adw.ToolbarView(content=scrolled)
        view.add_top_bar(Adw.HeaderBar())
        dialog = Adw.Dialog(title=_("What's New"), child=view, content_width=460)
        dialog.present(self.win)
        return dialog
