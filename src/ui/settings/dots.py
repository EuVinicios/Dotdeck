"""YOUR DOTS › Dots: the list of a server's Dots and the page that edits one.

A Dot's name, job, instructions, permissions, Spaces and Automatic Learning live on the
server and are saved together with an explicit Save (``PUT /api/dots/:id`` replaces the
whole Dot). Its Connections (connections.py) and call voice (dot_voice.py) are saved on
the server one change at a time.
Its pet, pinned and hidden flags and the Chief choice are Dotdeck's own and apply at
once. The job has no field in OpenDots: it is the ``# Name — job`` first line of the
instructions, the convention the owner's Dots follow.
"""

from __future__ import annotations

import importlib
import logging
import re
from typing import Callable

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from ...core import dotprefs, monitor
from ...core.workspace import LEARNING_CONTAINER, LEARNING_CONTAINER_LIMIT, Dot, with_job
from ...i18n import _, ngettext
from .common import SettingsPage, badge, escape
from .connections import ConnectionsGroup
from .dot_voice import DotVoiceGroup

log = logging.getLogger(__name__)

NAME_LIMIT = 40
INSTRUCTIONS_MIN = 3
INSTRUCTIONS_LIMIT = 2000
LEARNING_GUIDE = "https://docs.copilotkit.ai/learning"
_HEADER = re.compile(r"^#+\s*(?P<name>.+?)\s+[—–-]\s+(?P<job>.+)$")


def optional(module: str, attribute: str) -> Callable | None:
    """A function from another part of Dotdeck that may not exist yet (pet gallery…)."""
    try:
        found = importlib.import_module(f"..{module}", __package__)
    except Exception as error:      # missing, or half-written by its author
        log.debug("No %s.%s: %s", module, attribute, error)
        return None
    value = getattr(found, attribute, None)
    return value if callable(value) else None


