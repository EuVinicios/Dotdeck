"""YOUR DOTS › Dots › a Dot › Connections: the MCP servers that give a Dot its tools.

Unlike the rest of the Dot's page, each change here applies on the server at once, as in
the page's own Connections section: there is nothing to Save. Each tool is Always Allow,
Ask Every Time (the Dot asks in the chat before it runs) or Never Allow (off for the Dot),
the words every permission menu of Dotdeck uses (ADR-028). A token goes to the server and
is never shown again.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from gi.repository import Adw, Gtk

from ...core.workspace import Connection, ConnectionTool
from ...i18n import _, ngettext
from .common import confirm, escape

NAME_LIMIT = 40
# The choices of a tool, in the order of its menu.
ALLOWED, ASK_FIRST, BLOCKED = range(3)


def tool_choice(tool: ConnectionTool) -> int:
    if not tool.enabled:
        return BLOCKED
    return ASK_FIRST if tool.requires_approval else ALLOWED


def choice_patch(choice: int) -> dict:
    """What ``PATCH /api/connections/:id/tools/:name`` sends for ``choice``.

    Never Allow keeps "Ask Every Time" as it is, so allowing the tool again restores it.
    """
    if choice == BLOCKED:
        return {"enabled": False}
    return {"enabled": True, "requiresApproval": choice == ASK_FIRST}


def url_problem(url: str) -> str | None:
    """Why the server would refuse ``url`` (it wants http(s) without credentials)."""
    url = url.strip()
    if not url:
        return _("Enter the address of the MCP server.")
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        return _("Use a full address that starts with https:// or http://.")
    if parts.username or parts.password:
        return _("Leave the user and password out of the address; use the token instead.")
    return None


def summary(connection: Connection) -> str:
    """The line under a connection's name: where it is, its tools, its token."""
    parts = [connection.host]
    total = len(connection.tools)
    on = sum(1 for tool in connection.tools if tool.enabled)
    if total:
        parts.append(ngettext("{on} of {n} tool on", "{on} of {n} tools on", total)
                     .format(on=on, n=total))
    if connection.has_token:
        parts.append(_("token saved"))
    return " · ".join(parts)


