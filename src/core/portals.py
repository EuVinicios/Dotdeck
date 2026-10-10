"""XDG desktop portals used by Dotdeck: global shortcuts and running in the background.

Portals work the same inside Flatpak and on a regular install (GNOME 48+, KDE Plasma 6),
and the desktop shows its own dialogs for them, so the user stays in control: GNOME asks
before binding a global shortcut and lists Dotdeck under Background Apps.
"""

from __future__ import annotations

import logging
import secrets as _random
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from .. import config  # noqa: E402

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
BACKGROUND_IFACE = "org.freedesktop.portal.Background"

RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1


def _token() -> str:
    return f"dotdeck{_random.token_hex(6)}"


def window_identifier(window: Gtk.Window | None, callback: Callable[[str], None]) -> None:
    """The ``parent_window`` string portals use to place their dialogs over our window."""
    surface = window.get_surface() if window is not None else None
    if surface is None:
        callback("")
        return
    try:
        gi.require_version("GdkWayland", "4.0")
        from gi.repository import GdkWayland
        if isinstance(surface, GdkWayland.WaylandToplevel):
            if not surface.export_handle(lambda _s, handle, *_: callback(f"wayland:{handle}")):
                callback("")
            return
    except (ImportError, ValueError):
        pass
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
        if isinstance(surface, GdkX11.X11Surface):
            callback(f"x11:{surface.get_xid():x}")
            return
    except (ImportError, ValueError):
        pass
    callback("")


class Portal(GObject.Object):
    """Calls portal methods that answer through a Request object."""

    __gtype_name__ = "DotdeckPortal"

    def __init__(self, connection: Gio.DBusConnection):
        super().__init__()
        self.connection = connection
        self._sender = connection.get_unique_name()[1:].replace(".", "_")

    def version(self, interface: str) -> int:
        try:
            reply = self.connection.call_sync(
                BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (interface, "version")), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, 2000, None)
            return int(reply.unpack()[0])
        except GLib.Error:
            return 0

    def register_host_app(self) -> None:
        """Tell the portal our app ID when not sandboxed (xdg-desktop-portal 1.19+)."""
        if config.IN_FLATPAK:
            return
        self.connection.call(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.host.portal.Registry", "Register",
            GLib.Variant("(sa{sv})", (config.APP_ID, {})), None, Gio.DBusCallFlags.NONE, 2000,
            None, lambda c, r: self._ignore(c, r))

    @staticmethod
    def _ignore(connection, result) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            log.debug("Portal registry not available: %s", error.message)

    def request(self, interface: str, method: str, build_args: Callable[[str], GLib.Variant],
                callback: Callable[[int, dict], None]) -> None:
        """``build_args(handle_token)`` returns the parameters; ``callback(response, results)``."""
        token = _token()
        path = f"{OBJECT_PATH}/request/{self._sender}/{token}"
        state = {"sub": 0, "done": False}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            if state["done"]:
                return
            state["done"] = True
            self.connection.signal_unsubscribe(state["sub"])
            response, results = params.unpack()
            callback(response, results)

        state["sub"] = self.connection.signal_subscribe(
            BUS_NAME, REQUEST_IFACE, "Response", path, None, Gio.DBusSignalFlags.NONE,
            on_response)

        def called(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                if not state["done"]:
                    state["done"] = True
                    connection.signal_unsubscribe(state["sub"])
                    log.info("%s.%s failed: %s", interface, method, error.message)
                    callback(-1, {"error": error.message})

        self.connection.call(BUS_NAME, OBJECT_PATH, interface, method, build_args(token), None,
                             Gio.DBusCallFlags.NONE, -1, None, called)


class Background(GObject.Object):
    """Ask to keep running without windows, optionally starting at login."""

    __gtype_name__ = "DotdeckBackground"

    def __init__(self, portal: Portal):
        super().__init__()
        self._portal = portal
        self.version = portal.version(BACKGROUND_IFACE)

    @property
    def available(self) -> bool:
        return self.version > 0

    def request(self, parent: str, reason: str, autostart: bool,
                callback: Callable[[bool, bool], None] | None = None) -> None:
        """``callback(background_allowed, autostart_enabled)``."""
        if not self.available or config.IN_SNAP:
            ok = _fallback_autostart(autostart)
            if callback:
                callback(True, ok and autostart)
            return

        def build(token):
            return GLib.Variant("(sa{sv})", (parent, {
                "handle_token": GLib.Variant("s", token),
                "reason": GLib.Variant("s", reason),
                "autostart": GLib.Variant("b", autostart),
                "commandline": GLib.Variant("as", ["dotdeck", "--background"]),
                "dbus-activatable": GLib.Variant("b", False),
            }))

        def done(response, results):
            if callback:
                callback(response == RESPONSE_SUCCESS and bool(results.get("background", True)),
                         bool(results.get("autostart", False)))

        self._portal.request(BACKGROUND_IFACE, "RequestBackground", build, done)

    def set_status(self, message: str) -> None:
        """The line GNOME shows under Dotdeck in Background Apps (portal version 2)."""
        if self.version < 2:
            return
        self._portal.connection.call(
            BUS_NAME, OBJECT_PATH, BACKGROUND_IFACE, "SetStatus",
            GLib.Variant("(a{sv})", ({"message": GLib.Variant("s", message[:96])},)), None,
            Gio.DBusCallFlags.NONE, -1, None, lambda c, r: Portal._ignore(c, r))


def _fallback_autostart(enabled: bool) -> bool:
    """Manage the XDG autostart file ourselves: in a snap (snapd starts the app named in
    snapcraft.yaml ``autostart:`` from $SNAP_USER_DATA/.config/autostart) and on desktops
    without the Background portal."""
    if config.IN_FLATPAK:
        return False
    from pathlib import Path
    name = "dotdeck-autostart.desktop" if config.IN_SNAP else f"{config.APP_ID}.desktop"
    path = Path(GLib.get_user_config_dir()) / "autostart" / name
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Desktop Entry]\nType=Application\n"
                            f"Name={config.APP_NAME}\nExec=dotdeck --background\n"
                            f"Icon={config.APP_ID}\nX-GNOME-Autostart-enabled=true\n")
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as error:
        log.warning("Could not update %s: %s", path, error)
        return False


