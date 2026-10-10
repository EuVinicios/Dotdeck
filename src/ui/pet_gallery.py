"""The pet gallery: which plush pet a Dot wears, with a live preview and safe swaps.

``choose_pet(app, profile, dot, parent, done)`` shows every pet as a tile. A pet that
already belongs to another Dot says so ("With Assistente"); choosing it asks first and then
swaps the two pets, so no two Dots ever look the same. The choice is kept on this
computer by ``dotprefs`` (OpenDots has no field for it).
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..core import dotprefs, pets  # noqa: E402
from ..core.workspace import Dot  # noqa: E402
from ..i18n import N_, _  # noqa: E402
from .pet import PetAvatar  # noqa: E402

log = logging.getLogger(__name__)

PREVIEW_SIZE = 128
TILE_SIZE = 72

# The states the preview can play, so the owner sees how the pet looks at work.
STATES = (("idle", N_("Idle")), ("working", N_("Working")), ("speaking", N_("Speaking")),
          ("paused", N_("Paused")))

# One line of personality per pet, shown under its name in the preview.
TAGLINES = {
    "sky": N_("Headset on, always ready to coordinate."),
    "sprout": N_("Grows a little every day."),
    "ember": N_("Quick, curious and great with people."),
    "plum": N_("Tinkers until it works."),
    "sunny": N_("Keeps every cent in line."),
    "rosy": N_("Always finds the right words."),
    "graphite": N_("Reads the fine print."),
    "cocoa": N_("Keeps the home warm and running."),
    "snow": N_("Plans the trip and checks the weather."),
    "lilac": N_("Loves learning something new."),
    "sage": N_("Digs deep into the research."),
    "bolt": N_("Fixes things before they break."),
}

_CSS = """
.pet-stage {
  border-radius: 18px;
  padding: 18px 22px;
}
.pet-stage .pet-stage-name {
  font-size: 20pt;
  font-weight: 800;
}
flowbox.pet-grid > flowboxchild {
  border-radius: 16px;
  padding: 10px 4px 8px;
  transition: background-color 120ms ease-out, box-shadow 120ms ease-out;
}
flowbox.pet-grid > flowboxchild:hover {
  background-color: alpha(currentColor, 0.06);
}
flowbox.pet-grid > flowboxchild:selected {
  background-color: color-mix(in srgb, var(--accent-bg-color) 16%, transparent);
  box-shadow: inset 0 0 0 2px var(--accent-color);
  color: inherit;
}
flowbox.pet-grid > flowboxchild .pet-tile-name {
  font-weight: 600;
}
flowbox.pet-grid > flowboxchild .pet-tile-caption.current {
  color: var(--accent-color);
  font-weight: 700;
  opacity: 1;
}
.pet-swap-note {
  color: var(--warning-color);
  font-weight: 600;
}
.pet-current-note {
  color: var(--accent-color);
  font-weight: 600;
}
"""

_installed: set[tuple[str, str]] = set()


def install_css(key: str, css: str) -> None:
    """Add a stylesheet to the default display once (``key`` names it)."""
    display = Gdk.Display.get_default()
    if display is None or (display.get_name(), key) in _installed:
        return
    _installed.add((display.get_name(), key))
    provider = Gtk.CssProvider()
    try:
        provider.load_from_string(css)
    except (AttributeError, TypeError):  # GTK < 4.12
        provider.load_from_data(css, -1)
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def stage_tint(pet: pets.Pet) -> tuple[float, float, float]:
    """The colour behind a pet on the stage; near-white fur gets a cool winter blue.

    The same rule as the avatar tiles (``pet.tile_colour``), so stage and tile agree.
    """
    r, g, b = pet.fur
    if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.85:
        return (0.42, 0.56, 0.78)
    return pet.fur


def ensure_css() -> None:
    """The gallery styles, with a tint in each pet's colour for the preview stage."""
    tints = []
    for pet in pets.PETS:
        r, g, b = (round(c * 255) for c in stage_tint(pet))
        tints.append(f".pet-stage.pet-{pet.id} {{ background-image: linear-gradient("
                     f"135deg, rgba({r},{g},{b},0.24), rgba({r},{g},{b},0.07)); }}")
    install_css("pet-gallery", _CSS + "\n".join(tints))


