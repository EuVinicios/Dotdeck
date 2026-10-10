"""New Dot: meet a new teammate, from a template or from scratch.

Templates prefill the name, the job and the instructions. OpenDots has no job field,
so the job travels as the first line of the instructions (``# Name — job``), the same
convention the sidebar reads. The new Dot gets a pet nobody else has, shown live while
the owner types, and the Dot is created with ``POST /api/dots``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..core import api, dotprefs, pets  # noqa: E402
from ..i18n import N_, _  # noqa: E402
from .pet import PetAvatar  # noqa: E402
from .pet_gallery import ensure_css, install_css, pet_owners, present, show_toast  # noqa: E402

log = logging.getLogger(__name__)

NAME_LIMIT = 40             # the server's limits (src/server/workspace-routes.ts upstream)
INSTRUCTIONS_LIMIT = 2000
INSTRUCTIONS_MIN = 3
HERO_SIZE = 88
WATCHDOG_SECONDS = 30       # for the keyring; once sent, the HTTP session times out (12 s)

_DASH = re.compile(r"\s+[—–-]\s+\S")


@dataclass(frozen=True)
class Template:
    id: str
    title: str          # the chip (N_)
    icon: str
    summary: str        # one line under the chips (N_)
    name: str = ""      # prefilled fields (N_), empty for Blank
    job: str = ""
    body: str = ""


TEMPLATES: tuple[Template, ...] = (
    Template(
        "chief", N_("Chief of Staff"), "x-office-calendar-symbolic",
        N_("Plans your week, follows up on loose ends and coordinates the other Dots."),
        N_("Chief of Staff"), N_("keeps your week on track"),
        N_("You are my chief of staff. Help me see what matters today: deadlines, meetings, "
           "open loops and decisions waiting on me. Turn loose requests into clear next steps "
           "with an owner and a date, and draft short replies for me to approve. When a "
           "request fits another Dot better, say which one and why. Keep a running list of "
           "decisions and follow-ups, and remind me of anything that is slipping. Be brief and "
           "practical, and ask before doing anything that cannot be undone.")),
    Template(
        "researcher", N_("Researcher"), "edit-find-symbolic",
        N_("Finds reliable sources, compares them and tells you what is certain."),
        N_("Researcher"), N_("finds and checks sources for you"),
        N_("You are my research assistant. When I ask a question, look for reliable and recent "
           "sources, compare what they say and point out where they disagree. Start with a "
           "short answer, then the evidence, with a link for every claim. Prefer primary "
           "sources, official data and peer-reviewed work over opinion pieces. Say clearly when "
           "something is uncertain, out of date or could not be verified, and suggest what "
           "would settle it.")),
    Template(
        "writer", N_("Writer"), "document-edit-symbolic",
        N_("Drafts and polishes emails, posts and documents in your voice."),
        N_("Writer"), N_("drafts and polishes your writing"),
        N_("You help me write. Draft emails, posts, documents and messages in my voice: clear, "
           "warm and direct. When it is not obvious, ask who the text is for and what it should "
           "achieve. Offer one strong version instead of many options, keep it as short as it "
           "can be, and flag anything that could be misread. When I share a draft, edit it "
           "lightly, keep my style and explain the main changes in a sentence or two.")),
    Template(
        "finance", N_("Finance"), "accessories-calculator-symbolic",
        N_("Tracks spending, bills and budgets, and warns you before due dates."),
        N_("Finance"), N_("tracks spending, bills and budgets"),
        N_("You keep an eye on my money. Help me track spending, bills, subscriptions and "
           "budgets, and warn me early about due dates and unusual charges. Explain numbers in "
           "plain language, with simple tables when they help. Never move money, buy anything "
           "or share financial details; describe the step and let me do it. When something "
           "depends on tax rules or regulations, say so and suggest checking with a "
           "professional.")),
    Template(
        "home", N_("Home & Infrastructure"), "go-home-symbolic",
        N_("Looks after the house, the homelab, backups and repairs."),
        N_("Caretaker"), N_("looks after the house and the homelab"),
        N_("You look after my home and my homelab. Keep track of maintenance, warranties, "
           "backups and the health of my servers and devices. When something breaks, find the "
           "cause step by step, starting with the safest checks. Explain every command before "
           "suggesting it and never run anything destructive without my explicit approval. "
           "Keep a short log of what changed and why, so we can always undo it.")),
    Template(
        "study", N_("Study Partner"), "accessories-dictionary-symbolic",
        N_("Explains ideas simply, quizzes you and keeps a study plan."),
        N_("Study Partner"), N_("helps you learn and practice"),
        N_("You are my study partner. Help me learn at a steady pace: explain ideas simply, "
           "then check my understanding with short questions. Build small study plans, "
           "flashcards and practice exercises, and adapt to what I get wrong. Be encouraging "
           "but honest about mistakes. When I am preparing for an exam or a course, keep track "
           "of the topics we covered and what is still left.")),
    Template(
        "blank", N_("Blank"), "document-new-symbolic",
        N_("Start from scratch with your own name, job and instructions.")),
)

BY_ID = {template.id: template for template in TEMPLATES}
BLANK = BY_ID["blank"]

_CSS = """
button.dot-template {
  border-radius: 999px;
  padding: 5px 14px 5px 12px;
  min-height: 24px;
}
button.dot-template:checked {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
}
.dot-instructions {
  border-radius: 12px;
}
.dot-instructions:focus-within {
  outline: 2px solid color-mix(in srgb, var(--accent-color) 50%, transparent);
  outline-offset: -2px;
}
.dot-instructions textview,
.dot-instructions textview > text {
  background: none;
}
.dot-instructions .dot-header-line {
  font-family: monospace;
  font-size: 0.9em;
}
.dot-instructions .dot-counter.warning {
  color: var(--warning-color);
  opacity: 1;
}
.dot-instructions .dot-counter.error,
.dot-hint.error {
  color: var(--error-color);
  opacity: 1;
}
.pet-stage .dot-hero-name {
  font-size: 18pt;
  font-weight: 800;
}
"""


# -- pure helpers (unit tested) -------------------------------------------------------------

def js_length(text: str) -> int:
    """Length as the server counts it (JavaScript strings are UTF-16)."""
    return len(text.encode("utf-16-le")) // 2


def names_dot(line: str, name: str) -> bool:
    """Is ``line`` a ``# <name> — <job>`` header for the Dot called ``name``?"""
    line = line.strip()
    if not line.startswith("#") or not name.strip():
        return False
    text, key = line.lstrip("#").strip().casefold(), name.strip().casefold()
    return text.startswith(key) and _DASH.match(text[len(key):]) is not None


