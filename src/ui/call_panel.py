"""The call panel: what the Grok Bot app shows during a voice chat, natively (ADR-029).

The Dot's pet listening, thinking or speaking; two level waves, the Dot's and yours; what
each of you said, as it is said; and round buttons for the voice settings, the microphone
and hanging up. It floats under the header's title without taking the keyboard, and the
page's own call sheet stays as it is.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from ..core import pets  # noqa: E402
from ..i18n import _  # noqa: E402
from .pet import PetAvatar, tile_colour  # noqa: E402

TRANSCRIPT_LINES = 12      # the last things said that the panel keeps
STEP_MS = 100              # how often a wave moves on


def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    colour.red, colour.green, colour.blue, colour.alpha = r, g, b, a
    return colour


class LevelWave(Gtk.Widget):
    """Thin bars, newest on the right: the last moments of one side's loudness."""

    __gtype_name__ = "DotdeckLevelWave"

    def __init__(self, bars: int = 24, height: int = 28, bar: float = 3, gap: float = 4,
                 **kwargs):
        super().__init__(**kwargs)
        self._bars, self._height, self._bar, self._gap = bars, height, bar, gap
        self._history: deque[float] = deque([0.0] * bars, maxlen=bars)
        self._level = 0.0
        self._colour: Gdk.RGBA | None = None     # None: the accent colour
        self._timer = 0
        self.set_valign(Gtk.Align.CENTER)
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())

    def set_colour(self, colour: Gdk.RGBA | None) -> None:
        self._colour = colour
        self.queue_draw()

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level)))

    def reset(self) -> None:
        self._level = 0.0
        self._history.extend([0.0] * self._bars)
        self.queue_draw()

    @property
    def levels(self) -> list[float]:
        return list(self._history)

    def step(self) -> None:
        """The wave moves on by one bar (every ``STEP_MS`` while on screen)."""
        self._history.append(self._level)
        self.queue_draw()

    def _start(self) -> None:
        if not self._timer:
            self._timer = GLib.timeout_add(STEP_MS, lambda: (self.step(), True)[1])

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            width = int(self._bars * self._bar + (self._bars - 1) * self._gap)
            return width, width, -1, -1
        return self._height, self._height, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        colour = self._colour or Adw.StyleManager.get_default().get_accent_color_rgba()
        height = self.get_height()
        for index, level in enumerate(self._history):
            bar_height = max(self._bar, level * height)
            x = index * (self._bar + self._gap)
            y = (height - bar_height) / 2
            rect = Gsk.RoundedRect()
            rect.init_from_rect(Graphene.Rect().init(x, y, self._bar, bar_height), self._bar / 2)
            snapshot.push_rounded_clip(rect)
            alpha = 0.35 + 0.65 * min(1.0, level * 1.6)
            snapshot.append_color(_rgba(colour.red, colour.green, colour.blue,
                                        colour.alpha * alpha), rect.bounds)
            snapshot.pop()


def dot_colour(pet_id: str) -> Gdk.RGBA:
    """The colour of a Dot in the call: its pet's fur, as the Grok Bot tints its bots."""
    r, g, b = tile_colour(pets.get(pet_id) or pets.DEFAULT)
    return _rgba(r, g, b)