def _accepts(function: Callable, count: int) -> bool:
    """Can ``function`` be called with ``count`` positional arguments?"""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):  # a C function without a signature: assume it can
        return True
    try:
        signature.bind(*range(count))
    except TypeError:
        return False
    return True


def _call_toast(target, text: str, button: str | None, action) -> bool:
    """Use ``target.toast`` if it can show this toast (with its button when there is one)."""
    method = getattr(target, "toast", None)
    if not callable(method):
        return False
    if button and action:
        if not _accepts(method, 3):
            return False  # e.g. the settings window: toast(text) only
        method(text, button, action)
    else:
        method(text) if _accepts(method, 1) else method(text, None, None)
    return True


def show_toast(app, parent: Gtk.Widget | None, text: str, button: str | None = None,
               action: Callable[[], None] | None = None) -> None:
    """A toast on the window that opened us (main window, settings window or dialog).

    Never raises: a toast is a courtesy, and the dialogs call this right before closing.
    """
    try:
        _show_toast(app, parent, text, button, action)
    except Exception:
        log.exception("could not show the toast %r", text)


def _add_toast(holder, text: str, button: str | None, action) -> None:
    toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
    if button and action:
        toast.set_button_label(button)
        toast.connect("button-clicked", lambda _t: action())
    holder.add_toast(toast)


def _show_toast(app, parent, text, button, action) -> None:
    # 1. The closest overlay around the parent (a preferences dialog has its own, on top).
    holder = None
    if parent is not None:
        for kind in (Adw.ToastOverlay, Adw.PreferencesDialog):
            holder = parent if isinstance(parent, kind) else parent.get_ancestor(kind)
            if holder is not None:
                _add_toast(holder, text, button, action)
                return
    # 2. The window's own toast(), when it can show this toast (main window).
    root = parent.get_root() if parent is not None else None
    if root is not None and _call_toast(root, text, button, action):
        return
    # 3. Any overlay in that window (the settings window has toast(text) only).
    if root is not None:
        holder = next((w for w in _descendants(root) if isinstance(w, Adw.ToastOverlay)), None)
        if holder is not None:
            _add_toast(holder, text, button, action)
            return
    window = getattr(app, "window", None)
    if window is not None and window is not root:
        _call_toast(window, text, button, action) or _call_toast(window, text, None, None)


def _descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def present(dialog: Adw.Dialog, app, parent: Gtk.Widget | None) -> None:
    dialog.present(parent if parent is not None else getattr(app, "window", None))


# -- who has which pet ---------------------------------------------------------------------

def workspace_of(app, profile):
    monitor = (getattr(app, "monitors", None) or {}).get(profile.id)
    return monitor, (monitor.workspace if monitor is not None else None)


def pet_owners(prefs: dotprefs.DotPrefs, dots, dot: Dot | None = None) -> dict[str, Dot]:
    """``{pet_id: Dot}`` for the server's Dots (missing picks are assigned and saved)."""
    known = list(dots)
    if dot is not None and all(d.id != dot.id for d in known):
        known.append(dot)
    if not known:
        return {}
    mapping = prefs.pets_for(known)
    owners: dict[str, Dot] = {}
    for item in known:
        pet_id = mapping.get(item.id)
        if pet_id and pet_id not in owners:
            owners[pet_id] = item
    # Past 12 Dots pets repeat; the Dot being edited is always the owner of its own pet.
    if dot is not None and mapping.get(dot.id):
        owners[mapping[dot.id]] = dot
    return owners