class ConnectionsGroup(Adw.PreferencesGroup):
    """The Connections of one Dot, loaded when its page is built."""

    def __init__(self, win, profile, dot_id: str, dot_name: Callable[[], str]):
        super().__init__(
            title=_("Connections"),
            description=_("Tools from MCP servers. Each tool runs on its own, asks you in "
                          "the chat first or is blocked. Changes apply right away."))
        self.win = win
        self.profile_id = profile.id
        self.dot_id = dot_id
        self._dot_name = dot_name
        self.connections: list[Connection] = []
        self._rows: list[Gtk.Widget] = []
        self._expanded: set[str] = set()
        self._loading = False
        self._alive = True

        add = Gtk.Button(valign=Gtk.Align.CENTER, visible=False,
                         tooltip_text=_("Give this Dot the tools of an MCP server"))
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("Add Server…")))
        add.add_css_class("flat")
        add.connect("clicked", lambda _b: self.add_server())
        self.add_button = add
        self.set_header_suffix(add)

        # Loading, empty, an older server or an error: one row says which.
        self.status = Adw.ActionRow()
        self.status_spinner = Adw.Spinner(visible=False)
        self.status.add_prefix(self.status_spinner)
        self.status_icon = Gtk.Image(valign=Gtk.Align.CENTER, visible=False)
        self.status.add_prefix(self.status_icon)
        self.retry = Gtk.Button(label=_("Try Again"), valign=Gtk.Align.CENTER, visible=False)
        self.retry.connect("clicked", lambda _b: self.load())
        self.status.add_suffix(self.retry)
        self.add(self.status)
        self.load()

    @property
    def profile(self):
        return self.win.app.profiles.get(self.profile_id)

    def close(self) -> None:
        self._alive = False

    # -- the list --------------------------------------------------------------------------

    def _show_status(self, title: str, subtitle: str = "", icon: str = "",
                     busy: bool = False, retry: bool = False) -> None:
        self.status.set_title(escape(title))
        self.status.set_subtitle(escape(subtitle))
        self.status_spinner.set_visible(busy)
        self.status_icon.set_visible(bool(icon))
        if icon:
            self.status_icon.set_from_icon_name(icon)
        self.retry.set_visible(retry)
        self.status.set_visible(True)

    def load(self) -> None:
        profile = self.profile
        if profile is None:
            return
        self._show_status(_("Loading connections…"), busy=True)
        self.win.with_api(lambda client: client.connections(self.dot_id, self._loaded), profile)

    def _loaded(self, data, error) -> None:
        if not self._alive:
            return
        if error == "unsupported":
            self._clear()
            self.add_button.set_visible(False)
            self._show_status(_("Not on This Server Yet"),
                              _("Update OpenDots to give Dots tools from MCP servers."),
                              "software-update-available-symbolic")
            return
        if error is not None:
            self._clear()
            self.add_button.set_visible(False)
            self._show_status(_("Could Not Load the Connections"), error,
                              "dialog-warning-symbolic", retry=True)
            return
        self.add_button.set_visible(True)
        self.connections = [c for c in map(Connection.from_json, data) if c is not None]
        self._rebuild()

    def _clear(self) -> None:
        for row in self._rows:
            if isinstance(row, Adw.ExpanderRow) and row.get_expanded():
                self._expanded.add(row.connection_id)
            elif isinstance(row, Adw.ExpanderRow):
                self._expanded.discard(row.connection_id)
            self.remove(row)
        self._rows = []

    def _rebuild(self) -> None:
        self._clear()
        if not self.connections:
            self._show_status(_("No Connections Yet"),
                              _("Add an MCP server to give {name} its tools, like GitHub or "
                                "a calendar.").format(name=self._dot_name()),
                              "network-server-symbolic")
            return
        self.status.set_visible(False)
        for connection in self.connections:
            row = self._connection_row(connection)
            self.add(row)
            self._rows.append(row)

    def _connection_row(self, connection: Connection) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=escape(connection.name), subtitle=escape(summary(connection)),
                              expanded=connection.id in self._expanded or bool(connection.error))
        row.connection_id = connection.id
        row.set_subtitle_lines(1)
        row.add_prefix(Gtk.Image(icon_name="network-server-symbolic", valign=Gtk.Align.CENTER))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Refresh the Tools"))
        refresh.add_css_class("flat")
        refresh.update_property([Gtk.AccessibleProperty.LABEL],
                                [_("Refresh the tools of {name}").format(name=connection.name)])
        refresh.connect("clicked", lambda _b: self.refresh(connection.id))
        row.add_suffix(refresh)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove"))
        remove.add_css_class("flat")
        remove.update_property([Gtk.AccessibleProperty.LABEL],
                               [_("Remove {name}").format(name=connection.name)])
        remove.connect("clicked", lambda _b: self.remove_server(connection.id))
        row.add_suffix(remove)

        if connection.error:
            problem = Adw.ActionRow(title=_("The server did not answer as expected"),
                                    subtitle=escape(connection.error))
            problem.set_subtitle_selectable(True)
            icon = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER)
            icon.add_css_class("warning")
            problem.add_prefix(icon)
            row.add_row(problem)
        if not connection.tools:
            empty = Adw.ActionRow(title=_("This server offers no tools."))
            empty.add_css_class("dim-label")
            row.add_row(empty)
        choices = Gtk.StringList.new([_("Always Allow"), _("Ask Every Time"),
                                      _("Never Allow")])
        for tool in connection.tools:
            # "Read-only" leads the subtitle: a badge would push its menu out of line.
            about = " · ".join(([_("Read-only")] if tool.read_only else [])
                               + ([tool.description] if tool.description else []))
            tool_row = Adw.ComboRow(title=escape(tool.title), model=choices,
                                    subtitle=escape(about), selected=tool_choice(tool))
            tool_row.set_title_lines(1)
            tool_row.set_subtitle_lines(2)
            tool_row.tool_name = tool.name
            tool_row.connect("notify::selected", self._on_choice, connection.id, tool.name)
            row.add_row(tool_row)
        return row

    def _row_of(self, connection_id: str) -> Adw.ExpanderRow | None:
        return next((r for r in self._rows if getattr(r, "connection_id", None) == connection_id),
                    None)

    def _connection(self, connection_id: str) -> Connection | None:
        return next((c for c in self.connections if c.id == connection_id), None)

    def _replace(self, data) -> Connection | None:
        updated = Connection.from_json(data)
        if updated is not None:
            self.connections = [updated if c.id == updated.id else c for c in self.connections]
        return updated

    def _failed(self, what: str, error: str) -> None:
        self.win.toast(f"{what}: {error}")     # the server's own error, as it says it

    # -- changes (each one saved at once) --------------------------------------------------

    def _on_choice(self, row: Adw.ComboRow, _pspec, connection_id: str, tool_name: str) -> None:
        if self._loading:
            return
        connection = self._connection(connection_id)
        tool = next((t for t in connection.tools if t.name == tool_name), None) \
            if connection else None
        profile = self.profile
        if tool is None or profile is None or row.get_selected() == tool_choice(tool):
            return
        before = tool_choice(tool)
        row.set_sensitive(False)

        def done(data, error) -> None:
            if not self._alive:
                return
            row.set_sensitive(True)
            if error is not None:
                self._loading = True
                row.set_selected(before)
                self._loading = False
                self._failed(_("{tool} was not changed").format(tool=tool.title), error)
                return
            updated = self._replace(data)
            parent = self._row_of(connection_id)
            if updated is not None and parent is not None:
                parent.set_subtitle(escape(summary(updated)))

        patch = choice_patch(row.get_selected())
        self.win.with_api(lambda client: client.set_connection_tool(
            connection_id, tool_name, done, **patch), profile)

    def refresh(self, connection_id: str) -> None:
        connection = self._connection(connection_id)
        row = self._row_of(connection_id)
        profile = self.profile
        if connection is None or profile is None:
            return
        if row is not None:
            row.set_sensitive(False)

        def done(data, error) -> None:
            if not self._alive:
                return
            if row is not None:
                row.set_sensitive(True)
            if error is not None:
                self._failed(_("{name} was not refreshed").format(name=connection.name), error)
                return
            updated = self._replace(data)
            self._rebuild()
            if updated is not None:
                self.win.toast(ngettext("{name}: {n} tool", "{name}: {n} tools",
                                        len(updated.tools)).format(name=updated.name,
                                                                   n=len(updated.tools)))

        self.win.with_api(lambda client: client.refresh_connection(connection_id, done), profile)

    def remove_server(self, connection_id: str) -> None:
        connection = self._connection(connection_id)
        if connection is None:
            return

        def confirmed(ok: bool) -> None:
            profile = self.profile
            if not ok or profile is None or not self._alive:
                return

            def done(_data, error) -> None:
                if not self._alive:
                    return
                if error is not None:
                    self._failed(_("{name} was not removed").format(name=connection.name),
                                 error)
                    return
                self.connections = [c for c in self.connections if c.id != connection_id]
                self._rebuild()
                self.win.toast(_("{name} removed").format(name=connection.name))

            self.win.with_api(lambda client: client.remove_connection(connection_id, done),
                              profile)

        confirm(self.win, _("Remove {name}?").format(name=connection.name),
                _("{dot} can no longer use its tools, and its token is deleted from the "
                  "server.").format(dot=self._dot_name()),
                _("Remove"), confirmed, destructive=True)

    def add_server(self) -> AddServerDialog:
        dialog = AddServerDialog(self)
        dialog.present(self.win)
        return dialog

    def connect_server(self, name: str, url: str, token: str,
                       done: Callable[[str | None], None]) -> None:
        """Adds the server; ``done(None)`` once it is in the list, else the error."""
        profile = self.profile
        if profile is None:
            done(_("The server is not available."))
            return

        def added(data, error) -> None:
            if not self._alive:
                return
            created = Connection.from_json(data) if error is None else None
            if created is None:
                done(error or _("The server sent an unexpected answer."))
                return
            self.connections.append(created)
            self._expanded.add(created.id)
            self._rebuild()
            self.win.toast(ngettext("{name} connected with {n} tool",
                                    "{name} connected with {n} tools",
                                    len(created.tools)).format(name=created.name,
                                                               n=len(created.tools)))
            done(None)

        self.win.with_api(lambda client: client.add_connection(
            self.dot_id, added, name=name, url=url, token=token), profile)