def compose(name: str, job: str, body: str) -> str:
    """The instructions sent to the server: ``# Name — job``, a blank line, then the body.

    A header for this Dot already at the top of the body is replaced, never doubled; any
    other first line (a Markdown heading such as ``# Tone - be kind``) is kept as written.
    """
    name, job, body = name.strip(), job.strip(), body.strip()
    if not job or not name:
        return body
    header = f"# {name} — {job}"
    lines = body.splitlines()
    if lines and names_dot(lines[0], name):
        body = "\n".join(lines[1:]).strip()
    return f"{header}\n\n{body}" if body else header


def template_fields(template: Template) -> tuple[str, str, str]:
    """The translated name, job and body a template prefills."""
    return tuple(_(text) if text else "" for text in (template.name, template.job,
                                                       template.body))


def template_instructions(template: Template) -> str:
    return compose(*template_fields(template))


def problems(name: str, instructions: str, space_id: str | None,
             taken_names: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """What stops the Dot from being created, per field (``name``, ``instructions``,
    ``space``); empty when it is ready."""
    found: dict[str, str] = {}
    clean = name.strip()
    if not clean:
        found["name"] = _("Give this Dot a name.")
    elif js_length(clean) > NAME_LIMIT:
        found["name"] = _("Names can have up to {limit} characters.").format(limit=NAME_LIMIT)
    elif clean.casefold() in {n.casefold() for n in taken_names}:
        found["name"] = _("Another Dot is already called “{name}”.").format(name=clean)
    size = js_length(instructions.strip())
    if size < INSTRUCTIONS_MIN:
        found["instructions"] = _("Tell this Dot what it does and how it should work.")
    elif size > INSTRUCTIONS_LIMIT:
        found["instructions"] = _("{count} characters over the limit.").format(
            count=size - INSTRUCTIONS_LIMIT)
    if not space_id:
        found["space"] = _("Choose a Space.")
    return found


def free_pet(name: str, job: str, taken: set[str]) -> str:
    """The pet a new Dot gets: the best fit nobody on this server has yet."""
    return pets.suggest(name, job, taken if len(taken) < len(pets.PETS) else frozenset()).id


# -- the dialog ----------------------------------------------------------------------------

class NewDotDialog(Adw.Dialog):
    """``done(dot_id)`` once the Dot exists, ``done(None)`` if the owner gives up."""

    __gtype_name__ = "DotdeckNewDotDialog"

    def __init__(self, app, profile, done: Callable[[str | None], None] | None = None):
        super().__init__(title=_("New Dot"), content_width=600, content_height=760,
                         width_request=360, height_request=420)
        ensure_css()
        install_css("new-dot", _CSS)
        self._app = app
        self._profile = profile
        self._done = done
        self._finished = False
        self._busy = False
        self._watchdog = 0
        self._request = 0
        self._reverting = False
        self._syncing = False
        self._monitor = (getattr(app, "monitors", None) or {}).get(profile.id)
        self.prefs = dotprefs.for_profile(profile.id)
        self.template = BLANK
        self._prefill = ("", "", "")
        self.taken: set[str] = set()
        self.taken_names: set[str] = set()
        self.spaces: list = []
        self.pet_id = pets.DEFAULT.id
        self.issues: dict[str, str] = {}
        self.chips: dict[str, Gtk.ToggleButton] = {}
        self.alert: Adw.AlertDialog | None = None
        self.presented_on: Gtk.Widget | None = None
        self._build()
        self._load_server()
        self._update()
        self._handlers = [(self.prefs, self.prefs.connect("changed", self._on_server_change))]
        if self._monitor is not None:
            for signal, handler in (("workspace-changed", self._on_server_change),
                                    ("changed", self._on_monitor_changed)):
                try:
                    self._handlers.append((self._monitor, self._monitor.connect(signal,
                                                                                handler)))
                except TypeError:  # a monitor without that signal (older or fake)
                    log.debug("monitor has no %s signal", signal)
        self.connect("closed", self._on_closed)

    # -- building --------------------------------------------------------------------------

    def _build(self) -> None:
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel_button = Gtk.Button(label=_("Cancel"), use_underline=True)
        self.cancel_button.connect("clicked", lambda _b: self.close())
        header.pack_start(self.cancel_button)
        self.create_button = Gtk.Button()
        self.create_button.add_css_class("suggested-action")
        self._create_stack = Gtk.Stack(hhomogeneous=True)
        self._create_stack.add_named(Gtk.Label(label=_("Create")), "label")
        self._create_stack.add_named(Adw.Spinner(), "busy")
        self.create_button.set_child(self._create_stack)
        self.create_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Create")])
        self.create_button.connect("clicked", lambda _b: self.create())
        header.pack_end(self.create_button)
        self.set_default_widget(self.create_button)

        self.banner = Adw.Banner(revealed=False, use_markup=False)

        page = Adw.PreferencesPage()
        page.add(self._hero_group())
        page.add(self._template_group())
        page.add(self._identity_group())
        page.add(self._instructions_group())
        page.add(self._space_group())
        self._page = page

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.set_content(page)
        self.set_child(view)
        self.set_focus(self.name_row)

        shortcut = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        shortcut.add_shortcut(Gtk.Shortcut(
            trigger=Gtk.ShortcutTrigger.parse_string("<Control>Return"),
            action=Gtk.CallbackAction.new(lambda *_a: (self.create(), True)[1])))
        self.add_controller(shortcut)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 460sp"))
        narrow.add_setter(self.hero_avatar, "size", 64)
        narrow.add_setter(self.hero, "spacing", 14)
        self.add_breakpoint(narrow)

    def _hero_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        self.hero = Gtk.Box(spacing=20)
        self.hero.add_css_class("pet-stage")
        self.hero_avatar = PetAvatar(self.pet_id, size=HERO_SIZE)
        self.hero_avatar.update_state([Gtk.AccessibleState.HIDDEN], [True])
        self.hero.append(self.hero_avatar)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                        valign=Gtk.Align.CENTER)
        self.hero_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.hero_name.add_css_class("dot-hero-name")
        self.hero_job = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.hero_job.add_css_class("dim-label")
        self.hero_pet = Gtk.Label(xalign=0, wrap=True, margin_top=6)
        self.hero_pet.add_css_class("caption")
        for label in (self.hero_name, self.hero_job, self.hero_pet):
            words.append(label)
        self.hero.append(words)
        group.add(self.hero)
        return group

    def _template_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Start From a Template"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        chips = Adw.WrapBox(child_spacing=8, line_spacing=8)
        chips.update_property([Gtk.AccessibleProperty.LABEL], [_("Templates")])
        first = None
        for template in TEMPLATES:
            chip = Gtk.ToggleButton(child=Adw.ButtonContent(icon_name=template.icon,
                                                            label=_(template.title)))
            chip.add_css_class("dot-template")
            chip.set_tooltip_text(_(template.summary))
            if first is None:
                first = chip
            else:
                chip.set_group(first)
            chip.connect("toggled", self._on_chip, template)
            self.chips[template.id] = chip
            chips.append(chip)
        self._reverting = True
        self.chips[BLANK.id].set_active(True)
        self._reverting = False
        self.template_summary = Gtk.Label(label=_(BLANK.summary), xalign=0, wrap=True)
        self.template_summary.add_css_class("dim-label")
        box.append(chips)
        box.append(self.template_summary)
        group.add(box)
        return group

    def _identity_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Name and Job"),
            description=_("The job shows under the name in the sidebar."))
        self.name_row = Adw.EntryRow(title=_("Name"), max_length=NAME_LIMIT,
                                     activates_default=True)
        self.job_row = Adw.EntryRow(title=_("Job"), activates_default=True)
        self.name_row.connect("changed", lambda _r: self._update())
        self.job_row.connect("changed", lambda _r: self._update())
        group.add(self.name_row)
        group.add(self.job_row)
        self.name_hint = Gtk.Label(xalign=0, wrap=True, visible=False, margin_top=6,
                                   margin_start=12)
        self.name_hint.add_css_class("caption")
        self.name_hint.add_css_class("dot-hint")
        self.name_hint.add_css_class("error")
        group.add(self.name_hint)
        return group

    def _instructions_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Instructions"),
            description=_("What this Dot does and how it works. The model reads them at the "
                          "start of every conversation."))
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.add_css_class("dot-instructions")
        self.header_line = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                     margin_top=12, margin_start=14, margin_end=14,
                                     selectable=False)
        self.header_line.add_css_class("dot-header-line")
        self.header_line.add_css_class("dim-label")
        self.header_line.set_tooltip_text(
            _("Added for you from the name and the job, so the job shows in the sidebar."))
        card.append(self.header_line)

        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                 top_margin=10, bottom_margin=10, left_margin=14,
                                 right_margin=14)
        self.text.update_property([Gtk.AccessibleProperty.LABEL], [_("Instructions")])
        self.buffer = self.text.get_buffer()
        self.buffer.connect("changed", lambda _b: self._update())
        self.placeholder = Gtk.Label(
            label=_("Describe what this Dot does, how it should work and what it must never "
                    "do."),
            xalign=0, yalign=0, wrap=True, can_target=False, halign=Gtk.Align.FILL,
            valign=Gtk.Align.START, margin_top=10, margin_start=14, margin_end=14)
        self.placeholder.add_css_class("dim-label")
        self.placeholder.update_state([Gtk.AccessibleState.HIDDEN], [True])
        overlay = Gtk.Overlay(child=self.text)
        overlay.add_overlay(self.placeholder)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      min_content_height=180, max_content_height=340,
                                      propagate_natural_height=True)
        scroller.set_child(overlay)
        card.append(scroller)

        footer = Gtk.Box(spacing=12, margin_start=14, margin_end=14, margin_bottom=10,
                         margin_top=2)
        self.instructions_hint = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.instructions_hint.add_css_class("caption")
        self.instructions_hint.add_css_class("dot-hint")
        self.counter = Gtk.Label(xalign=1, valign=Gtk.Align.END)
        self.counter.add_css_class("caption")
        self.counter.add_css_class("numeric")
        self.counter.add_css_class("dot-counter")
        footer.append(self.instructions_hint)
        footer.append(self.counter)
        card.append(footer)
        group.add(card)
        return group

    def _space_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Space and Permissions"),
            description=_("The Space is where this Dot reads and saves pages."))
        self.space_model = Gtk.StringList()
        self.space_row = Adw.ComboRow(title=_("Space"), model=self.space_model,
                                      use_subtitle=False, subtitle_lines=2)
        self.space_row.connect("notify::selected", lambda *_a: self._update())
        # The same words as the Dot editor in Settings, so both read alike.
        self.research_row = Adw.SwitchRow(title=_("Web Research"), active=True)
        self.memory_row = Adw.SwitchRow(title=_("Memories"), active=True)
        group.add(self.space_row)
        group.add(self.research_row)
        group.add(self.memory_row)
        return group

    # -- server data -----------------------------------------------------------------------

    def _load_server(self) -> None:
        """Spaces, names and pets in use, from the monitor (refreshed when it changes)."""
        workspace = self._monitor.workspace if self._monitor is not None else None
        dots = workspace.dots if workspace is not None else ()
        self.taken_names = {d.name for d in dots}
        self._syncing = True  # assigning missing pets saves, which says "changed"
        try:
            self.taken = set(pet_owners(self.prefs, dots)) if dots else set()
        finally:
            self._syncing = False

        spaces = list(workspace.spaces) if workspace is not None else []
        if spaces != self.spaces:
            chosen = self.space_id
            self.spaces = spaces
            self.space_model.splice(0, self.space_model.get_n_items(),
                                    [s.name for s in spaces])
            index = next((i for i, s in enumerate(spaces) if s.id == chosen), 0)
            if spaces:
                self.space_row.set_selected(index)
        self._refresh_permissions()

    def _refresh_permissions(self) -> None:
        """Say so when the whole server has research or memories turned off."""
        state = getattr(self._monitor, "state", None)
        off = _("Off for the whole server, in Privacy")
        self.research_row.set_subtitle(
            off if state is not None and not state.research_allowed
            else _("Can search the web and read pages"))
        self.memory_row.set_subtitle(
            off if state is not None and not state.memory_allowed
            else _("Can save and use memories about you"))

    def _on_server_change(self, *_args) -> None:
        if self._finished or self._busy or self._syncing:
            return
        self._load_server()
        self._update()

    def _on_monitor_changed(self, *_args) -> None:
        """Every poll: the server's permissions and whether it is reachable."""
        if self._finished:
            return
        self._refresh_permissions()
        self._update_space_subtitle()

    @property
    def space_id(self) -> str | None:
        index = self.space_row.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self.spaces):
            return None
        return self.spaces[index].id

    # -- fields ----------------------------------------------------------------------------

    def body(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(),
                                    False)

    def instructions(self) -> str:
        return compose(self.name_row.get_text(), self.job_row.get_text(), self.body())

    def _fields(self) -> tuple[str, str, str]:
        return (self.name_row.get_text().strip(), self.job_row.get_text().strip(),
                self.body().strip())

    def _dirty(self) -> bool:
        """Did the owner write a job or instructions a template would throw away?

        The name does not count: a name the owner typed survives a template change.
        """
        _name, job, body = self._fields()
        return bool(job or body) and (job, body) != tuple(t.strip() for t in self._prefill[1:])

    def _on_chip(self, chip: Gtk.ToggleButton, template: Template) -> None:
        if self._reverting or not chip.get_active() or template.id == self.template.id:
            return
        if not self._dirty():
            self.use_template(template.id)
            return
        alert = Adw.AlertDialog(
            heading=_("Replace What You Wrote?"),
            body=_("The {template} template replaces the job and the instructions you "
                   "wrote.").format(template=_(template.title)))
        alert.add_response("keep", _("Keep Mine"))
        alert.add_response("replace", _("Replace"))
        alert.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("keep")
        alert.set_close_response("keep")

        def answered(_alert, response):
            if response == "replace":
                self.use_template(template.id)
            else:
                self._reverting = True
                self.chips[self.template.id].set_active(True)
                self._reverting = False

        alert.connect("response", answered)
        self.alert = alert
        alert.present(self)

    def use_template(self, template_id: str) -> None:
        """Prefill the fields from a template (no questions asked).

        A name the owner typed is kept; one that came from the last template is replaced.
        """
        template = BY_ID[template_id]
        typed = self.name_row.get_text().strip()
        keep_name = bool(typed) and typed != self._prefill[0].strip()
        self.template = template
        name, job, body = template_fields(template)
        self._prefill = (name, job, body)
        if not self.chips[template.id].get_active():
            self._reverting = True
            self.chips[template.id].set_active(True)
            self._reverting = False
        self.template_summary.set_label(_(template.summary))
        if not keep_name:
            self.name_row.set_text(name)
        self.job_row.set_text(job)
        self.buffer.set_text(body)
        self._update()

    def _update(self) -> None:
        name, job, body = self._fields()
        instructions = compose(name, job, body)
        size = js_length(instructions)

        self.counter.set_label(f"{size}/{INSTRUCTIONS_LIMIT}")
        for style in ("warning", "error", "dim-label"):
            self.counter.remove_css_class(style)
        self.counter.add_css_class("error" if size > INSTRUCTIONS_LIMIT else
                                   "warning" if size > INSTRUCTIONS_LIMIT * 0.9 else "dim-label")
        header = f"# {name} — {job}" if name and job else ""
        self.header_line.set_label(header)
        self.header_line.set_visible(bool(header))
        self.placeholder.set_visible(not self.buffer.get_char_count())

        self.pet_id = free_pet(name, job, self.taken)
        pet = pets.get(self.pet_id) or pets.DEFAULT
        self.hero_avatar.set_property("pet", pet.id)
        for other in pets.PETS:
            self.hero.remove_css_class(f"pet-{other.id}")
        self.hero.add_css_class(f"pet-{pet.id}")
        self.hero_name.set_label(name or _("New Dot"))
        self.hero_job.set_label(job or _("No job yet"))
        self.hero_pet.set_label(_("{pet} will be its pet. You can change it later.").format(
            pet=_(pet.name)))

        self.issues = problems(name, instructions, self.space_id, self.taken_names)
        # Only shout about the name once there is one; an empty form is not an error.
        name_issue = self.issues.get("name") if name else None
        self.name_hint.set_label(name_issue or "")
        self.name_hint.set_visible(bool(name_issue))
        if name_issue:
            self.name_row.add_css_class("error")
        else:
            self.name_row.remove_css_class("error")
        # An empty box has its placeholder; speak up once there is text that will not do.
        text_issue = self.issues.get("instructions", "") if body else ""
        self.instructions_hint.set_label(text_issue)
        if size > INSTRUCTIONS_LIMIT:
            self.instructions_hint.add_css_class("error")
            self.instructions_hint.remove_css_class("dim-label")
        else:
            self.instructions_hint.remove_css_class("error")
            self.instructions_hint.add_css_class("dim-label")
        self._update_space_subtitle()
        self.create_button.set_sensitive(self.ready)

    def _update_space_subtitle(self) -> None:
        if not self.spaces:
            status = getattr(self._monitor, "status", "unknown")
            if status == "online":
                text = _("This server has no Spaces yet. Create one in the web page first.")
            elif status == "unknown":
                text = _("Waiting for the server…")
            elif status == "offline":
                text = _("The server is not answering right now.")
            else:
                text = _("Could not read the Spaces from this server.")
            self.space_row.set_subtitle(text)
        else:
            space = self.spaces[self.space_row.get_selected()] if self.space_id else None
            self.space_row.set_subtitle(GLib.markup_escape_text(space.description)
                                        if space else "")

    @property
    def ready(self) -> bool:
        return not self.issues and not self._busy

    # -- creating --------------------------------------------------------------------------

    def create(self) -> None:
        if self._busy or self._finished:
            return  # Ctrl+Enter or the focused button again while the request runs
        self._update()
        if not self.ready:
            return
        name = self.name_row.get_text().strip()
        body = {"name": name, "instructions": self.instructions(), "space_id": self.space_id,
                "research_allowed": self.research_row.get_active(),
                "memory_allowed": self.memory_row.get_active()}
        pet_id = self.pet_id
        self._request += 1
        request = self._request
        self._set_busy(True)
        self.banner.set_revealed(False)
        self._watchdog = GLib.timeout_add_seconds(WATCHDOG_SECONDS, self._too_slow)

        def send(client) -> None:
            # A token that turns up after the owner was told it failed must not create a
            # Dot behind their back (they may have tried again, or cancelled).
            if request != self._request or self._finished or not self._busy:
                log.info("New Dot: the token came too late; nothing was sent")
                return
            # From here on the HTTP session's own timeout always answers.
            self._stop_watchdog()
            client.create_dot(lambda data, error: self._created(request, data, error, name,
                                                                pet_id), **body)

        api.for_profile(self._app, self._profile, send)

    def _stop_watchdog(self) -> None:
        if self._watchdog:
            GLib.source_remove(self._watchdog)
            self._watchdog = 0

    def _set_busy(self, busy: bool) -> None:
        if busy:
            # Nothing focused may turn insensitive under the keyboard: park the focus first.
            self.set_focus(self.create_button)
        self._busy = busy
        self._create_stack.set_visible_child_name("busy" if busy else "label")
        self._page.set_sensitive(not busy)
        self.cancel_button.set_sensitive(not busy)
        self.set_can_close(not busy)
        # While busy the button keeps its colour (with a spinner) but ignores clicks.
        self.create_button.set_can_target(not busy)
        self.create_button.set_sensitive(busy or self.ready)
        self.create_button.update_property([Gtk.AccessibleProperty.LABEL],
                                           [_("Creating…") if busy else _("Create")])

    def _too_slow(self) -> bool:
        """Never leave the dialog stuck: give the controls back if the keyring never answers.

        Nothing was sent yet (the watchdog stops once the request goes out), and the late
        token is ignored, so trying again cannot create the Dot twice.
        """
        self._watchdog = 0
        if self._busy:
            self._request += 1
            self._set_busy(False)
            self._show_error(_("Could not read the access token for this server. Check that "
                               "the keyring is unlocked and try again."))
        return GLib.SOURCE_REMOVE

    def _show_error(self, text: str) -> None:
        self.banner.set_title(text)
        self.banner.set_revealed(True)
        self._load_server()  # changes that came in while we waited
        self._update()
        self.set_focus(self.create_button if self.ready else self.name_row)

    def _created(self, request: int, data, error: str | None, name: str, pet_id: str) -> None:
        self._stop_watchdog()
        if self._finished or request != self._request:
            return
        self._set_busy(False)
        if error is not None:
            log.info("creating a Dot failed: %s", error)
            self._show_error(_("Could not create the Dot: {error}").format(error=error))
            return
        new_id = data.get("id") if isinstance(data, dict) else None
        new_id = new_id if isinstance(new_id, str) and new_id else None
        if new_id:
            self._give_pet(new_id, pet_id)
        if self._monitor is not None:
            self._monitor.poll_now()
        self._finish(new_id)
        self.close()
        show_toast(self._app, self.presented_on, _("{name} is ready").format(name=name))

    def _give_pet(self, dot_id: str, pet_id: str) -> None:
        """Keep the promise of the preview, unless another Dot got that pet meanwhile."""
        workspace = self._monitor.workspace if self._monitor is not None else None
        others = [d.id for d in workspace.dots if d.id != dot_id] if workspace else []
        if pet_id in self.taken or any(self.prefs.pet(other) == pet_id for other in others):
            return  # automatic assignment gives it a free one with the next poll
        self._syncing = True
        try:
            self.prefs.set_pet(dot_id, pet_id)
        finally:
            self._syncing = False

    def _finish(self, value: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._done is not None:
            try:
                self._done(value)
            except Exception:
                log.exception("new Dot callback failed")

    def _on_closed(self, _dialog) -> None:
        self._stop_watchdog()
        self._request += 1
        self._finish(None)
        for obj, handler in self._handlers:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        self._handlers = []


def create_dot(app, profile, parent: Gtk.Widget | None,
               done: Callable[[str | None], None] | None = None) -> NewDotDialog:
    """Show the New Dot dialog for ``profile``; ``done(new_dot_id | None)`` at the end."""
    dialog = NewDotDialog(app, profile, done)
    dialog.presented_on = parent if parent is not None else getattr(app, "window", None)
    present(dialog, app, parent)
    return dialog