def automatic_pet(dot: Dot, owners: dict[str, Dot]) -> str:
    """What automatic assignment would give this Dot now, ignoring its current pick."""
    taken = {pet_id for pet_id, owner in owners.items() if owner.id != dot.id}
    if len(taken) >= len(pets.PETS):
        taken = set()
    return pets.suggest(dot.name, dot.job, taken).id


def tile_label(pet: pets.Pet, owner: Dot | None) -> str:
    """What a screen reader says for a tile: "Sky, pet of Assistente" or "Sky, available"."""
    if owner is not None:
        return _("{pet}, pet of {dot}").format(pet=_(pet.name), dot=owner.name)
    return _("{pet}, available").format(pet=_(pet.name))


# -- widgets -------------------------------------------------------------------------------

class PetTile(Gtk.FlowBoxChild):
    """One pet in the grid: picture, name and who has it."""

    __gtype_name__ = "DotdeckPetTile"

    def __init__(self, pet: pets.Pet):
        super().__init__(focusable=True)
        self.pet = pet
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.avatar = PetAvatar(pet.id, size=TILE_SIZE)
        self.avatar.set_margin_bottom(4)
        self.name_label = Gtk.Label(label=_(pet.name), ellipsize=Pango.EllipsizeMode.END,
                                    max_width_chars=12)
        self.name_label.add_css_class("pet-tile-name")
        self.caption = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=14)
        self.caption.add_css_class("pet-tile-caption")
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")
        box.append(self.avatar)
        box.append(self.name_label)
        box.append(self.caption)
        # The tile speaks for itself ("Sky, pet of Assistente"); its parts would only repeat it.
        box.update_state([Gtk.AccessibleState.HIDDEN], [True])
        self.set_child(box)
        self.set_tooltip_text(_(TAGLINES.get(pet.id, "")) or None)
        self.owner: Dot | None = None

    def update(self, owner: Dot | None, current: bool) -> None:
        self.owner = owner
        if current:
            text = _("Current")
        elif owner is not None:
            text = _("With {dot}").format(dot=owner.name)
        else:
            text = " "  # keeps every tile the same height
        self.caption.set_label(text)
        if current:
            self.caption.add_css_class("current")
            self.caption.remove_css_class("dim-label")
        else:
            self.caption.remove_css_class("current")
            self.caption.add_css_class("dim-label")
        self.update_property([Gtk.AccessibleProperty.LABEL], [tile_label(self.pet, owner)])