def split_job(name: str, instructions: str) -> tuple[str, str]:
    """(job, the rest) when the instructions start with ``# <name> — <job>``."""
    lines = instructions.splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return "", instructions
    match = _HEADER.match(lines[first].strip())
    if not match or match.group("name").strip().casefold() != name.strip().casefold():
        return "", instructions
    rest = lines[first + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return match.group("job").strip(), "\n".join(rest)


def join_job(name: str, body: str, job: str) -> str:
    """The instructions to save: the job header (if any) and the body."""
    job = " ".join(job.split())
    if not job:
        return body
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if first.startswith("#") and _HEADER.match(first):
        # The body starts with another "# X — Y" heading; with_job would replace it.
        return f"# {name} — {job}\n\n{body}"
    return with_job(name, body, job)


def chief_of(app, profile) -> str | None:
    mon = app.monitors.get(profile.id) if profile else None
    names = [d.name for d in mon.workspace.dots] if mon is not None else []
    return monitor.chief_dot(profile, names or (list(mon.dots) if mon else [])) \
        if profile else None


def server_problem(app, profile, mon, purpose: str | None = None,
                   waiting: str | None = None) -> tuple[str, str, str] | None:
    """(icon, title, description) when a server page cannot show its server.

    ``purpose`` says what adding a server is for and ``waiting`` what comes back once
    an offline server answers (both default to the Dots page's wording).
    """
    if profile is None:
        return ("network-server-symbolic", _("No Server Yet"),
                purpose or _("Add an OpenDots server to see and edit its Dots."))
    if mon is None or mon.status == "unknown":
        return ("content-loading-symbolic", _("Connecting…"),
                _("Dotdeck is reaching {name}.").format(name=profile.name))
    if mon.status == "offline":
        return ("network-offline-symbolic", _("{name} Is Offline").format(name=profile.name),
                waiting or _("Dotdeck keeps trying. The Dots show up as soon as the server "
                             "answers."))
    if mon.status == "token":
        return ("dialog-password-symbolic", _("Access Token Needed"),
                _("{name} needs a valid owner access token.").format(name=profile.name))
    if mon.status == "local-only":
        return ("network-server-symbolic", _("Only Reachable From Its Own Computer"),
                _("Change the server's address in Servers."))
    if mon.status != "online":
        return ("dialog-warning-symbolic", _("Not an OpenDots Server"),
                _("Check the server's address in Servers."))
    return None


class ProblemGroup(Adw.PreferencesGroup):
    """A status page inside a preferences page, with an optional action button."""

    def __init__(self):
        super().__init__()
        self.status = Adw.StatusPage(vexpand=True)
        self.status.add_css_class("compact")
        self._button = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self._button.add_css_class("pill")
        self._button.connect("clicked", lambda _b: self._action and self._action())
        self._action: Callable[[], None] | None = None
        self.status.set_child(self._button)
        self.add(self.status)

    def show(self, problem: tuple[str, str, str] | None, button: str = "",
             action: Callable[[], None] | None = None) -> None:
        self.set_visible(problem is not None)
        if problem is None:
            return
        icon, title, description = problem
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(escape(description))   # markup, unlike the title
        self._action = action
        self._button.set_label(button)
        self._button.set_visible(bool(button and action))

    def show_server(self, win, problem: tuple[str, str, str], profile, mon) -> None:
        """A server problem, with the button that fixes it (add or edit the server)."""
        if profile is None:
            self.show(problem, _("Add Server…"), lambda: open_servers(win))
        elif mon is not None and mon.status in ("token", "local-only", "other"):
            self.show(problem, _("Edit Server…"), lambda: open_servers(win, profile.id))
        else:
            self.show(problem)


def open_servers(win, profile_id: str | None = None) -> None:
    """The Servers page with the Add Server form, or the page of ``profile_id``."""
    win.show_page("servers")
    if profile_id is None:
        win.pages["servers"].add_server()
    else:
        win.pages["servers"].edit_server(profile_id)


class DotsPage(SettingsPage):
    name = "dots"
    title = _("Dots")
    icon = "system-users-symbolic"

    def build(self) -> None:
        self._signature = None
        self._rows: list[Gtk.Widget] = []
        self._prefs: dotprefs.DotPrefs | None = None
        self._prefs_handler = 0
        self._pending_dot: str | None = None

        top = self.group()
        self.selector = self.add(top, self.win.server_selector(), ("server", "switch"))
        self.selector.bind_property("visible", top, "visible",
                                    GObject.BindingFlags.SYNC_CREATE)
        self.problem = ProblemGroup()
        self.widget.add(self.problem)

        self.list = self.group(_("Dots"))
        self._create_dot = optional("new_dot", "create_dot")
        new = Gtk.Button(valign=Gtk.Align.CENTER, visible=self._create_dot is not None,
                         tooltip_text=_("Create a Dot on this server"))
        new.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("New Dot…")))
        new.add_css_class("flat")
        new.connect("clicked", lambda _b: self.new_dot())
        self.new_button = new
        self.list.set_header_suffix(new)
        self.refresh()

    # -- data ------------------------------------------------------------------------------

    def _watch_prefs(self, profile) -> None:
        wanted = dotprefs.for_profile(profile.id) if profile else None
        if wanted is self._prefs:
            return
        if self._prefs is not None and self._prefs_handler:
            self._prefs.disconnect(self._prefs_handler)
        self._prefs, self._prefs_handler = wanted, 0
        if wanted is not None:
            self._prefs_handler = wanted.connect("changed", lambda _p: self.win.schedule_refresh())

    def refresh(self) -> None:
        profile = self.win.profile
        mon = self.win.monitor
        self._watch_prefs(profile)
        problem = server_problem(self.app, profile, mon)
        if problem is not None:
            self._signature = None
            self._clear()
            self.list.set_visible(False)
            self.problem.show_server(self.win, problem, profile, mon)
            return
        workspace = mon.workspace
        dots = list(workspace.dots)
        if not dots:
            self._signature = None
            self._clear()
            self.list.set_visible(False)
            self.problem.show(("system-users-symbolic", _("No Dots Yet"),
                               _("Dots are the coworkers on your OpenDots server.")),
                              _("New Dot…") if self._create_dot else "",
                              self.new_dot if self._create_dot else None)
            return
        self.problem.show(None)
        self.list.set_visible(True)
        prefs = self._prefs
        pets = prefs.pets_for(dots)
        chief = chief_of(self.app, profile)
        in_call = workspace.dot_in_call()
        ordered = prefs.ordered(dots) + prefs.hidden(dots)
        state = "offline" if mon.status != "online" else \
            "paused" if mon.state.paused else "idle"
        signature = tuple((d.id, d.name, d.job, pets.get(d.id), prefs.is_pinned(d.id),
                           prefs.is_hidden(d.id), d.name == chief, self._dirty(profile, d.id),
                           in_call is not None and in_call.id == d.id)
                          for d in ordered) + (profile.id, state)
        self.list.set_description(ngettext("{n} Dot on {server}", "{n} Dots on {server}",
                                           len(dots)).format(n=len(dots),
                                                             server=escape(profile.name)))
        if self._pending_dot and workspace.dot(self._pending_dot):
            pending, self._pending_dot = self._pending_dot, None
            GLib.idle_add(lambda: (self.open_dot(pending), False)[1])
        if signature == self._signature:
            return
        self._signature = signature
        self._clear()
        from ..pet import PetAvatar
        for dot in ordered:
            row = Adw.ActionRow(title=escape(dot.name), activatable=True,
                                subtitle=escape(dot.job) or _("No job line yet"))
            row.set_subtitle_lines(1)
            row.set_title_lines(1)
            avatar_state = "call" if in_call is not None and in_call.id == dot.id else state
            avatar = PetAvatar(pets.get(dot.id), size=40, state=avatar_state)
            row.add_prefix(avatar)
            if self._dirty(profile, dot.id):
                row.add_suffix(badge(_("Unsaved"), "warning"))
            if dot.name == chief:
                row.add_suffix(badge(_("Chief"), "accent"))
            if prefs.is_pinned(dot.id):
                pin = Gtk.Image(icon_name="view-pin-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Pinned"))
                pin.add_css_class("dim-label")
                row.add_suffix(pin)
            if prefs.is_hidden(dot.id):
                row.add_suffix(badge(_("Hidden")))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, dot_id=dot.id: self.open_dot(dot_id))
            self.add(self.list, row, ("dot", "edit", "instructions", dot.job))
            self._rows.append(row)

    def _dirty(self, profile, dot_id: str) -> bool:
        editor = self.win.editors.get((profile.id, dot_id))
        return bool(editor and editor.dirty)

    def _clear(self) -> None:
        for row in self._rows:
            self.list.remove(row)
        self._rows = []

    # -- actions ---------------------------------------------------------------------------

    def open_dot(self, dot_id: str) -> None:
        profile = self.win.profile
        mon = self.win.monitor
        if profile is None:
            return
        dot = mon.workspace.dot(dot_id) if mon is not None else None
        if dot is None:
            if mon is None or (mon.status in ("unknown", "online") and not mon.workspace.dots):
                self._pending_dot = dot_id      # opens once the Dots arrive
            else:
                self.win.toast(_("That Dot is not on {name}.").format(name=profile.name))
            return
        key = (profile.id, dot.id)
        editor = self.win.editors.get(key)
        if editor is None:
            editor = DotEditor(self.win, profile, dot)
            self.win.editors[key] = editor
        if self.win.nav.find_page(editor.page.get_tag()) is None:
            self.win.push(editor.page)

    def new_dot(self) -> None:
        profile = self.win.profile
        if profile is None or self._create_dot is None:
            return

        def created(dot_id) -> None:
            mon = self.win.monitor
            if mon is not None:
                mon.poll_now()
            if dot_id:
                self._pending_dot = dot_id
                self.win.schedule_refresh()

        self._create_dot(self.app, profile, self.win, created)

    def destroy(self) -> None:
        if self._prefs is not None and self._prefs_handler:
            self._prefs.disconnect(self._prefs_handler)
        self._prefs, self._prefs_handler = None, 0


