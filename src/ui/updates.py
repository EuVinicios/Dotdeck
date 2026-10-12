"""Preferences group that checks the official OpenDots and updates the server."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import config
from ..core import updates
from ..core.store import Profile
from ..i18n import _, ngettext

DOCS = f"{config.REPOSITORY}/blob/main/server/README.md"


def _date(iso: str) -> str:
    value = GLib.DateTime.new_from_iso8601(iso, None) if iso else None
    return value.to_local().format("%x") if value else ""


def _files(paths: list[str], shown: int = 3) -> str:
    names = ", ".join(paths[:shown])
    rest = len(paths) - shown
    if rest <= 0:
        return names
    return ngettext("{files} and {n} more", "{files} and {n} more", rest).format(files=names,
                                                                                 n=rest)


def _blocked_text(found: dict) -> str:
    """Why the update cannot run, one sentence per reason (see updates.blockers)."""
    lines = []
    if found["branch"]:
        current, expected = found["branch"]
        lines.append(_("The server is on the branch {branch}, not {expected}.").format(
            branch=current, expected=expected) if current else
            _("The server is not on the branch {expected}.").format(expected=expected))
    if found["untracked"]:
        lines.append(_("Files that are not in git would be overwritten: {files}.").format(
            files=_files(found["untracked"])))
    if found["conflicts"]:
        n = len(found["conflicts"])
        lines.append(ngettext("The official changes conflict with your local commits in "
                              "{n} file: {files}.",
                              "The official changes conflict with your local commits in "
                              "{n} files: {files}.", n).format(n=n,
                                                               files=_files(found["conflicts"])))
    lines.append(_("Fix it on the server, then check again."))
    return "\n".join(lines)


def _step(step: str) -> str:
    return {
        "fetch": _("Downloading the news…"),
        "merge": _("Merging with your local commits…"),
        "install": _("Installing dependencies…"),
        "checks": _("Running the OpenDots tests…"),
        "build": _("Building…"),
        "restart": _("Restarting OpenDots…"),
        "health": _("Checking that OpenDots is back…"),
        "backup": _("Saving the backup…"),
    }.get(step, _("Restoring the previous version…") if step.startswith("rollback")
          else _("Updating…"))


class UpdatesGroup(Adw.PreferencesGroup):
    __gtype_name__ = "DotdeckUpdatesGroup"

    def __init__(self, app, profile: Profile, toast):
        super().__init__(title=_("OpenDots Updates"),
                         description=_("Compare this server with the official project and "
                                       "update it here. Your local changes are kept, and a "
                                       "failed update is undone automatically."))
        self._app = app
        self._profile = profile
        self._toast = toast
        self._poll = 0
        self._busy = False

        self.missing = Adw.ActionRow(
            title=_("Updater Not Found"), visible=False, subtitle_lines=4,
            subtitle=_("Install the updater next to OpenDots to check and apply updates from "
                       "Dotdeck.") if updates.on_this_machine(profile) else
            _("Install the updater next to OpenDots. It only answers on the server itself: to "
              "update from this device, reach it through an SSH tunnel or set it up to listen "
              "on the network, then enter its address below."))
        docs = Gtk.Button(label=_("How to Install"), valign=Gtk.Align.CENTER)
        docs.add_css_class("flat")
        docs.connect("clicked", lambda _b: app.window.open_external(DOCS))
        self.missing.add_suffix(docs)
        self.address = Adw.EntryRow(title=_("Updater address (optional)"), visible=False,
                                    text=profile.updater_url, show_apply_button=True)
        self.address.connect("apply", self._on_address)

        self.current = Adw.ActionRow(title=_("Installed Version"), subtitle_selectable=True)
        self.news = Adw.ExpanderRow(title=_("Official Project"))
        self._news_rows: list[Gtk.Widget] = []
        self.local = Adw.ActionRow(title=_("Local Commits"))
        # File and branch names are shown as they are, not as markup.
        self.blocked = Adw.ActionRow(title=_("Update Blocked"), subtitle_lines=6,
                                     subtitle_selectable=True, use_markup=False)
        warning = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER)
        warning.add_css_class("warning")
        self.blocked.add_prefix(warning)
        self.job = Adw.ActionRow(title=_("Last Update"), visible=False, subtitle_lines=4,
                                 use_markup=False)
        self._spinner = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.job.add_prefix(self._spinner)
        self.check_button = Adw.ButtonRow(title=_("Check Now"),
                                          start_icon_name="view-refresh-symbolic")
        self.check_button.connect("activated", lambda _r: self.check())
        self.update_button = Adw.ButtonRow(title=_("Update Server…"),
                                           start_icon_name="software-update-available-symbolic")
        self.update_button.add_css_class("suggested-action")
        self.update_button.connect("activated", lambda _r: self._confirm())
        for row in (self.missing, self.address, self.current, self.news, self.local,
                    self.blocked, self.job, self.check_button, self.update_button):
            self.add(row)
            row.set_visible(False)        # until the first status arrives
        self.connect("unrealize", lambda *_: self._stop_polling())

        mon = app.monitors.get(profile.id)
        if mon is not None and mon.updates is not None:
            self.show_status(mon.updates)
        self.refresh()

    # -- data ------------------------------------------------------------------------------

    def _client(self, callback) -> None:
        profile = self._app.profiles.get(self._profile.id) or self._profile
        self._app.tokens.lookup(profile.id, profile.url,
                                lambda token: callback(updates.UpdaterClient(profile, token)))

    def refresh(self) -> None:
        self._client(lambda client: client.status(self._got))

    def check(self) -> None:
        self._set_busy(True)
        self._client(lambda client: client.check(self._checked))

    def _checked(self, data, error) -> None:
        self._set_busy(False)
        self._got(data, error)
        if data is not None:
            behind = int(data.get("behind") or 0)
            self._toast(ngettext("{n} change in the official OpenDots",
                                 "{n} changes in the official OpenDots", behind).format(n=behind)
                        if behind else _("OpenDots is up to date"))

    def _got(self, data, error) -> None:
        mon = self._app.monitors.get(self._profile.id)
        if mon is not None:
            mon.set_updates(data or {})
        self.show_status(data or {})

    def show_status(self, data: dict) -> None:
        installed = bool(data)
        self.missing.set_visible(not installed)
        self.address.set_visible(not installed or bool(self._profile.updater_url))
        for row in (self.current, self.news, self.local, self.check_button, self.update_button):
            row.set_visible(installed)
        if not installed:
            self.job.set_visible(False)
            self.blocked.set_visible(False)
            return
        current = data.get("current") or {}
        self.current.set_subtitle(" · ".join(x for x in (
            current.get("short", ""), current.get("subject", ""),
            _date(current.get("date", ""))) if x))
        behind = int(data.get("behind") or 0)
        for row in self._news_rows:
            self.news.remove(row)
        self._news_rows = []
        if behind:
            self.news.set_subtitle(ngettext("{n} change to install", "{n} changes to install",
                                            behind).format(n=behind))
            for item in data.get("news") or []:
                row = Adw.ActionRow(title=GLib.markup_escape_text(item.get("subject", "")),
                                    subtitle=f"{item.get('short', '')} · {_date(item.get('date', ''))}")
                self.news.add_row(row)
                self._news_rows.append(row)
        else:
            upstream = data.get("upstream") or {}
            self.news.set_subtitle(_("Up to date (official version of {date})").format(
                date=_date(upstream.get("date", ""))) if upstream else _("Up to date"))
        self.news.set_enable_expansion(bool(behind))
        ahead = int(data.get("ahead") or 0)
        self.local.set_subtitle(ngettext("{n} commit of your own, kept when updating",
                                         "{n} commits of your own, kept when updating",
                                         ahead).format(n=ahead) if ahead else _("None"))
        job = data.get("job") or {}
        state = job.get("state", "idle")
        running = state == updates.RUNNING
        self.job.set_visible(state != "idle")
        self._spinner.set_visible(running)
        if running:
            self.job.set_subtitle(_step(job.get("step", "")))
        elif state == "done":
            self.job.set_subtitle(_("Finished on {date}: {message}").format(
                date=_date(job.get("finished_at", "")), message=job.get("message", "")))
        elif state == "rolled-back":
            self.job.set_subtitle(_("The update failed and the previous version was restored. "
                                    "{message}").format(message=job.get("message", "")))
        elif state == "failed":
            self.job.set_subtitle(_("Could not update: {message}").format(
                message=job.get("message", "")))
        # The end of what a failed command printed (the updater's tests, for example).
        output = job.get("output") if state in ("failed", "rolled-back") else None
        self.job.set_tooltip_text("\n".join(str(line) for line in output[-20:])
                                  if isinstance(output, list) and output else None)
        found = updates.blockers(data)
        # Uncommitted changes keep their own wording on the button (and no warning row).
        shown = bool(behind) and (found["branch"] or found["untracked"] or found["conflicts"])
        self.blocked.set_visible(bool(shown))
        if shown:
            self.blocked.set_subtitle(_blocked_text(found))
        self.update_button.set_sensitive(bool(behind) and not running and not found["any"]
                                         and not self._busy)
        if found["dirty"]:
            self.update_button.set_title(_("Update Server… (uncommitted changes on the server)"))
        elif shown:
            self.update_button.set_title(_("Update Server… (blocked, see above)"))
        else:
            self.update_button.set_title(_("Update Server…"))
        if running and not self._poll:
            self._poll = GLib.timeout_add_seconds(2, self._on_poll)
        elif not running:
            self._stop_polling()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.check_button.set_sensitive(not busy)

    def _on_address(self, row) -> None:
        profile = self._app.profiles.get(self._profile.id)
        if profile is not None:
            profile.updater_url = row.get_text().strip()
            self._app.profiles.update(profile)
            self._profile = profile
        self.refresh()

    # -- update ----------------------------------------------------------------------------

    def _confirm(self) -> None:
        mon = self._app.monitors.get(self._profile.id)
        data = mon.updates if mon else {}
        behind = int((data or {}).get("behind") or 0)
        dialog = Adw.AlertDialog(
            heading=_("Update {name}?").format(name=self._profile.name),
            body=ngettext(
                "{n} official change will be merged with your local commits. OpenDots "
                "restarts, which takes about one or two minutes; calls in progress end. If "
                "anything fails, the previous version comes back by itself.",
                "{n} official changes will be merged with your local commits. OpenDots "
                "restarts, which takes about one or two minutes; calls in progress end. If "
                "anything fails, the previous version comes back by itself.",
                behind).format(n=behind))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("update", _("Update"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "update" and self._update())
        dialog.present(self.get_root())

    def _update(self) -> None:
        self.update_button.set_sensitive(False)

        def started(data, error):
            if error:
                self._toast(_("Could not start the update: {error}").format(error=error))
                self.refresh()
                return
            self._got(data, None)
            if not self._poll:
                self._poll = GLib.timeout_add_seconds(2, self._on_poll)

        self._client(lambda client: client.update(started))

    def _on_poll(self) -> bool:
        def got(data, error):
            if data is None:  # OpenDots and the updater can be busy restarting
                return
            job = data.get("job") or {}
            self._got(data, None)
            if job.get("state") != updates.RUNNING:
                self._toast({"done": _("OpenDots was updated"),
                             "rolled-back": _("The update failed; the previous version is back"),
                             }.get(job.get("state"), _("The update did not finish")))
                self._app.reload_server(self._profile.id)

        self._client(lambda client: client.status(got))
        return GLib.SOURCE_CONTINUE

    def _stop_polling(self) -> None:
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0