class GlobalShortcuts(GObject.Object):
    """Shortcuts that work while Dotdeck is in the background (GNOME 48+, Plasma 6)."""

    __gtype_name__ = "DotdeckGlobalShortcuts"
    __gsignals__ = {
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "deactivated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, portal: Portal):
        super().__init__()
        self._portal = portal
        self.version = portal.version(SHORTCUTS_IFACE)
        self.session: str | None = None
        self.triggers: dict[str, str] = {}
        self._subs: list[int] = []

    @property
    def available(self) -> bool:
        return self.version > 0

    @property
    def can_configure(self) -> bool:
        return self.version >= 2 and self.session is not None

    def bind(self, shortcuts: list[tuple[str, str, str]], parent: str,
             callback: Callable[[bool], None] | None = None) -> None:
        """Bind ``(id, description, preferred trigger)`` items. GNOME asks the user once."""
        if not self.available:
            if callback:
                callback(False)
            return
        if self.session:
            self._bind(shortcuts, parent, callback)
            return

        def build(token):
            return GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", _token()),
            },))

        def created(response, results):
            if response != RESPONSE_SUCCESS or "session_handle" not in results:
                if callback:
                    callback(False)
                return
            self.session = results["session_handle"]
            self._listen()
            self._bind(shortcuts, parent, callback)

        self._portal.request(SHORTCUTS_IFACE, "CreateSession", build, created)

    def _bind(self, shortcuts, parent, callback) -> None:
        items = [(shortcut_id, {
            "description": GLib.Variant("s", description),
            **({"preferred_trigger": GLib.Variant("s", trigger)} if trigger else {}),
        }) for shortcut_id, description, trigger in shortcuts]

        def build(token):
            return GLib.Variant("(oa(sa{sv})sa{sv})", (
                self.session, items, parent, {"handle_token": GLib.Variant("s", token)}))

        def bound(response, results):
            ok = response == RESPONSE_SUCCESS
            if ok:
                self._update_triggers(results.get("shortcuts", []))
            if callback:
                callback(ok)

        self._portal.request(SHORTCUTS_IFACE, "BindShortcuts", build, bound)

    def configure(self, parent: str) -> None:
        """Open the desktop's own shortcut settings for Dotdeck (portal version 2)."""
        if not self.can_configure:
            return
        self._portal.connection.call(
            BUS_NAME, OBJECT_PATH, SHORTCUTS_IFACE, "ConfigureShortcuts",
            GLib.Variant("(osa{sv})", (self.session, parent, {})), None, Gio.DBusCallFlags.NONE,
            -1, None, lambda c, r: Portal._ignore(c, r))

    def close(self) -> None:
        for sub in self._subs:
            self._portal.connection.signal_unsubscribe(sub)
        self._subs.clear()
        if self.session:
            self._portal.connection.call(
                BUS_NAME, self.session, "org.freedesktop.portal.Session", "Close", None, None,
                Gio.DBusCallFlags.NONE, -1, None, lambda c, r: Portal._ignore(c, r))
            self.session = None

    def _listen(self) -> None:
        connection = self._portal.connection

        def on_signal(_conn, _sender, _path, _iface, signal, params):
            values = params.unpack()
            if values[0] != self.session:
                return
            if signal == "Activated":
                self.emit("activated", values[1])
            elif signal == "Deactivated":
                self.emit("deactivated", values[1])
            elif signal == "ShortcutsChanged":
                self._update_triggers(values[1])

        for name in ("Activated", "Deactivated", "ShortcutsChanged"):
            self._subs.append(connection.signal_subscribe(
                BUS_NAME, SHORTCUTS_IFACE, name, OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
                on_signal))

    def _update_triggers(self, shortcuts) -> None:
        self.triggers = {shortcut_id: str(info.get("trigger_description", ""))
                         for shortcut_id, info in shortcuts}
        self.emit("changed")
