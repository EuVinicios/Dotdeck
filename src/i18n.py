"""Gettext setup. Source strings are in English; po/ has the translations."""

import gettext
import locale

from .config import GETTEXT_DOMAIN, PKG_DIR

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

_translation = gettext.translation(
    GETTEXT_DOMAIN, localedir=str(PKG_DIR / "locale"), fallback=True
)

_ = _translation.gettext
ngettext = _translation.ngettext


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet (translate with _ later)."""
    return message
