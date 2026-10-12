"""Changing things on an OpenDots server: settings, Dots, conversations, memories, model.

Reads go through the monitor (``/api/state`` and ``/api/workspace``); this module is
for the few writes the native interface needs. Every call is asynchronous and ends
with ``callback(data, error)`` where ``error`` is a message for people or ``None``.

Two rules from the server (src/server/app.ts upstream): every non-GET request needs
``Content-Type: application/json`` (DELETE too), and no ``Origin`` header is sent, so
the server's origin allow-list does not apply to Dotdeck itself.
"""

from __future__ import annotations

import json
import logging
from typing import Callable
from urllib.parse import quote

from gi.repository import Gio, GLib, Soup

from . import server
from ..i18n import _
from .workspace import Dot

log = logging.getLogger(__name__)

Callback = Callable[[object, str | None], None]


class OpenDotsAPI:
    def __init__(self, url: str, token: str | None, session: Soup.Session | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self._session = session or server.shared_session()

    # -- transport -------------------------------------------------------------------------

    def request(self, method: str, path: str, body: dict | None, callback: Callback,
                cancellable: Gio.Cancellable | None = None) -> None:
        message = Soup.Message.new(method, f"{self.url}{path}")
        if message is None:
            callback(None, _("The server address is not valid."))
            return
        headers = message.get_request_headers()
        headers.append("Accept", "application/json")
        if self.token:
            headers.append("Authorization", f"Bearer {self.token}")
        if method not in ("GET", "HEAD"):
            payload = json.dumps(body if body is not None else {}).encode("utf-8")
            message.set_request_body_from_bytes("application/json", GLib.Bytes.new(payload))

        def done(session, result):
            try:
                raw = session.send_and_read_finish(result)
            except GLib.Error as error:
                if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    log.debug("%s %s failed: %s", method, path, error.message)
                callback(None, error.message)
                return
            status = int(message.get_status())
            data = None
            content = bytes(raw.get_data() or b"") if raw is not None else b""
            if content:
                try:
                    data = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    data = None
            if status >= 400:
                text = data.get("error") if isinstance(data, dict) else None
                callback(None, text if isinstance(text, str) and text else f"HTTP {status}")
                return
            callback(data, None)

        self._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, cancellable, done)

    def get(self, path: str, callback: Callback, cancellable=None) -> None:
        self.request("GET", path, None, callback, cancellable)

    # -- workspace settings ----------------------------------------------------------------

    def update_settings(self, callback: Callback, **changes) -> None:
        """``name``, ``paused``, ``researchAllowed``, ``memoryAllowed``.

        Pausing or changing a permission stops the work in progress on the server.
        """
        allowed = {"name", "paused", "researchAllowed", "memoryAllowed"}
        self.request("PATCH", "/api/settings", {k: v for k, v in changes.items()
                                                if k in allowed}, callback)

    # -- Dots and conversations ------------------------------------------------------------

    def update_dot(self, dot: Dot, callback: Callback, **changes) -> None:
        self.request("PUT", f"/api/dots/{quote(dot.id)}", dot.update_body(**changes), callback)

    def create_dot(self, callback: Callback, *, name: str, instructions: str, space_id: str,
                   research_allowed: bool = True, memory_allowed: bool = True) -> None:
        self.request("POST", "/api/dots", {
            "name": name, "instructions": instructions, "spaceId": space_id,
            "researchAllowed": research_allowed, "memoryAllowed": memory_allowed}, callback)

    def create_conversation(self, dot_id: str, callback: Callback,
                            title: str = "A new thought") -> None:
        self.request("POST", "/api/conversations", {"dotId": dot_id, "title": title[:120]},
                     callback)

    def threads(self, dot_id: str, callback: Callback, limit: int = 20) -> None:
        """The chat titles and update times the page shows (CopilotKit Intelligence).

        The answer also carries a ``joinCode``: it is a credential and is dropped here.
        """
        def got(data, error):
            if isinstance(data, dict):
                data = {"threads": data.get("threads") if isinstance(data.get("threads"), list)
                        else []}
            callback(data, error)
        self.get(f"/api/copilotkit/threads?agentId={quote(dot_id)}&limit={int(limit)}", got)

    # -- a Dot's connections (MCP servers) ------------------------------------------------
    #
    # Each change applies on the server at once, like in the page. Servers older than the
    # per-Dot connections (upstream #52) answer 404: error ``"unsupported"``.

    def connections(self, dot_id: str, callback: Callback) -> None:
        def got(data, error):
            if error in ("Not found.", "HTTP 404"):
                callback(None, "unsupported")
            elif error is None and not isinstance(data, list):
                callback(None, _("The server sent an unexpected answer."))
            else:
                callback(data, error)
        self.get(f"/api/dots/{quote(dot_id)}/connections", got)

    def add_connection(self, dot_id: str, callback: Callback, *, name: str, url: str,
                       token: str = "") -> None:
        """Connects to the MCP server and lists its tools; the token stays on the server."""
        body = {"name": name.strip(), "url": url.strip()}
        if token.strip():
            body["token"] = token.strip()
        self.request("POST", f"/api/dots/{quote(dot_id)}/connections", body, callback)

    def refresh_connection(self, connection_id: str, callback: Callback) -> None:
        self.request("POST", f"/api/connections/{quote(connection_id)}/refresh", {}, callback)

    def set_connection_tool(self, connection_id: str, tool: str, callback: Callback,
                            **changes) -> None:
        """``enabled`` and ``requiresApproval`` ("Ask first")."""
        body = {k: bool(v) for k, v in changes.items() if k in ("enabled", "requiresApproval")}
        self.request("PATCH", f"/api/connections/{quote(connection_id)}/tools/"
                     f"{quote(tool, safe='')}", body, callback)

    def remove_connection(self, connection_id: str, callback: Callback) -> None:
        self.request("DELETE", f"/api/connections/{quote(connection_id)}", {}, callback)

    # -- memories --------------------------------------------------------------------------

    def add_memory(self, text: str, callback: Callback) -> None:
        self.request("POST", "/api/memories", {"text": text}, callback)

    def update_memory(self, memory_id: str, text: str, callback: Callback) -> None:
        self.request("PUT", f"/api/memories/{quote(memory_id)}", {"text": text}, callback)

    def delete_memory(self, memory_id: str, callback: Callback) -> None:
        self.request("DELETE", f"/api/memories/{quote(memory_id)}", {}, callback)

    # -- model and voice (only on servers that have /api/model-config) ---------------------

    def model_config(self, callback: Callback) -> None:
        """``None`` data with error ``"unsupported"`` when the server has no such endpoint."""
        def got(data, error):
            if error in ("Not found.", "HTTP 404"):
                callback(None, "unsupported")
            else:
                callback(data, error)
        self.get("/api/model-config", got)

    def set_model_config(self, callback: Callback, **changes) -> None:
        """``provider``, ``model``, ``baseUrl``, ``apiKey``, ``voiceModel``, ``voiceName``,
        ``voiceKey``.

        ``provider`` (``openrouter``, ``anthropic``, ``openai-compatible``) switches the
        provider, and ``apiKey`` goes to the key of that provider (or of the one in use).
        Servers older than Dotdeck 0.3.3 ignore ``provider``: do not send it to them.
        Values with line breaks are refused here: the server writes them into its .env.
        """
        allowed = {"provider", "model", "baseUrl", "apiKey", "voiceModel", "voiceName",
                   "voiceKey"}
        body = {k: v.strip() for k, v in changes.items() if k in allowed and isinstance(v, str)}
        if any(("\n" in v or "\r" in v) for v in body.values()):
            callback(None, _("The value can only be one line."))
            return
        self.request("POST", "/api/model-config", body, callback)

    def try_model(self, callback: Callback, **body) -> None:
        """One tiny message to the chat model (``provider``, ``model``: the ones in use).

        ``callback({"ok", "ms", "error"?}, None)``; error ``"unsupported"`` when the server
        cannot test. A failed test is an answer, not an error: ``{"ok": False, ...}``.
        """
        sent = {k: v for k, v in body.items() if k in ("provider", "model")
                and isinstance(v, str) and v}

        def got(data, error):
            if error in ("Not found.", "HTTP 404"):
                callback(None, "unsupported")
            elif error is not None:
                callback({"ok": False, "error": error}, None)
            elif isinstance(data, dict):
                callback(data, None)
            else:
                callback({"ok": False, "error": _("The server sent an unexpected answer.")},
                         None)
        self.request("POST", "/api/model-config/test", sent, got)


def for_profile(app, profile, callback: Callable[[OpenDotsAPI], None]) -> None:
    """An API client for ``profile`` with its token from the keyring."""
    app.tokens.lookup(profile.id, profile.url,
                      lambda token: callback(OpenDotsAPI(profile.url, token)))
