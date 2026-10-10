"""Compatibility: the preferences now live in the Settings window (src/ui/settings).

``apply_color_scheme`` stays here because main.py imports it from this module.
"""

from __future__ import annotations

from gi.repository import Adw

from .settings import PAGES, get_window, open_settings  # noqa: F401 (re-exported)


def apply_color_scheme(value: str) -> None:
    Adw.StyleManager.get_default().set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(value, Adw.ColorScheme.DEFAULT))