class AddServerDialog(Adw.Dialog):
    """Name, address and an optional token; Connect lists the server's tools."""

    def __init__(self, group: ConnectionsGroup):
        super().__init__(title=_("Add an MCP Server"), content_width=480)
        self.group = group
        self._busy = False
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        self.connect_button = Gtk.Button(label=_("Connect"), sensitive=False)
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.connect("clicked", lambda _b: self.submit())
        header.pack_end(self.connect_button)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)
        view.add_top_bar(header)
        self.banner = Adw.Banner(use_markup=False)
        view.add_top_bar(self.banner)

        page = Adw.PreferencesPage()
        server = Adw.PreferencesGroup(
            description=_("A name like GitHub and an address like https://example.com/mcp. "
                          "Dotdeck connects and lists the server's tools: read-only ones are "
                          "allowed, the others ask you in the chat before they run. You can "
                          "change each one later."))
        self.name_row = Adw.EntryRow(title=_("Name"), max_length=NAME_LIMIT)
        self.url_row = Adw.EntryRow(title=_("Address"), input_purpose=Gtk.InputPurpose.URL)
        for row in (self.name_row, self.url_row):
            row.connect("changed", lambda _r: self._changed())
            row.connect("entry-activated", lambda _r: self.submit())
            server.add(row)
        page.add(server)
        access = Adw.PreferencesGroup(
            description=_("Only if the server asks for one. It goes to your OpenDots server "
                          "as a bearer token and is never shown again."))
        self.token_row = Adw.PasswordEntryRow(title=_("Access Token (Optional)"))
        self.token_row.connect("entry-activated", lambda _r: self.submit())
        access.add(self.token_row)
        page.add(access)
        view.set_content(page)
        self.set_child(view)
        self.set_focus(self.name_row)

    def problem(self) -> str | None:
        if not " ".join(self.name_row.get_text().split()):
            return _("Give the server a name.")
        return url_problem(self.url_row.get_text())

    def _changed(self) -> None:
        url = self.url_row.get_text().strip()
        if url and url_problem(url):
            self.url_row.add_css_class("error")
        else:
            self.url_row.remove_css_class("error")
        self.connect_button.set_sensitive(not self._busy and self.problem() is None)
        self.banner.set_revealed(False)

    def submit(self) -> None:
        problem = self.problem()
        if self._busy:
            return
        if problem is not None:
            self.banner.set_title(problem)
            self.banner.set_revealed(True)
            return
        self._set_busy(True)
        self.group.connect_server(" ".join(self.name_row.get_text().split()),
                                  self.url_row.get_text().strip(), self.token_row.get_text(),
                                  self._done)

    def _done(self, error: str | None) -> None:
        self._set_busy(False)
        if error is None:
            self.token_row.set_text("")
            self.close()
            return
        self.banner.set_title(_("Could not connect: {error}").format(error=error))
        self.banner.set_revealed(True)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.spinner.set_visible(busy)
        for row in (self.name_row, self.url_row, self.token_row):
            row.set_sensitive(not busy)
        self.connect_button.set_sensitive(not busy and self.problem() is None)