class PetGallery(Adw.Dialog):
    """Pick a pet for one Dot. ``done(pet_id)`` after Choose, ``done(None)`` if cancelled."""

    __gtype_name__ = "DotdeckPetGallery"

    def __init__(self, app, profile, dot: Dot,
                 done: Callable[[str | None], None] | None = None):
        super().__init__(title=dot.name, content_width=560, content_height=712,
                         width_request=360, height_request=420)
        ensure_css()
        self._app = app
        self._profile = profile
        self.dot = dot
        self._done = done
        self._finished = False
        self._closed = False
        self.prefs = dotprefs.for_profile(profile.id)
        self._monitor, _workspace = workspace_of(app, profile)
        self.owners: dict[str, Dot] = {}
        self.current: str | None = None
        self.selected: str = pets.DEFAULT.id
        self.suggested: str | None = None
        self.alert: Adw.AlertDialog | None = None
        self._alert_open = False
        self.presented_on: Gtk.Widget | None = None
        self.tiles: dict[str, PetTile] = {}
        self._syncing = False
        self._listed = False
        self._build()
        self._refresh()
        self.select(self.current or pets.DEFAULT.id)
        self._handlers = [(self.prefs, self.prefs.connect("changed", self._on_outside_change))]
        if self._monitor is not None:
            self._handlers.append((self._monitor, self._monitor.connect(
                "workspace-changed", self._on_outside_change)))
        self.connect("closed", self._on_closed)

    # -- building --------------------------------------------------------------------------

    def _build(self) -> None:
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.window_title = Adw.WindowTitle(title=self.dot.name, subtitle=_("Choose a pet"))
        header.set_title_widget(self.window_title)
        cancel = Gtk.Button(label=_("Cancel"), use_underline=True)
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        self.choose_button = Gtk.Button(label=_("Choose"), use_underline=True)
        self.choose_button.add_css_class("suggested-action")
        self.choose_button.connect("clicked", lambda _b: self.choose())
        header.pack_end(self.choose_button)
        self.set_default_widget(self.choose_button)

        # The stage: a big preview of the selected pet, tinted in its colour.
        self.stage = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22,
                             margin_start=16, margin_end=16, margin_top=4, margin_bottom=12)
        self.stage.add_css_class("pet-stage")
        self.preview = PetAvatar(pets.DEFAULT.id, size=PREVIEW_SIZE)
        self.preview.update_state([Gtk.AccessibleState.HIDDEN], [True])
        self.stage.append(self.preview)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True,
                       valign=Gtk.Align.CENTER)
        self.preview_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.preview_name.add_css_class("pet-stage-name")
        self.preview_tagline = Gtk.Label(xalign=0, wrap=True,
                                         natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        self.preview_tagline.add_css_class("dim-label")
        self.preview_note = Gtk.Label(xalign=0, wrap=True)
        self.preview_note.add_css_class("caption")
        self.states = Adw.ToggleGroup(halign=Gtk.Align.START, margin_top=10, can_shrink=True)
        for name, label in STATES:
            self.states.add(Adw.Toggle(name=name, label=_(label)))
        self.states.set_active_name("idle")
        self.states.connect("notify::active-name", self._on_state)
        self.states.update_property([Gtk.AccessibleProperty.LABEL], [_("Preview state")])
        for widget in (self.preview_name, self.preview_tagline, self.preview_note, self.states):
            info.append(widget)
        self.stage.append(info)
        self._info = info

        # The grid of pets.
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_start=22, margin_end=16,
                      margin_bottom=6)
        heading = Gtk.Label(label=_("All Pets"), xalign=0, hexpand=True)
        heading.add_css_class("heading")
        top.append(heading)
        self.automatic_button = Gtk.Button(child=Adw.ButtonContent(
            icon_name="starred-symbolic", label=_("Automatic"), use_underline=True))
        self.automatic_button.add_css_class("flat")
        self.automatic_button.set_tooltip_text(
            _("Pick the free pet that best fits this Dot's name and job"))
        self.automatic_button.connect("clicked", lambda _b: self.automatic())
        top.append(self.automatic_button)

        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
                                min_children_per_line=3, max_children_per_line=4,
                                column_spacing=6, row_spacing=6, valign=Gtk.Align.START,
                                activate_on_single_click=False, margin_start=16,
                                margin_end=16, margin_bottom=18)
        self.grid.add_css_class("pet-grid")
        self.grid.update_property([Gtk.AccessibleProperty.LABEL], [_("Pets")])
        for pet in pets.PETS:
            tile = PetTile(pet)
            self.tiles[pet.id] = tile
            self.grid.append(tile)
        self.grid.connect("selected-children-changed", self._on_selection)
        self.grid.connect("child-activated", lambda *_: self.choose())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(top)
        body.append(self.grid)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                      propagate_natural_height=True)
        scroller.set_child(body)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.stage)
        view.set_content(scroller)
        self.set_child(view)

        # On a phone the stage stacks: picture on top, words centred under it.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 460sp"))
        narrow.add_setter(self.stage, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.stage, "spacing", 10)
        narrow.add_setter(self.preview, "size", 104)
        narrow.add_setter(self._info, "halign", Gtk.Align.CENTER)
        narrow.add_setter(self.states, "halign", Gtk.Align.CENTER)
        for label in (self.preview_name, self.preview_tagline, self.preview_note):
            narrow.add_setter(label, "xalign", 0.5)
            narrow.add_setter(label, "justify", Gtk.Justification.CENTER)
        self.add_breakpoint(narrow)

    # -- state -----------------------------------------------------------------------------

    def _refresh(self) -> None:
        _monitor, workspace = workspace_of(self._app, self._profile)
        dots = workspace.dots if workspace is not None else ()
        fresh = workspace.dot(self.dot.id) if workspace is not None else None
        if fresh is not None:
            self._listed = True
            self.dot = fresh
            self.set_title(fresh.name)
            self.window_title.set_title(fresh.name)
        elif self._listed and workspace is not None:
            # Deleted on the server (in the web page, or another computer) while open here.
            self._vanished()
            return
        self._syncing = True
        try:
            self.owners = pet_owners(self.prefs, dots, self.dot)
        finally:
            self._syncing = False
        self.current = self.prefs.pet(self.dot.id) or next(
            (pet_id for pet_id, owner in self.owners.items() if owner.id == self.dot.id), None)
        for pet_id, tile in self.tiles.items():
            owner = self.owners.get(pet_id)
            tile.update(owner, pet_id == self.current)
        self._update_preview()

    def _on_outside_change(self, *_args) -> None:
        if not self._syncing and not self._finished:
            self._refresh()

    def _vanished(self) -> None:
        """The Dot no longer exists: there is nothing left to choose a pet for."""
        log.info("pet gallery: Dot %s was deleted", self.dot.id)
        self._finish(None)
        if self.alert is not None and self._alert_open:
            self.alert.force_close()
        show_toast(self._app, self._parent_for_toast(),
                   _("{dot} was deleted on the server").format(dot=self.dot.name))
        GLib.idle_add(self._close_later)

    def select(self, pet_id: str, *, focus: bool = False) -> None:
        tile = self.tiles.get(pet_id)
        if tile is None:
            return
        self.grid.select_child(tile)
        if focus:
            tile.grab_focus()
        self.selected = pet_id
        self._update_preview()

    def _on_selection(self, _grid) -> None:
        chosen = self.grid.get_selected_children()
        if not chosen:
            return
        pet_id = chosen[0].pet.id
        if pet_id != self.selected:
            self.suggested = None
        self.selected = pet_id
        self._update_preview()

    def _on_state(self, *_args) -> None:
        self.preview.set_property("state", self.states.get_active_name() or "idle")

    def _update_preview(self) -> None:
        pet = pets.get(self.selected) or pets.DEFAULT
        for other in pets.PETS:
            self.stage.remove_css_class(f"pet-{other.id}")
        self.stage.add_css_class(f"pet-{pet.id}")
        self.preview.set_property("pet", pet.id)
        self.preview_name.set_label(_(pet.name))
        self.preview_tagline.set_label(_(TAGLINES.get(pet.id, "")))
        owner = self.owners.get(pet.id)
        for name in ("pet-swap-note", "pet-current-note", "dim-label"):
            self.preview_note.remove_css_class(name)
        if pet.id == self.current:
            note, style = _("Current pet of {dot}").format(dot=self.dot.name), "pet-current-note"
        elif owner is not None and owner.id != self.dot.id:
            note = _("With {dot} now. Choosing it swaps your pets.").format(dot=owner.name)
            style = "pet-swap-note"
        elif pet.id == self.suggested:
            note, style = _("Best match for {dot}").format(dot=self.dot.name), "pet-current-note"
        else:
            note, style = _("Available"), "dim-label"
        if pet.id == self.suggested and pet.id == self.current:
            note = _("Current pet, and the best match")
        self.preview_note.set_label(note)
        self.preview_note.add_css_class(style)

    # -- actions ---------------------------------------------------------------------------

    def automatic(self) -> str:
        """Select the pet automatic assignment would give this Dot; returns its id."""
        pet_id = automatic_pet(self.dot, self.owners)
        self.select(pet_id, focus=True)
        self.suggested = pet_id
        self._update_preview()
        return pet_id

    def choose(self) -> None:
        if self._finished:
            return
        pet_id = self.selected
        if pet_id == self.current:
            self._finish(pet_id)
            self.close()
            return
        owner = self.owners.get(pet_id)
        if owner is not None and owner.id != self.dot.id:
            self._ask_swap(pet_id, owner)
            return
        self._apply(pet_id, None)

    def _ask_swap(self, pet_id: str, owner: Dot) -> None:
        pet = pets.get(pet_id) or pets.DEFAULT
        mine = pets.get(self.current)
        if mine is not None:
            body = _("{pet} belongs to {other}. {other} will get {mine} in exchange.").format(
                pet=_(pet.name), other=owner.name, mine=_(mine.name))
        else:
            body = _("{pet} belongs to {other}. {other} will get another pet.").format(
                pet=_(pet.name), other=owner.name)
        alert = Adw.AlertDialog(heading=_("Swap Pets with {dot}?").format(dot=owner.name),
                                body=body)
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("swap", _("Swap"))
        alert.set_response_appearance("swap", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("swap")
        alert.set_close_response("cancel")

        def answered(_alert, response):
            if response == "swap" and not self._finished:
                self._apply(pet_id, owner)

        alert.connect("response", answered)
        alert.connect("closed", self._on_alert_closed)
        self.alert = alert
        self._alert_open = True
        alert.present(self)

    def _on_alert_closed(self, _alert) -> None:
        self._alert_open = False

    def _apply(self, pet_id: str, owner: Dot | None) -> None:
        previous = self.current
        prefs, dot = self.prefs, self.dot
        self._syncing = True
        try:
            prefs.set_pet(dot.id, pet_id)
        finally:
            self._syncing = False
        self._finish(pet_id)
        # Let a confirmation dialog finish closing before the gallery goes.
        GLib.idle_add(self._close_later)
        if owner is not None:
            def undo():
                if previous and prefs.pet(dot.id) == pet_id:
                    prefs.set_pet(dot.id, previous)
            show_toast(self._app, self._parent_for_toast(),
                       _("{dot} and {other} swapped pets").format(dot=dot.name,
                                                                  other=owner.name),
                       _("Undo") if previous else None, undo if previous else None)

    def _close_later(self) -> bool:
        if not self._closed:
            self.close()
        return GLib.SOURCE_REMOVE

    def _parent_for_toast(self) -> Gtk.Widget | None:
        return self.presented_on or self.get_parent()

    def _finish(self, value: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._done is not None:
            try:
                self._done(value)
            except Exception:  # a broken callback must not leave the dialog half-closed
                log.exception("pet gallery callback failed")

    def _on_closed(self, _dialog) -> None:
        self._closed = True
        self._finish(None)
        for obj, handler in self._handlers:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        self._handlers = []


def choose_pet(app, profile, dot, parent: Gtk.Widget | None,
               done: Callable[[str | None], None] | None = None) -> PetGallery | None:
    """Show the pet gallery for ``dot`` (a workspace Dot, or its id) on ``parent``.

    ``done(pet_id)`` runs after the owner chooses (also when they keep the same pet) and
    ``done(None)`` when they cancel.
    """
    if isinstance(dot, str):
        _monitor, workspace = workspace_of(app, profile)
        found = workspace.dot(dot) if workspace is not None else None
        if found is None:
            log.warning("choose_pet: unknown Dot %s", dot)
            if done is not None:
                done(None)
            return None
        dot = found
    gallery = PetGallery(app, profile, dot, done)
    gallery.presented_on = parent if parent is not None else getattr(app, "window", None)
    present(gallery, app, parent)
    current = gallery.tiles.get(gallery.selected)
    if current is not None:
        gallery.set_focus(current)
    return gallery
