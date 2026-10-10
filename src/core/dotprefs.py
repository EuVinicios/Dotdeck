"""What the owner chose for each Dot on one server: its pet, pinned and hidden Dots.

Kept on this computer, in ``~/.config/dotdeck/dots/<profile id>.json`` (mode 0600),
because OpenDots has no place for it: its Dot schema is strict and the instructions
go into the model's prompt. Pets, once given, never reshuffle: automatic picks are
saved too, and choosing a pet another Dot has swaps the two.
"""

from __future__ import annotations

import time

from gi.repository import GObject

from .. import config
from . import pets
from .store import _read, _write_private

VERSION = 1


class DotPrefs(GObject.Object):
    __gtype_name__ = "DotdeckDotPrefs"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, profile_id: str, path=None):
        super().__init__()
        self.profile_id = profile_id
        self.path = path or (config.config_dir() / "dots" / f"{profile_id}.json")
        data = _read(self.path)
        dots = data.get("dots") if isinstance(data.get("dots"), dict) else {}
        self._dots: dict[str, dict] = {k: dict(v) for k, v in dots.items()
                                       if isinstance(k, str) and isinstance(v, dict)}

    # -- storage ---------------------------------------------------------------------------

    def _entry(self, dot_id: str) -> dict:
        return self._dots.setdefault(dot_id, {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.path, {"version": VERSION, "dots": self._dots})
        self.emit("changed")

    # -- pets ------------------------------------------------------------------------------

    def pets_for(self, dots) -> dict[str, str]:
        """A pet for every Dot (``dots`` are workspace.Dot); new picks are saved."""
        chosen = {dot_id: entry.get("pet") for dot_id, entry in self._dots.items()
                  if pets.get(entry.get("pet"))}
        result = pets.assign([(d.id, d.name, d.job) for d in dots], chosen)
        changed = False
        for dot_id, pet_id in result.items():
            if chosen.get(dot_id) != pet_id:
                self._entry(dot_id)["pet"] = pet_id
                changed = True
        if changed:
            self._save()
        return result

    def pet(self, dot_id: str) -> str | None:
        value = self._dots.get(dot_id, {}).get("pet")
        return value if pets.get(value) else None

    def set_pet(self, dot_id: str, pet_id: str) -> str | None:
        """Give ``pet_id`` to the Dot; returns the Dot it was taken from (they swap)."""
        if not pets.get(pet_id):
            raise ValueError(pet_id)
        previous = self.pet(dot_id)
        other = next((d for d, entry in self._dots.items()
                      if d != dot_id and entry.get("pet") == pet_id), None)
        if other is not None:
            if previous:
                self._dots[other]["pet"] = previous
            else:
                self._dots[other].pop("pet", None)
        self._entry(dot_id)["pet"] = pet_id
        self._save()
        return other

    # -- pinned, hidden, seen --------------------------------------------------------------

    def is_pinned(self, dot_id: str) -> bool:
        return bool(self._dots.get(dot_id, {}).get("pinned"))

    def set_pinned(self, dot_id: str, pinned: bool) -> None:
        self._entry(dot_id)["pinned"] = bool(pinned)
        self._save()

    def is_hidden(self, dot_id: str) -> bool:
        return bool(self._dots.get(dot_id, {}).get("hidden"))

    def set_hidden(self, dot_id: str, hidden: bool) -> None:
        self._entry(dot_id)["hidden"] = bool(hidden)
        self._save()

    def last_seen(self, dot_id: str) -> float:
        value = self._dots.get(dot_id, {}).get("seen")
        return float(value) if isinstance(value, (int, float)) else 0.0

    def mark_seen(self, dot_id: str, when: float | None = None) -> None:
        self._entry(dot_id)["seen"] = when if when is not None else time.time()
        self._save()

    def ordered(self, dots) -> list:
        """Pinned Dots first, then the server's order; hidden ones last."""
        visible = [d for d in dots if not self.is_hidden(d.id)]
        return sorted(visible, key=lambda d: not self.is_pinned(d.id))

    def hidden(self, dots) -> list:
        return [d for d in dots if self.is_hidden(d.id)]


_cache: dict[str, DotPrefs] = {}


def for_profile(profile_id: str) -> DotPrefs:
    """One shared instance per server, so every view sees the same choices."""
    prefs = _cache.get(profile_id)
    if prefs is None:
        prefs = _cache[profile_id] = DotPrefs(profile_id)
    return prefs


def forget(profile_id: str) -> None:
    prefs = _cache.pop(profile_id, None)
    path = prefs.path if prefs else config.config_dir() / "dots" / f"{profile_id}.json"
    try:
        path.unlink()
    except OSError:
        pass