class DotEditor:
    """The page of one Dot. Keeps its draft while you look at other pages."""

    def __init__(self, win, profile, dot: Dot):
        self.win = win
        self.app = win.app
        self.profile_id = profile.id
        self.dot = dot
        self.dirty = False
        self._loading = False
        self._busy = False
        self._loaded: tuple | None = None
        self._remote: Dot | None = None
        self._space_ids: list[str] = []
        self._space_checks: dict[str, Gtk.CheckButton] = {}
        self._space_rows: list[Gtk.Widget] = []
        self._prefs = dotprefs.for_profile(profile.id)
        self._prefs_handler = self._prefs.connect("changed", lambda _p: self.refresh_local())
        self._build()
        self.load(dot)

    @property
    def title(self) -> str:
        return self.dot.name

    @property
    def profile(self):
        return self.app.profiles.get(self.profile_id)

    def _workspace(self):
        mon = self.app.monitors.get(self.profile_id)
        return mon.workspace if mon is not None else None

    # -- layout ----------------------------------------------------------------------------

    def _build(self) -> None:
        self.page = Adw.NavigationPage(title=self.dot.name, tag=f"dot-{self.dot.id}")
        self.page.editor = self
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.banner = Adw.Banner(use_markup=False)
        self.banner.connect("button-clicked", lambda _b: self._on_banner())
        view.add_top_bar(self.banner)

        prefs = Adw.PreferencesPage()
        view.set_content(prefs)

        # The pet, the name and the job, like a profile card.
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       halign=Gtk.Align.CENTER)
        hero.add_css_class("dot-hero")
        from ..pet import PetAvatar
        self.avatar = PetAvatar(None, size=96)
        hero.append(self.avatar)
        self.hero_name = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
        self.hero_name.add_css_class("title-1")
        hero.append(self.hero_name)
        self.hero_job = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                  max_width_chars=48)
        self.hero_job.add_css_class("dim-label")
        hero.append(self.hero_job)
        self._choose_pet = optional("pet_gallery", "choose_pet")
        self.pet_button = Gtk.Button(label=_("Change Pet…"), halign=Gtk.Align.CENTER,
                                     margin_top=8, visible=self._choose_pet is not None)
        self.pet_button.add_css_class("pill")
        self.pet_button.connect("clicked", lambda _b: self._change_pet())
        hero.append(self.pet_button)
        top = Adw.PreferencesGroup()
        top.add(hero)
        prefs.add(top)

        identity = Adw.PreferencesGroup(
            title=_("Identity"),
            description=_("The job shows under the name in the sidebar. Dotdeck keeps it in "
                          "the first line of the instructions."))
        self.name_row = Adw.EntryRow(title=_("Name"), max_length=NAME_LIMIT)
        self.name_row.connect("changed", lambda _r: self._changed())
        identity.add(self.name_row)
        self.job_row = Adw.EntryRow(title=_("Job"))
        self.job_row.connect("changed", lambda _r: self._changed())
        identity.add(self.job_row)
        prefs.add(identity)

        instructions = Adw.PreferencesGroup(
            title=_("Instructions"),
            description=_("What this Dot knows and how it works. Up to 2,000 characters, "
                          "counting the job line."))
        self.counter = Gtk.Label(valign=Gtk.Align.END)
        self.counter.add_css_class("counter")
        self.counter.add_css_class("dim-label")
        instructions.set_header_suffix(self.counter)
        self.buffer = Gtk.TextBuffer()
        self.buffer.connect("changed", lambda _b: self._changed())
        self.text = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 accepts_tab=False, top_margin=12, bottom_margin=12,
                                 left_margin=14, right_margin=14)
        self.text.add_css_class("instructions-view")
        self.text.update_property([Gtk.AccessibleProperty.LABEL], [_("Instructions")])
        scrolled = Gtk.ScrolledWindow(child=self.text, min_content_height=240,
                                      max_content_height=420, propagate_natural_height=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        card = Gtk.Box(overflow=Gtk.Overflow.HIDDEN)
        card.append(scrolled)
        scrolled.set_hexpand(True)
        card.add_css_class("card")
        card.add_css_class("settings-card")
        instructions.add(card)
        prefs.add(instructions)

        permissions = Adw.PreferencesGroup(title=_("Permissions"))
        self.research_row = Adw.SwitchRow(title=_("Web Research"))
        self.research_row.connect("notify::active", lambda *_: self._changed())
        permissions.add(self.research_row)
        self.memory_row = Adw.SwitchRow(title=_("Memories"))
        self.memory_row.connect("notify::active", lambda *_: self._changed())
        permissions.add(self.memory_row)
        prefs.add(permissions)

        self.spaces = Adw.PreferencesGroup(
            title=_("Spaces"),
            description=_("The Spaces this Dot can read and write. New chats start in the "
                          "default one."))
        self.default_space = Adw.ComboRow(title=_("Default Space"))
        self.default_space.connect("notify::selected", lambda *_: self._changed())
        prefs.add(self.spaces)

        # Everything the page's own Dot form had, so it never has to open (ADR-023).
        self.connections = ConnectionsGroup(self.win, self.profile, self.dot.id,
                                            lambda: self.dot.name)
        prefs.add(self.connections)

        # Its voice in calls, saved at once like the Connections (fork servers only).
        self.voice = DotVoiceGroup(self.win, self.profile, self.dot.id)
        prefs.add(self.voice)

        self.learning = Adw.PreferencesGroup(
            title=_("Automatic Learning"),
            description=_("New conversations add what they learn to a container of your "
                          "Intelligence project; create it there first. Leave the ID empty "
                          "to keep new conversations out. Existing ones keep theirs."))
        self.container_row = Adw.EntryRow(title=_("Container ID"),
                                          max_length=LEARNING_CONTAINER_LIMIT)
        self.container_row.connect("changed", lambda _r: self._learning_changed())
        self.learning.add(self.container_row)
        self.skills_row = Adw.SwitchRow(
            title=_("Use Published Skills"),
            subtitle=_("Loads the reviewed skills of the container. Turn on delivery in "
                       "Intelligence too."))
        self.skills_row.connect("notify::active", lambda *_: self._changed())
        self.learning.add(self.skills_row)
        guide = Adw.ActionRow(title=_("How to Set Up Learning"), activatable=True)
        guide.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        guide.connect("activated", lambda _r: self.win.open_url(LEARNING_GUIDE))
        self.learning.add(guide)
        prefs.add(self.learning)

        local = Adw.PreferencesGroup(
            title=_("In Dotdeck"),
            description=_("Kept on this computer and applied right away."))
        self.chief_row = Adw.SwitchRow(title=_("Chief"))
        self.chief_row.connect("notify::active", self._on_chief)
        local.add(self.chief_row)
        self.pin_row = Adw.SwitchRow(title=_("Pin to Top"),
                                     subtitle=_("First in the sidebar"))
        self.pin_row.connect("notify::active", self._on_pinned)
        local.add(self.pin_row)
        self.hide_row = Adw.SwitchRow(title=_("Hide From the Sidebar"),
                                      subtitle=_("Hidden Dots wait at the end of the list"))
        self.hide_row.connect("notify::active", self._on_hidden)
        local.add(self.hide_row)
        prefs.add(local)

        # Save and Discard appear when something changed.
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _b: self.save())
        self.spinner = Adw.Spinner(visible=False)
        discard = Gtk.Button(label=_("Discard"))
        discard.connect("clicked", lambda _b: self.discard())
        self.bar_label = Gtk.Label(label=_("Unsaved changes"), ellipsize=Pango.EllipsizeMode.END)
        self.bar_label.add_css_class("dim-label")
        self.bar = Gtk.ActionBar(revealed=False)
        self.bar.pack_start(self.bar_label)
        self.bar.pack_end(self.save_button)
        self.bar.pack_end(discard)
        self.bar.pack_end(self.spinner)
        view.add_bottom_bar(self.bar)
        self.page.set_child(view)

    # -- loading and comparing -------------------------------------------------------------

    def load(self, dot: Dot) -> None:
        """Show the server's version of the Dot; forgets the draft."""
        self._loading = True
        try:
            self.dot = dot
            self._remote = None
            job, body = split_job(dot.name, dot.instructions)
            self.name_row.set_text(dot.name)
            self.job_row.set_text(job)
            # Not an undo step: Ctrl+Z right after opening must not empty the text.
            self.buffer.begin_irreversible_action()
            self.buffer.set_text(body)
            self.buffer.end_irreversible_action()
            self.research_row.set_active(dot.research_allowed)
            self.memory_row.set_active(dot.memory_allowed)
            self._load_spaces(dot)
            self.learning.set_visible(dot.has_learning)
            self.container_row.set_text(dot.learning_container)
            self.skills_row.set_active(dot.skill_delivery)
            self.skills_row.set_sensitive(bool(dot.learning_container))
            self.banner.set_revealed(False)
        finally:
            self._loading = False
        self._loaded = self._fields()
        self.refresh_local()
        self._changed()

    def _load_spaces(self, dot: Dot) -> None:
        for row in self._space_rows:
            self.spaces.remove(row)
        self._space_rows, self._space_checks = [], {}
        workspace = self._workspace()
        spaces = list(workspace.spaces) if workspace is not None else []
        editable = bool(spaces) and ("spaceIds" in dot.raw or "spaceId" in dot.raw)
        self.spaces.set_visible(editable)
        if not editable:
            self._space_ids = []
            return
        chosen = set(dot.space_ids or ((dot.space_id,) if dot.space_id else ()))
        for space in spaces:
            check = Gtk.CheckButton(active=space.id in chosen, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda *_: self._spaces_toggled())
            row = Adw.ActionRow(title=escape(space.name), activatable_widget=check,
                                subtitle=escape(" ".join(space.description.split())[:90]))
            row.set_subtitle_lines(1)
            row.add_prefix(check)
            self.spaces.add(row)
            self._space_rows.append(row)
            self._space_checks[space.id] = check
        self.spaces.add(self.default_space)
        self._space_rows.append(self.default_space)
        self._sync_default_space(dot.space_id)

    def _checked_spaces(self) -> list[str]:
        return [space_id for space_id, check in self._space_checks.items() if check.get_active()]

    def _sync_default_space(self, wanted: str | None = None) -> None:
        workspace = self._workspace()
        current = wanted if wanted is not None else (
            self._space_ids[self.default_space.get_selected()]
            if 0 <= self.default_space.get_selected() < len(self._space_ids) else "")
        ids = self._checked_spaces()
        self._space_ids = ids
        names = []
        for space_id in ids:
            space = workspace.space(space_id) if workspace is not None else None
            names.append(space.name if space else space_id)
        loading, self._loading = self._loading, True
        try:
            self.default_space.set_model(Gtk.StringList.new(names))
            if current in ids:
                self.default_space.set_selected(ids.index(current))
            elif ids:
                self.default_space.set_selected(0)
            self.default_space.set_sensitive(len(ids) > 1)
        finally:
            self._loading = loading

    def _spaces_toggled(self) -> None:
        if self._loading:
            return
        checked = self._checked_spaces()
        if not checked:
            # A Dot needs at least one Space: keep the last one ticked.
            loading, self._loading = self._loading, True
            first = next(iter(self._space_checks.values()), None)
            default = self._default_space_id() or (next(iter(self._space_checks), ""))
            check = self._space_checks.get(default) or first
            if check is not None:
                check.set_active(True)
            self._loading = loading
            self.win.toast(_("A Dot needs at least one Space"))
        self._sync_default_space()
        self._changed()

    def _default_space_id(self) -> str:
        index = self.default_space.get_selected()
        return self._space_ids[index] if 0 <= index < len(self._space_ids) else ""

    def _fields(self) -> tuple:
        start, end = self.buffer.get_bounds()
        return (self.name_row.get_text(), self.job_row.get_text(),
                self.buffer.get_text(start, end, False), self.research_row.get_active(),
                self.memory_row.get_active(), tuple(self._checked_spaces()),
                self._default_space_id(), self.container_row.get_text().strip(),
                self.skills_row.get_active())

    def instructions(self) -> str:
        """The instructions as they would be saved now."""
        name, job, body = self._fields()[:3]
        loaded = self._loaded
        if loaded is not None and (name, job, body) == loaded[:3]:
            return self.dot.instructions      # untouched: keep the server's exact text
        return join_job(" ".join(name.split()), body, job)

    def changes(self) -> dict:
        """What Save sends: the full set the server wants (see Dot.update_body)."""
        fields = self._fields()
        changes = {"name": " ".join(fields[0].split()), "instructions": self.instructions(),
                   "researchAllowed": fields[3], "memoryAllowed": fields[4]}
        if self.spaces.get_visible() and fields[5]:
            changes["spaceIds"] = list(fields[5])
            changes["spaceId"] = fields[6] or fields[5][0]
        loaded = self._loaded
        # Untouched Learning settings go back exactly as the server sent them.
        if self.learning.get_visible() and (loaded is None or fields[7:9] != loaded[7:9]):
            changes["learningContainerId"] = fields[7] or None
            changes["skillDeliveryEnabled"] = bool(fields[7]) and fields[8]
        return changes

    def problems(self) -> list[str]:
        name = " ".join(self.name_row.get_text().split())
        size = len(self.instructions())
        found = []
        if not name:
            found.append(_("The name is empty."))
        elif len(name) > NAME_LIMIT:
            found.append(_("The name is longer than 40 characters."))
        if size < INSTRUCTIONS_MIN:
            found.append(_("The instructions need at least 3 characters."))
        elif size > INSTRUCTIONS_LIMIT:
            found.append(_("The instructions are {n} characters over the limit.").format(
                n=size - INSTRUCTIONS_LIMIT))
        if not self._container_ok():
            found.append(_("The container ID can only have lowercase letters, numbers and "
                           "single hyphens."))
        return found

    def _container_ok(self) -> bool:
        container = self.container_row.get_text().strip()
        return not container or bool(LEARNING_CONTAINER.match(container))

    def _learning_changed(self) -> None:
        has_container = bool(self.container_row.get_text().strip())
        self.skills_row.set_sensitive(has_container)
        if not has_container and not self._loading:
            self.skills_row.set_active(False)    # the server refuses skills without one
        self._changed()

    def _changed(self) -> None:
        if self._loading:
            return
        size = len(self.instructions())
        self.counter.set_label(f"{size} / {INSTRUCTIONS_LIMIT}")
        if size > INSTRUCTIONS_LIMIT:
            self.counter.add_css_class("error")
            self.counter.remove_css_class("dim-label")
        else:
            self.counter.remove_css_class("error")
            self.counter.add_css_class("dim-label")
        name = " ".join(self.name_row.get_text().split()) or self.dot.name
        self.hero_name.set_label(name)
        job = " ".join(self.job_row.get_text().split()) or self.dot.job
        self.hero_job.set_label(job)
        self.hero_job.set_visible(bool(job))
        self.page.set_title(name)
        dirty = self._fields() != self._loaded
        problems = self.problems() if dirty else []
        if dirty and self.name_row.get_text().strip() == "":
            self.name_row.add_css_class("error")
        else:
            self.name_row.remove_css_class("error")
        if self._container_ok():
            self.container_row.remove_css_class("error")
        else:
            self.container_row.add_css_class("error")
        self.save_button.set_sensitive(dirty and not problems and not self._busy
                                       and self._workspace_has_dot())
        self.bar_label.set_label(problems[0] if problems else _("Unsaved changes"))
        self.bar.set_revealed(dirty)
        if dirty != self.dirty:
            self.dirty = dirty
            self.win.schedule_refresh()      # the list shows an "Unsaved" badge

    def _workspace_has_dot(self) -> bool:
        workspace = self._workspace()
        return workspace is None or not workspace.dots or workspace.dot(self.dot.id) is not None

    # -- Dotdeck's own choices (instant) ----------------------------------------------------

    def refresh_local(self) -> None:
        profile = self.profile
        if profile is None:
            return
        loading, self._loading = self._loading, True
        try:
            self.avatar.set_property("pet", self._prefs.pet(self.dot.id) or "")
            self.pin_row.set_active(self._prefs.is_pinned(self.dot.id))
            self.hide_row.set_active(self._prefs.is_hidden(self.dot.id))
            self.chief_row.set_active(profile.chief == self.dot.name)
            automatic = chief_of(self.app, profile)
            if profile.chief == self.dot.name:
                subtitle = _("Answers “Talk to the Chief” in the tray and with Ctrl+Shift+C")
            elif profile.chief:
                subtitle = _("Now {name} answers “Talk to the Chief”").format(
                    name=profile.chief)
            elif automatic == self.dot.name:
                subtitle = _("The chief for now, picked automatically. Turn on to keep it.")
            else:
                subtitle = _("Now picked automatically: {name}").format(name=automatic or "—")
            self.chief_row.set_subtitle(escape(subtitle))
            mon = self.app.monitors.get(self.profile_id)
            server = mon.state if mon is not None else None
            self.research_row.set_subtitle(
                _("Off for the whole server, in Privacy") if server is not None
                and not server.research_allowed else _("Can search the web and read pages"))
            self.memory_row.set_subtitle(
                _("Off for the whole server, in Privacy") if server is not None
                and not server.memory_allowed else _("Can save and use memories about you"))
        finally:
            self._loading = loading

    def _on_chief(self, row, _pspec) -> None:
        if self._loading:
            return
        profile = self.profile
        if profile is None:
            return
        profile.chief = self.dot.name if row.get_active() else ""
        self.app.profiles.update(profile)
        self.refresh_local()
        self.win.schedule_refresh()

    def _on_pinned(self, row, _pspec) -> None:
        if not self._loading:
            self._prefs.set_pinned(self.dot.id, row.get_active())

    def _on_hidden(self, row, _pspec) -> None:
        if not self._loading:
            self._prefs.set_hidden(self.dot.id, row.get_active())

    def _change_pet(self) -> None:
        profile = self.profile
        if self._choose_pet is None or profile is None:
            return
        self._choose_pet(self.app, profile, self.dot, self.win,
                         lambda *_args: self.refresh_local())

    # -- server updates --------------------------------------------------------------------

    def refresh(self) -> None:
        """New data from the server: follow it unless there is a draft."""
        workspace = self._workspace()
        self.refresh_local()
        if workspace is None or not workspace.dots:
            return
        dot = workspace.dot(self.dot.id)
        if dot is None:
            self._show_banner(_("This Dot is no longer on the server."), "")
            self._changed()
            return
        if dot == self.dot and dot.raw == self.dot.raw:
            if self._remote is None and self.banner.get_revealed() and \
                    self.banner.get_button_label() == "":
                self.banner.set_revealed(False)
            return
        if not self.dirty:
            self.load(dot)
            return
        if self._remote is None or self._remote != dot:
            self._remote = dot
            self._show_banner(_("{name} was changed somewhere else. Saving replaces those "
                                "changes.").format(name=dot.name), _("Show Theirs"))

    def _show_banner(self, text: str, button: str) -> None:
        self.banner.set_title(text)
        self.banner.set_button_label(button or None)
        self.banner.set_revealed(True)

    def _on_banner(self) -> None:
        if self._remote is not None:
            self.load(self._remote)
        else:
            self.banner.set_revealed(False)

    # -- saving ----------------------------------------------------------------------------

    def discard(self) -> None:
        self.load(self._remote or self.dot)

    def save(self, done: Callable[[bool], None] | None = None) -> None:
        problems = self.problems()
        profile = self.profile
        if problems or profile is None or self._busy:
            if problems:
                self.win.toast(problems[0])
            if done:
                done(False)
            return
        changes = self.changes()
        old_name = self.dot.name
        base = self.dot
        self._set_busy(True)

        def finished(data, error) -> None:
            if self.win._closing:        # the window went away meanwhile: only refresh
                mon = self.app.monitors.get(self.profile_id)
                if mon is not None and error is None:
                    mon.poll_now()
                if done:
                    done(error is None)
                return
            self._set_busy(False)
            if error is not None:
                self._show_banner(_("Could not save: {error}").format(error=error), "")
                self.win.toast(_("{name} was not saved").format(name=old_name))
                self._changed()
                if done:
                    done(False)
                return
            saved = Dot.from_json(data) if isinstance(data, dict) else None
            if saved is None:
                saved = Dot.from_json({**base.raw, **changes, "id": base.id}) or base
            current = self.profile
            if current is not None and saved.name != old_name and current.chief == old_name:
                current.chief = saved.name
                self.app.profiles.update(current)
            self.load(saved)
            self.win.toast(_("{name} saved").format(name=saved.name))
            mon = self.app.monitors.get(self.profile_id)
            if mon is not None:
                mon.poll_now()
            if done:
                done(True)

        self.win.with_api(lambda client: client.update_dot(base, finished, **changes), profile)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.spinner.set_visible(busy)
        self.save_button.set_sensitive(not busy)
        self.name_row.set_sensitive(not busy)

    def close(self) -> None:
        self.connections.close()
        self.voice.close()
        if self._prefs_handler:
            self._prefs.disconnect(self._prefs_handler)
            self._prefs_handler = 0
