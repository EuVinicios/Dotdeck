"""Talking to an OpenDots server: address parsing, connection tests and JSON requests."""

from __future__ import annotations

import ipaddress
import json
import logging
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import gi

gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup  # noqa: E402

from .. import config  # noqa: E402
from ..i18n import _  # noqa: E402

log = logging.getLogger(__name__)

# Probe outcomes, from best to worst.
OK = "ok"
TOKEN_REQUIRED = "token-required"
TOKEN_INVALID = "token-invalid"
LOCAL_ONLY = "local-only"
NOT_OPENDOTS = "not-opendots"
TLS_ERROR = "tls-error"
UNREACHABLE = "unreachable"

_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".home.arpa")


def _ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _looks_local(host: str) -> bool:
    ip = _ip(host)
    if ip is not None:
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip in _TAILSCALE_V4 \
            or ip in _TAILSCALE_V6
    return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)


def normalize_url(text: str) -> str:
    """Turn what the user typed into a server origin such as ``https://host:4310``.

    Without a scheme, local-looking hosts (localhost, LAN and Tailscale addresses) get
    ``http://`` and everything else ``https://``. Paths are dropped: the OpenDots web
    client calls ``/api`` at the root of its origin.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError(_("Enter the server address."))
    if "://" not in raw:
        host_part = raw.split("/", 1)[0]
        host = urlsplit(f"//{host_part}").hostname or ""
        raw = f"{'http' if _looks_local(host) else 'https'}://{raw}"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ValueError(_("This server address is not valid.")) from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(_("Use an address that starts with http:// or https://."))
    host = parts.hostname
    if not host or parts.username or parts.password:
        raise ValueError(_("This server address is not valid."))
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}{'' if port in (None, default) else f':{port}'}"


def has_explicit_port(text: str) -> bool:
    raw = (text or "").strip()
    if "://" not in raw:
        raw = f"//{raw}"
    try:
        return urlsplit(raw).port is not None
    except ValueError:
        return False


def candidate_urls(text: str) -> list[str]:
    """Addresses worth trying for what the user typed, best guess first.

    When the scheme is missing the other scheme is tried too, and when the port is
    missing the OpenDots default port (4310) is tried after the bare address.
    """
    first = normalize_url(text)
    typed_scheme = "://" in (text or "")
    urls = [first]
    if not typed_scheme:
        other = "http" if first.startswith("https://") else "https"
        urls.append(other + first[first.index("://"):])
    if not has_explicit_port(text):
        urls += [with_port(url, config.DEFAULT_PORT) for url in list(urls)]
    seen: list[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    return seen


def with_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}"


def host_label(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc or url


def is_cleartext_remote(url: str) -> bool:
    """True for http:// to another machine outside a private or Tailscale network."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "http" and not (_looks_local(host) or host.endswith(".ts.net"))


def origin_of(uri: str) -> str | None:
    try:
        parts = urlsplit(uri)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return normalize_url(f"{parts.scheme}://{parts.netloc}")
    except ValueError:
        return None


@dataclass
class ProbeResult:
    kind: str
    message: str = ""
    name: str = ""
    dots: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind == OK


def describe(kind: str, url: str = "") -> str:
    """A sentence for the user explaining a probe outcome."""
    return {
        OK: _("Connected."),
        TOKEN_REQUIRED: _("This server asks for an owner access token."),
        TOKEN_INVALID: _("The server did not accept this access token."),
        LOCAL_ONLY: _("This server only accepts connections from its own computer. "
                      "Set OWNER_TOKEN on the server to reach it over the network."),
        NOT_OPENDOTS: _("This address answered, but it does not look like an OpenDots server."),
        TLS_ERROR: _("The server's security certificate is not trusted."),
        UNREACHABLE: _("Could not reach {host}. Check the address and your network.").format(
            host=host_label(url) if url else _("the server")),
    }.get(kind, _("Something went wrong."))


def _session() -> Soup.Session:
    session = Soup.Session()
    session.set_timeout(12)
    session.set_idle_timeout(30)
    session.set_user_agent(f"{config.APP_NAME}/{config.VERSION} (+{config.REPOSITORY})")
    return session


_SHARED_SESSION: Soup.Session | None = None


def shared_session() -> Soup.Session:
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        _SHARED_SESSION = _session()
    return _SHARED_SESSION


class ServerClient:
    """Read-only JSON calls to one OpenDots server."""

    def __init__(self, url: str, token: str | None = None, session: Soup.Session | None = None):
        self.url = url
        self.token = token
        self._session = session or shared_session()

    def get_json(self, path: str, callback: Callable[[int, object, GLib.Error | None], None],
                 cancellable: Gio.Cancellable | None = None) -> None:
        """GET ``path``; ``callback(status, data, error)`` with status 0 on transport errors."""
        message = Soup.Message.new("GET", f"{self.url}{path}")
        if message is None:
            callback(0, None, GLib.Error(_("This server address is not valid.")))
            return
        headers = message.get_request_headers()
        headers.append("Accept", "application/json")
        if self.token:
            headers.append("Authorization", f"Bearer {self.token}")

        def done(session, result):
            try:
                body = session.send_and_read_finish(result)
            except GLib.Error as error:
                if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    log.debug("GET %s%s failed: %s", self.url, path, error.message)
                callback(0, None, error)
                return
            status = message.get_status()
            data = None
            raw = body.get_data() if body is not None else b""
            if raw:
                try:
                    data = json.loads(bytes(raw).decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    data = None
            callback(int(status), data, None)

        self._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, cancellable, done)

    def probe(self, callback: Callable[[ProbeResult], None],
              cancellable: Gio.Cancellable | None = None) -> None:
        """Check that this is an OpenDots server that accepts our token."""

        def got_state(status, data, error):
            if error is not None:
                tls = error.domain == GLib.quark_to_string(Gio.tls_error_quark())
                kind = TLS_ERROR if tls else UNREACHABLE
                callback(ProbeResult(kind, describe(kind, self.url)))
                return
            result = classify(status, data, bool(self.token))
            if not result.ok:
                result.message = describe(result.kind, self.url)
                callback(result)
                return
            self.get_json("/api/workspace", lambda s, d, e: got_workspace(result, s, d),
                          cancellable)

        def got_workspace(result, status, data):
            if status == 200 and isinstance(data, dict):
                result.dots = [d.get("name", "") for d in data.get("dots", [])
                               if isinstance(d, dict)]
                setup = data.get("setup") if isinstance(data.get("setup"), dict) else {}
                result.missing = [m for m in setup.get("missing", []) if isinstance(m, str)]
            result.message = describe(OK)
            callback(result)

        self.get_json("/api/state", got_state, cancellable)


def classify(status: int, data, sent_token: bool) -> ProbeResult:
    """Interpret the answer to ``GET /api/state`` (pure function, unit tested)."""
    error = data.get("error", "") if isinstance(data, dict) else ""
    if status == 200 and isinstance(data, dict) and isinstance(data.get("settings"), dict):
        name = data["settings"].get("name")
        return ProbeResult(OK, name=name if isinstance(name, str) else "")
    if status == 401 and error:
        return ProbeResult(TOKEN_INVALID if sent_token else TOKEN_REQUIRED)
    if status == 403 and "host" in error.lower():
        return ProbeResult(LOCAL_ONLY)
    return ProbeResult(NOT_OPENDOTS)
