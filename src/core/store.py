"""Server profiles and app preferences, saved as JSON in the config folder.

Nothing secret is written here: owner tokens live in the keyring (see secrets.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from gi.repository import GObject

from .. import config

log = logging.getLogger(__name__)

# Microphone permission per server, like a browser's site permission.
MIC_ASK = "ask"
MIC_ALLOW = "allow"
MIC_DENY = "deny"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        log.warning("Ignoring unreadable %s: %s", path, error)
        return {}


@dataclass
class Profile:
    id: str
    name: str
    url: str
    chief: str = ""
    microphone: str = MIC_ASK
    updater_url: str = ""          # "" = the server's host on port 4311
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> Profile | None:
        known = {f.name for f in fields(cls)}
        try:
            profile = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        if not profile.id or not profile.url:
            return None
        if profile.microphone not in (MIC_ASK, MIC_ALLOW, MIC_DENY):
            profile.microphone = MIC_ASK
        return profile


class ProfileStore(GObject.Object):
    """The servers the user connected to, and which one is open."""

    __gtype_name__ = "DotdeckProfileStore"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "active-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, path: Path | None = None):
        super().__init__()
        self._path = path or (config.config_dir() / "profiles.json")
        data = _read(self._path)
        self._profiles = [p for p in (Profile.from_dict(item) for item in data.get("profiles", [])
                                      if isinstance(item, dict)) if p]
        self._active_id = data.get("active") if isinstance(data.get("active"), str) else None
        if self._active_id and not self.get(self._active_id):
            self._active_id = None
        if not self._active_id and self._profiles:
            self._active_id = self._profiles[0].id

    # -- queries ---------------------------------------------------------------------------

    def list(self) -> list[Profile]:
        return list(self._profiles)

    def get(self, profile_id: str | None) -> Profile | None:
        return next((p for p in self._profiles if p.id == profile_id), None)

    def find_by_url(self, url: str) -> Profile | None:
        return next((p for p in self._profiles if p.url == url), None)

    @property
    def active(self) -> Profile | None:
        return self.get(self._active_id)

    # -- changes ---------------------------------------------------------------------------

    def add(self, name: str, url: str) -> Profile:
        profile = Profile(id=uuid.uuid4().hex[:12], name=name.strip() or url, url=url)
        self._profiles.append(profile)
        self._save()
        self.emit("changed")
        return profile

    def update(self, profile: Profile) -> None:
        for index, existing in enumerate(self._profiles):
            if existing.id == profile.id:
                self._profiles[index] = profile
                break
        else:
            raise KeyError(profile.id)
        self._save()
        self.emit("changed")

    def remove(self, profile_id: str) -> None:
        self._profiles = [p for p in self._profiles if p.id != profile_id]
        was_active = profile_id == self._active_id
        if was_active:
            self._active_id = self._profiles[0].id if self._profiles else None
        self._save()
        self.emit("changed")
        if was_active:
            self.emit("active-changed")

    def set_active(self, profile_id: str) -> None:
        if profile_id == self._active_id or not self.get(profile_id):
            return
        self._active_id = profile_id
        self._save()
        self.emit("active-changed")

    def _save(self) -> None:
        _write_private(self._path, {
            "version": 1,
            "active": self._active_id,
            "profiles": [asdict(p) for p in self._profiles],
        })


class Settings(GObject.Object):
    """App preferences with defaults. Values are plain JSON types."""

    __gtype_name__ = "DotdeckSettings"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    DEFAULTS: dict = {
        "color-scheme": "system",        # system | light | dark
        "run-in-background": True,
        "autostart": False,
        "notifications": True,
        "global-shortcut": True,
        "echo-cancellation": True,
        "tray-icon": True,
        "window-width": 1280,
        "window-height": 820,
        "window-maximized": False,
        "zoom": 1.0,
    }

    def __init__(self, path: Path | None = None):
        super().__init__()
        self._path = path or (config.config_dir() / "settings.json")
        stored = _read(self._path)
        self._values = {key: stored.get(key, default) for key, default in self.DEFAULTS.items()
                        if isinstance(stored.get(key, default), type(default))
                        or (isinstance(default, float) and isinstance(stored.get(key), int))}
        for key, default in self.DEFAULTS.items():
            self._values.setdefault(key, default)

    def get(self, key: str):
        return self._values[key]

    def set(self, key: str, value) -> None:
        if key not in self.DEFAULTS:
            raise KeyError(key)
        if self._values.get(key) == value:
            return
        self._values[key] = value
        _write_private(self._path, self._values)
        self.emit("changed", key)