class CallPanel(Gtk.Popover):
    __gtype_name__ = "DotdeckCallPanel"

    def __init__(self, on_settings: Callable[[], None]):
        super().__init__(autohide=False, has_arrow=True, position=Gtk.PositionType.BOTTOM)
        self.add_css_class("call-panel")
        self._said: deque[tuple[str, str]] = deque(maxlen=TRANSCRIPT_LINES)
        self._partial: dict[str, str] = {}
        self._dot_name = ""
        self._pulse = 0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=14,
                      margin_bottom=14, margin_start=16, margin_end=16, width_request=300)

        top = Gtk.Box(spacing=12)
        self.avatar = PetAvatar(None, size=56, state="call")
        top.append(self.avatar)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                        hexpand=True)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class("title-4")
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("numeric")
        names.append(self.name)
        names.append(self.status)
        top.append(names)
        hide = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.START,
                          tooltip_text=_("Hide the Call Panel"))
        hide.add_css_class("flat")
        hide.add_css_class("circular")
        hide.connect("clicked", lambda _b: self.popdown())
        top.append(hide)
        box.append(top)

        # Until the Dot can hear: a bar that keeps moving, in the Dot's colour.
        self.progress = Gtk.ProgressBar(visible=False, pulse_step=0.12)
        self.progress.add_css_class("call-progress")
        box.append(self.progress)

        waves = Gtk.Grid(column_spacing=12, row_spacing=6)
        self.dot_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                   max_width_chars=10)
        self.dot_label.add_css_class("caption")
        self.dot_wave = LevelWave()
        you = Gtk.Label(label=_("You"), xalign=0)
        you.add_css_class("caption")
        self.you_wave = LevelWave()
        waves.attach(self.dot_label, 0, 0, 1, 1)
        waves.attach(self.dot_wave, 1, 0, 1, 1)
        waves.attach(you, 0, 1, 1, 1)
        waves.attach(self.you_wave, 1, 1, 1, 1)
        box.append(waves)

        self.transcript = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                    selectable=True, valign=Gtk.Align.END)
        self.transcript.add_css_class("call-transcript")
        self.transcript_scroll = Gtk.ScrolledWindow(
            child=self.transcript, hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True, max_content_height=160)
        box.append(self.transcript_scroll)

        controls = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        settings = Gtk.Button(icon_name="emblem-system-symbolic",
                              tooltip_text=_("Voice Settings"))
        settings.connect("clicked", lambda _b: on_settings())
        self.mute = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic",
                                     action_name="win.toggle-mute",
                                     tooltip_text=_("Mute Microphone (Ctrl+Shift+M)"))
        self.hang_up = Gtk.Button(icon_name="call-stop-symbolic", action_name="win.call",
                                  tooltip_text=_("Hang Up (Ctrl+Shift+L)"))
        self.hang_up.add_css_class("destructive-action")
        for button in (settings, self.mute, self.hang_up):
            button.add_css_class("circular")
            button.add_css_class("call-control")
            controls.append(button)
        box.append(controls)
        self.set_child(box)
        self.connect("closed", lambda *_: self._stop_pulse())
        self.reset()

    # -- state -----------------------------------------------------------------------------

    def update(self, *, dot_name: str, pet_id: str, phase: str, status: str,
               connecting: bool, ending: bool, muted: bool = False) -> None:
        """What the call is doing now (from the window, about once a second)."""
        self.mute.set_icon_name("microphone-disabled-symbolic" if muted
                                else "audio-input-microphone-symbolic")
        self.mute.set_tooltip_text(_("Unmute Microphone (Ctrl+Shift+M)") if muted
                                   else _("Mute Microphone (Ctrl+Shift+M)"))
        self._dot_name = dot_name
        self.name.set_label(dot_name)
        self.dot_label.set_label(dot_name)
        self.avatar.pet = pet_id
        self.avatar.state = phase if phase in ("listening", "thinking", "speaking") else "call"
        self.dot_wave.set_colour(dot_colour(pet_id))
        self.status.set_label(status)
        self.hang_up.set_sensitive(not ending)
        self.progress.set_visible(connecting or ending)
        if connecting or ending:
            self._start_pulse()
        else:
            self._stop_pulse()
        self._show_transcript()

    def set_level(self, role: str, level: float) -> None:
        (self.dot_wave if role == "dot" else self.you_wave).set_level(level)

    def add_caption(self, role: str, text: str, final: bool) -> None:
        """What one side said: ``final`` lines stay, the others grow as they are said."""
        if final:
            self._partial.pop(role, None)
            if text:
                self._said.append((role, text))
        else:
            self._partial[role] = text
        self._show_transcript()

    def reset(self) -> None:
        self._said.clear()
        self._partial.clear()
        self.dot_wave.reset()
        self.you_wave.reset()
        self._show_transcript()

    def _said_now(self) -> list[tuple[str, str]]:
        """``(who, what)`` for each thing said, the ones still being said last."""
        who = {"dot": self._dot_name or _("Dot"), "you": _("You")}
        said = list(self._said) + [(role, text) for role, text in self._partial.items() if text]
        return [(who.get(role, role), text) for role, text in said]

    def lines(self) -> list[str]:
        """The transcript as shown, one line per thing said."""
        return [f"{who}: {text}" for who, text in self._said_now()]

    def _show_transcript(self) -> None:
        said = self._said_now()
        if said:
            # Who said it in bold, then what they said.
            escape = GLib.markup_escape_text
            self.transcript.set_markup("\n".join(f"<b>{escape(who)}:</b> {escape(text)}"
                                                 for who, text in said))
            self.transcript.remove_css_class("dim-label")
        else:
            self.transcript.set_text(_("Nothing said yet"))
            self.transcript.add_css_class("dim-label")
        adjustment = self.transcript_scroll.get_vadjustment()
        GLib.idle_add(lambda: (adjustment.set_value(adjustment.get_upper()), False)[1])

    def _start_pulse(self) -> None:
        if not self._pulse:
            self._pulse = GLib.timeout_add(120, lambda: (self.progress.pulse(), True)[1])

    def _stop_pulse(self) -> None:
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = 0
