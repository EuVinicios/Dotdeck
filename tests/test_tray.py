"""The tray icon on a private D-Bus, talking to a stand-in StatusNotifierWatcher."""

from __future__ import annotations

import shutil
import subprocess
import unittest

from tests.helpers import run_until

from gi.repository import Gio, GLib  # noqa: E402

from src.core import tray  # noqa: E402

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>"""


class MenuLayoutTest(unittest.TestCase):
    def test_ids_and_properties(self):
        items = [tray.MenuItem("Show_Dotdeck"), tray.MenuItem(separator=True),
                 tray.MenuItem("Servers", children=[tray.MenuItem("Home", toggle="radio",
                                                                  checked=True)]),
                 tray.MenuItem("Busy", enabled=False)]
        layout, index = tray.menu_layout(items)
        self.assertEqual(layout.get_type_string(), "(ia{sv}av)")
        self.assertEqual(sorted(index), [1, 2, 3, 4, 5])
        self.assertEqual(index[4].label, "Home")
        self.assertEqual(index[1].properties()["label"].unpack(), "Show__Dotdeck")
        self.assertEqual(index[2].properties()["type"].unpack(), "separator")
        self.assertEqual(index[4].properties()["toggle-state"].unpack(), 1)
        self.assertFalse(index[5].properties()["enabled"].unpack())
        self.assertEqual(layout.unpack()[2][2][2][0][0], 4)  # Servers > Home

    def test_pixmap_has_badge(self):
        plain = tray._pixmap(22, None, False)
        badged = tray._pixmap(22, "active", False)
        if plain is None:
            self.skipTest("app icon not found")
        width, height, data = plain
        self.assertEqual(len(data), width * height * 4)
        self.assertNotEqual(plain[2], badged[2])


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is not installed")
class TrayDBusTest(unittest.TestCase):
    def setUp(self):
        # A private bus started as a separate program: Gio.TestDBus forks this process,
        # and the forked copy can hang on exit inside the GL driver.
        daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(daemon.wait, 5)
        self.addCleanup(daemon.terminate)
        address = daemon.stdout.readline().strip()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | \
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.app_conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.host_conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.addCleanup(self.app_conn.close_sync, None)
        self.addCleanup(self.host_conn.close_sync, None)

    def _start_watcher(self):
        registered = []
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def on_call(_c, sender, _p, _i, method, params, invocation):
            registered.append((sender, params.unpack()[0]))
            invocation.return_value(None)

        reg = self.host_conn.register_object(tray.WATCHER_PATH, info, on_call, None, None)
        self.addCleanup(self.host_conn.unregister_object, reg)
        owned = []
        Gio.bus_own_name_on_connection(self.host_conn, tray.WATCHER_NAME,
                                       Gio.BusNameOwnerFlags.NONE,
                                       lambda *a: owned.append(True), None)
        self.assertTrue(run_until(lambda: owned, 5))
        return registered

    def call_item(self, path, iface, method, params, reply_type):
        box = {}

        def done(conn, result):
            box["value"] = conn.call_finish(result)

        self.host_conn.call(self.app_conn.get_unique_name(), path, iface, method, params,
                            GLib.VariantType(reply_type) if reply_type else None,
                            Gio.DBusCallFlags.NONE, 3000, None, done)
        self.assertTrue(run_until(lambda: "value" in box, 5))
        return box["value"]

    def test_registers_serves_menu_and_runs_actions(self):
        registered = self._start_watcher()
        icon = tray.TrayIcon(self.app_conn)
        self.addCleanup(icon.stop)
        clicked = []
        activated = []
        icon.connect("activate", lambda *_: activated.append(True))
        icon.update("2 tasks running", "active", False, [
            tray.MenuItem("Show Dotdeck", action=lambda: clicked.append("show")),
            tray.MenuItem("Quit", action=lambda: clicked.append("quit"))])
        icon.start()
        self.assertTrue(run_until(lambda: registered and icon.visible_in_host, 5))
        self.assertEqual(registered[0], (self.app_conn.get_unique_name(), tray.ITEM_PATH))

        props = self.call_item(tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                               GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)), "(a{sv})")
        values = props.unpack()[0]
        self.assertEqual(values["Id"], "io.github.euvinicios.Dotdeck")
        self.assertEqual(values["ToolTip"][3], "2 tasks running")
        self.assertEqual(values["Menu"], tray.MENU_PATH)

        layout = self.call_item(tray.MENU_PATH, "com.canonical.dbusmenu", "GetLayout",
                                GLib.Variant("(iias)", (0, -1, [])), "(u(ia{sv}av))")
        revision, (root, _props, children) = layout.unpack()
        self.assertEqual([c[1]["label"] for c in children], ["Show Dotdeck", "Quit"])

        self.call_item(tray.MENU_PATH, "com.canonical.dbusmenu", "Event",
                       GLib.Variant("(isvu)", (2, "clicked", GLib.Variant("s", ""), 0)), None)
        self.assertTrue(run_until(lambda: clicked == ["quit"], 5))
        self.call_item(tray.ITEM_PATH, "org.kde.StatusNotifierItem", "Activate",
                       GLib.Variant("(ii)", (0, 0)), None)
        self.assertTrue(run_until(lambda: activated, 5))


if __name__ == "__main__":
    unittest.main()
