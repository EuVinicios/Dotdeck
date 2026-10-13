"""Building blocks shared by the settings pages: the page base class and bound rows."""

from __future__ import annotations

import inspect
from typing import Callable, Sequence

from gi.repository import Adw, GLib, Gtk, Pango

from ...i18n import _


def escape(text: str) -> str:
    """Row titles and subtitles are markup; names from servers are not."""
    return GLib.markup_escape_text(text or "")


def status_icon(name: str, css: str = "") -> Gtk.Image:
    icon = Gtk.Image(icon_name=name, valign=Gtk.Align.CENTER)
    if css:
        icon.add_css_class(css)
    return icon


def badge(text: str, css: str = "") -> Gtk.Label:
    """A small pill next to a row's title: Chief, Hidden, Unsaved, Read-only…"""
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("settings-badge")
    if css:
        label.add_css_class(css)
    return label


def suffix_label(text: str = "", dim: bool = True) -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER, xalign=1,
                      ellipsize=Pango.EllipsizeMode.END)
    if dim:
        label.add_css_class("dim-label")
    return label


def confirm(parent: Gtk.Widget, heading: str, body: str, action: str,
            done: Callable[[bool], None], destructive: bool = False) -> Adw.AlertDialog:
    """An Adw.AlertDialog with Cancel and ``action``; ``done(confirmed)``."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel" if destructive else "ok")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: done(response == "ok"))
    dialog.present(parent)
    return dialog


def _visible_dialog(window) -> Adw.Dialog | None:
    getter = getattr(window, "get_visible_dialog", None)
    try:
        return getter() if callable(getter) else None
    except (TypeError, GLib.Error):
        return None


def call_with_parent(method: Callable, parent: Gtk.Widget, host=None) -> None:
    """Call an app method that shows a dialog (``show_about``…) over ``parent``.

    The Settings window stays above the main window (it is its transient child), so a
    dialog shown on the main window (``host``) would open hidden behind it. When the
    method cannot take a ``parent``, the dialog it presented on ``host`` moves here.
    """
    try:
        accepts = "parent" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        method(parent=parent)
        return
    before = _visible_dialog(host)
    method()
    dialog = _visible_dialog(host)
    if dialog is not None and dialog is not before and host is not parent:
        dialog.force_close()       # our reference keeps it alive for the second present
        dialog.present(parent)


def toggle_group(choices: Sequence[tuple[str, str]], active: str,
                 changed: Callable[[str], None]) -> Adw.ToggleGroup:
    """A segmented control; ``choices`` are (name, label)."""
    group = Adw.ToggleGroup(valign=Gtk.Align.CENTER, homogeneous=True, can_shrink=False)
    for name, label in choices:
        group.add(Adw.Toggle(name=name, label=label))
    if active in [name for name, _label in choices]:
        group.set_active_name(active)

    def notify(widget, _pspec):
        name = widget.get_active_name()
        if name:
            changed(name)

    group.connect("notify::active-name", notify)
    return group


def string_model(labels: Sequence[str]) -> Gtk.StringList:
    return Gtk.StringList.new(list(labels))


SAVE_FAILED = "save-failed"


def row_failed(row: Adw.ActionRow, error: str) -> None:
    """The row itself says the change did not save, as the Grok Bot does: in its subtitle
    until the next try, with the server's words in its tooltip."""
    if not row.has_css_class(SAVE_FAILED):
        row._saved_subtitle = row.get_subtitle() or ""
        row.add_css_class(SAVE_FAILED)
    row.set_subtitle(escape(_("Couldn't save. Try again.")))
    row.set_tooltip_text(error or None)


def row_saving(row: Adw.ActionRow) -> None:
    """A new try: the row gets its own subtitle back."""
    if row.has_css_class(SAVE_FAILED):
        row.remove_css_class(SAVE_FAILED)
        row.set_subtitle(getattr(row, "_saved_subtitle", ""))
        row.set_tooltip_text(None)


class SettingsPage:
    """One page of the settings window: an Adw.PreferencesPage plus search entries."""

    name = ""
    title = ""
    icon = ""
    section = ""          # set on the first page of a sidebar section
    # The page this one is a section of: its groups go there, under its title, and
    # showing it shows that page at its first group (ADR-029: fewer places to look).
    host = ""

    def __init__(self, win):
        self.win = win
        self.app = win.app
        self.first_group: Adw.PreferencesGroup | None = None
        if self.host:
            self.widget = win.pages[self.host].widget
        else:
            self.widget = Adw.PreferencesPage(name=self.name, title=self.title,
                                              icon_name=self.icon)
        self.build()

    # -- building ----------------------------------------------------------------------------

    def build(self) -> None:
        raise NotImplementedError

    def group(self, title: str = "", description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        if self.first_group is None:
            self.first_group = group
            if self.host and not title:
                group.set_title(self.title)      # where this section begins in its host
        self.widget.add(group)
        return group

    def add(self, group: Adw.PreferencesGroup, row: Gtk.Widget,
            keywords: Sequence[str] = ()) -> Gtk.Widget:
        group.add(row)
        self.win.index(self.name, group, row, keywords)
        return row

    def switch(self, group: Adw.PreferencesGroup, key: str, title: str, subtitle: str = "",
               keywords: Sequence[str] = ()) -> Adw.SwitchRow:
        row = self.win.setting_switch(key, title, subtitle)
        return self.add(group, row, keywords)

    def choice(self, group: Adw.PreferencesGroup, key: str, title: str,
               choices: Sequence[tuple[str, str]], subtitle: str = "",
               keywords: Sequence[str] = ()) -> Adw.ActionRow:
        """An action row with a segmented control bound to a setting."""
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        control = self.win.setting_toggle_group(key, choices)
        row.add_suffix(control)
        row.toggle_group = control
        return self.add(group, row, keywords)

    def combo(self, group: Adw.PreferencesGroup, key: str, title: str,
              choices: Sequence[tuple[object, str]], subtitle: str = "",
              keywords: Sequence[str] = ()) -> Adw.ComboRow:
        row = self.win.setting_combo(key, title, choices, subtitle)
        return self.add(group, row, keywords)

    # -- life cycle --------------------------------------------------------------------------

    def refresh(self) -> None:
        """Server data or profiles changed; update what is shown (keep unsaved edits)."""

    def shown(self) -> None:
        """The page became the visible one."""

    def hidden(self) -> None:
        """Another page became visible, or the window closed."""

    def destroy(self) -> None:
        self.hidden()
