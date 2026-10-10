"""Tray icon through the StatusNotifierItem D-Bus protocol, with its menu (dbusmenu).

GTK 4 has no tray API; StatusNotifierItem is what KDE Plasma, Ubuntu's AppIndicator
extension, Cinnamon, XFCE and others display. It is plain D-Bus, so it works inside
Flatpak with ``--talk-name=org.kde.StatusNotifierWatcher`` and needs no extra library.
When no tray host is running (stock GNOME), nothing is shown and Dotdeck relies on the
Background Apps list instead.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, field
from typing import Callable

import cairo
from gi.repository import Gio, GLib, GObject

from .. import config

log = logging.getLogger(__name__)

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
# /MenuBar is the dbusmenu path that snap confinement (unity7 interface) allows.
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    label: str = ""
    action: Callable[[], None] | None = None
    enabled: bool = True
    visible: bool = True
    separator: bool = False
    toggle: str = ""            # "", "checkmark" or "radio"
    checked: bool = False
    children: list[MenuItem] = field(default_factory=list)

    def properties(self) -> dict[str, GLib.Variant]:
        props: dict[str, GLib.Variant] = {}
        if self.separator:
            props["type"] = GLib.Variant("s", "separator")
        else:
            props["label"] = GLib.Variant("s", self.label.replace("_", "__"))
        if not self.enabled:
            props["enabled"] = GLib.Variant("b", False)
        if not self.visible:
            props["visible"] = GLib.Variant("b", False)
        if self.toggle:
            props["toggle-type"] = GLib.Variant("s", self.toggle)
            props["toggle-state"] = GLib.Variant("i", 1 if self.checked else 0)
        if self.children:
            props["children-display"] = GLib.Variant("s", "submenu")
        return props


def menu_layout(items: list[MenuItem]) -> tuple[GLib.Variant, dict[int, MenuItem]]:
    """Number the items depth-first (root is 0) and build the dbusmenu layout variant."""
    index: dict[int, MenuItem] = {}
    counter = [0]

    def build(item: MenuItem, item_id: int, props: dict[str, GLib.Variant]) -> GLib.Variant:
        children = []
        for child in item.children:
            counter[0] += 1
            child_id = counter[0]
            index[child_id] = child
            children.append(build(child, child_id, child.properties()))
        return GLib.Variant("(ia{sv}av)", (item_id, props, children))

    root = MenuItem(children=items)
    layout = build(root, 0, {"children-display": GLib.Variant("s", "submenu")})
    return layout, index


def _pixmap(size: int, badge: str | None, dim: bool) -> tuple[int, int, bytes] | None:
    """The app icon as ARGB32 in network byte order, with an optional status dot."""
    path = config.icon_file()
    if path is None:
        return None
    try:
        icon = cairo.ImageSurface.create_from_png(str(path))
    except (cairo.Error, OSError) as error:
        log.debug("Could not load the tray icon: %s", error)
        return None
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.scale(size / icon.get_width(), size / icon.get_height())
    ctx.set_source_surface(icon)
    ctx.get_source().set_filter(cairo.FILTER_GOOD)
    ctx.paint()
    ctx.identity_matrix()
    colour = {"active": (0.23, 0.76, 0.42), "offline": (0.6, 0.6, 0.59),
              "alert": (0.88, 0.11, 0.14)}.get(badge or "")
    if colour is not None:
        radius = size * 0.2
        ctx.arc(size - radius - 0.5, size - radius - 0.5, radius, 0, 2 * math.pi)
        ctx.set_source_rgb(1, 1, 1)
        ctx.fill()
        ctx.arc(size - radius - 0.5, size - radius - 0.5, radius - max(1.0, size / 22), 0,
                2 * math.pi)
        ctx.set_source_rgb(*colour)
        ctx.fill()
    surface.flush()
    stride, raw = surface.get_stride(), bytes(surface.get_data())
    little = sys.byteorder == "little"
    out = bytearray()
    for y in range(size):
        for x in range(size):
            o = y * stride + x * 4
            if little:
                b, g, r, a = raw[o:o + 4]
            else:
                a, r, g, b = raw[o:o + 4]
            if a:  # cairo stores premultiplied colour
                r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
            if dim:
                r = g = b = (r * 30 + g * 59 + b * 11) // 100
                a = a * 2 // 3
            out += bytes((a, r, g, b))
    return size, size, bytes(out)


class TrayIcon(GObject.Object):
    """Publishes the tray icon while a StatusNotifierWatcher is present."""

    __gtype_name__ = "DotdeckTrayIcon"
    __gsignals__ = {
        "activate": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, connection: Gio.DBusConnection):
        super().__init__()
        self._connection = connection
        self._registrations: list[int] = []
        self._watch = 0
        self.visible_in_host = False
        self.title = config.APP_NAME
        self.tooltip = ""
        self.status = "Active"
        self._badge: str | None = None
        self._dim = False
        self._pixmaps: list[tuple[int, int, bytes]] = []
        self._items: list[MenuItem] = []
        self._index: dict[int, MenuItem] = {}
        self._layout: GLib.Variant | None = None
        self._revision = 1

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        if self._registrations:
            return
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._registrations = [
            self._connection.register_object(ITEM_PATH, item_info, self._item_call,
                                             self._item_property, None),
            self._connection.register_object(MENU_PATH, menu_info, self._menu_call,
                                             self._menu_property, None),
        ]
        self._render_icon()
        self._watch = Gio.bus_watch_name_on_connection(
            self._connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared, self._watcher_vanished)

    def stop(self) -> None:
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        for registration in self._registrations:
            self._connection.unregister_object(registration)
        self._registrations = []
        self.visible_in_host = False

    def _watcher_appeared(self, connection, _name, _owner) -> None:
        def registered(conn, result):
            try:
                conn.call_finish(result)
                self.visible_in_host = True
            except GLib.Error as error:
                log.info("The tray did not accept the icon: %s", error.message)

        # Registering by object path: the watcher pairs it with our unique bus name.
        connection.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE, -1, None,
                        registered)

    def _watcher_vanished(self, _connection, _name) -> None:
        self.visible_in_host = False

    # -- state -----------------------------------------------------------------------------

    def update(self, tooltip: str, badge: str | None, dim: bool, items: list[MenuItem]) -> None:
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        if (badge, dim) != (self._badge, self._dim):
            self._badge, self._dim = badge, dim
            self._render_icon()
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        self._items = items
        self._layout, self._index = menu_layout(items)
        self._revision += 1
        self._signal(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                     GLib.Variant("(ui)", (self._revision, 0)))

    def _render_icon(self) -> None:
        self._pixmaps = [p for p in (_pixmap(size, self._badge, self._dim) for size in (22, 32, 48))
                         if p]

    def _signal(self, path: str, interface: str, name: str, params: GLib.Variant | None = None):
        if not self._registrations:
            return
        try:
            self._connection.emit_signal(None, path, interface, name, params)
        except GLib.Error as error:
            log.debug("Could not emit %s: %s", name, error.message)

    # -- org.kde.StatusNotifierItem -------------------------------------------------------

    def _item_property(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "Communications"),
            "Id": GLib.Variant("s", config.APP_ID),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", self.status),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "" if self._pixmaps else config.APP_ID),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.title, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.emit("activate")
        invocation.return_value(None)

    # -- com.canonical.dbusmenu ------------------------------------------------------------

    def _menu_property(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _find_layout(self, layout: GLib.Variant, item_id: int) -> GLib.Variant | None:
        if layout.get_child_value(0).get_int32() == item_id:
            return layout
        children = layout.get_child_value(2)
        for i in range(children.n_children()):
            found = self._find_layout(children.get_child_value(i).get_variant(), item_id)
            if found is not None:
                return found
        return None

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if self._layout is None:
            self._layout, self._index = menu_layout(self._items)
        if method == "GetLayout":
            parent = params.get_child_value(0).get_int32()
            node = self._find_layout(self._layout, parent) or self._layout
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self._revision), node))
        elif method == "GetGroupProperties":
            ids = params.get_child_value(0).unpack()
            result = [(i, self._index[i].properties()) for i in ids if i in self._index]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            item = self._index.get(item_id)
            value = item.properties().get(name) if item else None
            invocation.return_value(GLib.Variant("(v)", (value or GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event_id = params.get_child_value(0).get_int32(), \
                params.get_child_value(1).get_string()
            self._activate(item_id, event_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            errors = []
            for item_id, event_id, _data, _time in params.unpack()[0]:
                if item_id in self._index or item_id == 0:
                    self._activate(item_id, event_id)
                else:
                    errors.append(item_id)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _activate(self, item_id: int, event_id: str) -> None:
        item = self._index.get(item_id)
        if event_id == "clicked" and item is not None and item.enabled and item.action:
            GLib.idle_add(lambda: (item.action(), GLib.SOURCE_REMOVE)[1])
