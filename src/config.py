"""Application constants and data locations."""

from __future__ import annotations

import os
from pathlib import Path

from gi.repository import GLib

APP_ID = "io.github.euvinicios.Dotdeck"
APP_NAME = "Dotdeck"
VERSION = "0.6.0"
GETTEXT_DOMAIN = "dotdeck"
DEVELOPER = "EuVinicios"
REPOSITORY = "https://github.com/EuVinicios/Dotdeck"
WEBSITE = "https://euvinicios.github.io/Dotdeck/"
ISSUE_URL = f"{REPOSITORY}/issues"
OPENDOTS_URL = "https://github.com/CopilotKit/OpenDots"

# The OpenDots web client keeps the owner token in sessionStorage under this key
# (src/client/api.ts upstream). Dotdeck injects the token from the keyring there.
TOKEN_STORAGE_KEY = "opendots-token"
DEFAULT_PORT = 4310

PKG_DIR = Path(__file__).resolve().parent
IN_FLATPAK = os.path.exists("/.flatpak-info")
IN_SNAP = bool(os.environ.get("SNAP_NAME"))

# DOTDECK_DEVEL=1 enables the web inspector and debug logging.
DEVEL = os.environ.get("DOTDECK_DEVEL") == "1"


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    return _ensure(Path(GLib.get_user_config_dir()) / "dotdeck")


def data_dir() -> Path:
    return _ensure(Path(GLib.get_user_data_dir()) / "dotdeck")


def cache_dir() -> Path:
    return _ensure(Path(GLib.get_user_cache_dir()) / "dotdeck")


def profile_data_dir(profile_id: str) -> Path:
    """Cookies and web storage of one server, kept apart from the others."""
    return _ensure(data_dir() / "profiles" / profile_id)


def profile_cache_dir(profile_id: str) -> Path:
    return _ensure(cache_dir() / "profiles" / profile_id)


def source_data_dir() -> Path | None:
    """The repository's data/ folder when running from a source checkout."""
    candidate = PKG_DIR.parent / "data"
    return candidate if (candidate / "icons").is_dir() else None


def pets_dir() -> Path:
    """The pet pictures (src/pets, rendered by tools/render_pets.py)."""
    return PKG_DIR / "pets"


def icon_file(size: int = 256) -> Path | None:
    """The app icon as a PNG, for places that cannot use the icon theme (tray pixmaps)."""
    relative = Path("icons") / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
    roots = [source_data_dir()] + [Path(p) for p in (GLib.get_user_data_dir(),
                                                      *GLib.get_system_data_dirs())]
    return next((root / relative for root in roots if root and (root / relative).is_file()), None)
