"""Talking to the OpenDots updater that runs next to the server (server/updater/)."""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Callable
from urllib.parse import urlsplit

from gi.repository import Gio, GLib, Soup

from . import server
from .store import Profile

log = logging.getLogger(__name__)

DEFAULT_PORT = 4311
RUNNING = "running"


def updater_url(profile: Profile) -> str:
    """The updater lives on the same machine as OpenDots, on port 4311, unless configured."""
    if profile.updater_url:
        return profile.updater_url.rstrip("/")
    host = urlsplit(profile.url).hostname or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{DEFAULT_PORT}"


def on_this_machine(profile: Profile) -> bool:
    """Whether the server runs here. The updater only answers on its own machine (127.0.0.1)
    unless it was set up to listen on the network (server/README.md)."""
    host = (urlsplit(profile.url).hostname or "").strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def blockers(data: dict) -> dict:
    """What stops an update, from the updater's /status (older updaters report less).

    ``dirty``: uncommitted changes. ``branch``: (current, expected) when the server is on
    another branch ("" = detached). ``untracked``: files outside git that the official
    changes would overwrite. ``conflicts``: files that would conflict with local commits.
    """
    branch, expected = data.get("branch"), data.get("expected_branch")
    found = {"dirty": bool(data.get("dirty")),
             "branch": (branch or "", expected) if isinstance(branch, str) and expected
             and branch != expected else None,
             "untracked": [str(p) for p in data.get("untracked") or [] if p],
             "conflicts": [str(p) for p in data.get("conflicts") or [] if p]}
    found["any"] = bool(found["dirty"] or found["branch"] or found["untracked"]
                        or found["conflicts"])
    return found


class UpdaterClient:
    def __init__(self, profile: Profile, token: str | None):
        self.url = updater_url(profile)
        self.token = token
        self._session = server.shared_session()

    def _call(self, method: str, path: str, callback: Callable[[dict | None, str | None], None],
              cancellable: Gio.Cancellable | None = None) -> None:
        message = Soup.Message.new(method, f"{self.url}{path}")
        if message is None:
            callback(None, "invalid address")
            return
        headers = message.get_request_headers()
        headers.append("Accept", "application/json")
        if self.token:
            headers.append("Authorization", f"Bearer {self.token}")
        if method == "POST":
            message.set_request_body_from_bytes("application/json", GLib.Bytes.new(b"{}"))

        def done(session, result):
            try:
                body = session.send_and_read_finish(result)
            except GLib.Error as error:
                callback(None, error.message)
                return
            try:
                data = json.loads(bytes(body.get_data() or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = {}
            status = message.get_status()
            if status >= 400 or not isinstance(data, dict):
                callback(None, (data.get("error") if isinstance(data, dict) else None)
                         or f"HTTP {status}")
            else:
                callback(data, None)

        self._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, cancellable, done)

    def status(self, callback, cancellable=None) -> None:
        self._call("GET", "/status", callback, cancellable)

    def check(self, callback) -> None:
        self._call("POST", "/check", callback)

    def update(self, callback) -> None:
        self._call("POST", "/update", callback)
