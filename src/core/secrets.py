"""Owner access tokens, kept in the desktop keyring and never on disk in plain text.

libsecret talks to the Secret Service (GNOME Keyring, KWallet) on a regular install and
to the Secret portal inside Flatpak, which keeps an encrypted per-app store unlocked by
the user's keyring. When neither is reachable the token only lives for this session, and
the caller is told so it can warn the user.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from .. import config

log = logging.getLogger(__name__)

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ImportError, ValueError):
    Secret = None

# DOTDECK_SECRETS=memory keeps tokens in memory only (tests and CI have no keyring).
_MEMORY_ONLY = os.environ.get("DOTDECK_SECRETS") == "memory"

_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = Secret.Schema.new(
            f"{config.APP_ID}.Token", Secret.SchemaFlags.NONE,
            {"profile": Secret.SchemaAttributeType.STRING,
             "server": Secret.SchemaAttributeType.STRING})
    return _SCHEMA


def keyring_available() -> bool:
    return Secret is not None and not _MEMORY_ONLY


class TokenStore:
    """Asynchronous token storage. Callbacks run on the main loop."""

    def __init__(self):
        self._cache: dict[str, str] = {}

    def cached(self, profile_id: str) -> str | None:
        return self._cache.get(profile_id)

    def lookup(self, profile_id: str, server: str, callback: Callable[[str | None], None]) -> None:
        if profile_id in self._cache or not keyring_available():
            callback(self._cache.get(profile_id))
            return

        def done(_source, result):
            token = None
            try:
                token = Secret.password_lookup_finish(result)
            except Exception as error:  # locked keyring, no Secret Service, portal denial…
                log.warning("Could not read the token from the keyring: %s", error)
            if token:
                self._cache[profile_id] = token
            callback(token or None)

        Secret.password_lookup(_schema(), {"profile": profile_id, "server": server}, None, done)

    def store(self, profile_id: str, server: str, label: str, token: str,
              callback: Callable[[bool], None] | None = None) -> None:
        """Save the token. The callback gets False when it could not reach the keyring."""
        self._cache[profile_id] = token
        if not keyring_available():
            if callback:
                callback(_MEMORY_ONLY)
            return

        def done(_source, result):
            ok = False
            try:
                ok = bool(Secret.password_store_finish(result))
            except Exception as error:
                log.warning("Could not save the token in the keyring: %s", error)
            if callback:
                callback(ok)

        Secret.password_store(_schema(), {"profile": profile_id, "server": server},
                              Secret.COLLECTION_DEFAULT, label, token, None, done)

    def clear(self, profile_id: str, server: str,
              callback: Callable[[], None] | None = None) -> None:
        self._cache.pop(profile_id, None)
        if not keyring_available():
            if callback:
                callback()
            return

        def done(_source, result):
            try:
                Secret.password_clear_finish(result)
            except Exception as error:
                log.info("Could not remove the token from the keyring: %s", error)
            if callback:
                callback()

        Secret.password_clear(_schema(), {"profile": profile_id, "server": server}, None, done)
