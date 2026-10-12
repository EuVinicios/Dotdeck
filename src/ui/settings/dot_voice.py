"""The call voice of one Dot (servers set up with the Dotdeck guide).

Saved on the server at once, like the Connections: it lives in a table of the OpenDots
fork (``PUT /api/dots/:id/voice``), not in the Dot's own form. A Dot without one speaks
with the server's voice (Voice Calls › Voice).
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ...i18n import N_, _
from .common import escape, string_model

# How each voice sounds, to choose by ear without trying them all.
TIMBRES = {
    "alloy": N_("neutral and balanced"), "ash": N_("clear and confident"),
    "ballad": N_("soft and melodic"), "cedar": N_("natural and steady"),
    "coral": N_("warm and friendly"), "echo": N_("calm and resonant"),
    "fable": N_("expressive, a storyteller"), "marin": N_("natural and clear"),
    "nova": N_("bright and energetic"), "onyx": N_("deep and authoritative"),
    "sage": N_("calm and thoughtful"), "shimmer": N_("soft and gentle"),
    "verse": N_("expressive and versatile"),
}


def voice_label(name: str) -> str:
    """"Onyx — deep and authoritative"."""
    timbre = TIMBRES.get(name)
    return f"{name.capitalize()} — {_(timbre)}" if timbre else name.capitalize()


def choices(data: dict) -> list[str | None]:
    """``None`` (the server's voice) and then every voice the server offers."""
    available = [v for v in data.get("available") or [] if isinstance(v, str) and v]
    return [None, *available]


class DotVoiceGroup(Adw.PreferencesGroup):
    """One row: the voice this Dot speaks with in calls."""

    def __init__(self, win, profile, dot_id: str):
        super().__init__(title=_("Call Voice"),
                         description=_("How this Dot sounds in voice calls. Changes apply "
                                       "from the next call."))
        self.win = win
        self.profile_id = profile.id
        self.dot_id = dot_id
        self._choices: list[str | None] = []
        self._current: str | None = None
        self._setting = False
        self._alive = True
        self.row = Adw.ComboRow(title=_("Voice"))
        self.row.connect("notify::selected", lambda *_: self._on_selected())
        self.add(self.row)
        self.set_visible(False)            # until the server says it has the route
        self.load()

    @property
    def profile(self):
        return self.win.app.profiles.get(self.profile_id)

    def close(self) -> None:
        self._alive = False

    def load(self) -> None:
        if self.profile is not None:
            self.win.with_api(lambda client: client.dot_voices(self._loaded), self.profile)

    def _loaded(self, data, error) -> None:
        if not self._alive:
            return
        if error is not None or not isinstance(data, dict):
            # An older server (or a passing error): the row stays out of the way.
            self.set_visible(False)
            return
        voices = data.get("voices") if isinstance(data.get("voices"), dict) else {}
        default = str(data.get("default") or "marin")
        self._choices = choices(data)
        self._current = voices.get(self.dot_id) if voices.get(self.dot_id) in self._choices \
            else None
        labels = [_("Server voice ({name})").format(name=default.capitalize())]
        labels += [voice_label(v) for v in self._choices[1:]]
        self._setting = True
        self.row.set_model(string_model(labels))
        self.row.set_selected(self._choices.index(self._current))
        self._setting = False
        self.row.set_subtitle(escape(_("Dots without their own voice use {name}").format(
            name=default.capitalize())) if self._current is None else "")
        self.set_visible(True)

    def _on_selected(self) -> None:
        index = self.row.get_selected()
        if self._setting or not 0 <= index < len(self._choices):
            return
        voice = self._choices[index]
        if voice == self._current:
            return
        self.row.set_sensitive(False)

        def done(data, error) -> None:
            if not self._alive:
                return
            self.row.set_sensitive(True)
            if error is not None:
                self.win.toast(_("Could not change the voice: {error}").format(error=error))
            else:
                self.win.toast(_("Voice saved. It applies from the next call."))
            self._loaded(data, None) if error is None else self.load()

        self.win.with_api(lambda client: client.set_dot_voice(self.dot_id, voice, done),
                          self.profile)

