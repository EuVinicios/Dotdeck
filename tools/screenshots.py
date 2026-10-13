#!/usr/bin/env python3
"""Drive Dotdeck through its main screens and save screenshots (also a UI smoke test).

    python3 tools/screenshots.py [--smoke] [--dark] [--demo CLIENT | --url URL --token TOKEN]
                                 [--out DIR]

Without ``--url`` or ``--demo`` it serves a stand-in OpenDots page (tests/fake_server.py,
which mimics the real web client's layout). For the store, the site and AppStream, use
``--demo <OpenDots>/dist/client``: tools/demo_server.py then serves fictional Dots on the
real OpenDots client, in Portuguese when ``LANGUAGE``/``LANG`` say pt (``make
screenshots`` does both languages, light and dark). It never touches your own config or
keyring: everything lives in a temporary folder and tokens stay in memory. The pictures
use the GNOME interface font (Adwaita Sans 11) and made-up microphones and speakers, so
they look like a stock GNOME desktop and show nothing of this computer.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="dotdeck-shots-")
for _name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = os.path.join(_TMP, _name.lower())
os.environ["DOTDECK_SECRETS"] = "memory"
os.environ.setdefault("DOTDECK_TEST_AUDIO", "1")

# GNOME's interface font for the app and the page (a bare X server falls back to DejaVu
# Sans). The rule comes before the system configuration so it wins over its defaults.
FONT = "Adwaita Sans"
_FONTS_CONF = os.path.join(_TMP, "fonts.conf")
with open(_FONTS_CONF, "w", encoding="utf-8") as _conf:
    _conf.write(f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias binding="strong"><family>sans-serif</family><prefer><family>{FONT}</family></prefer></alias>
  <alias binding="strong"><family>system-ui</family><prefer><family>{FONT}</family></prefer></alias>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
</fontconfig>
""")
os.environ.setdefault("FONTCONFIG_FILE", _FONTS_CONF)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

from src import main as dotdeck  # noqa: E402
from src.core import voice  # noqa: E402
from tests.fake_server import MODEL_CONFIG, FakeOpenDots  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import demo_server  # noqa: E402

# The stand-in page (no --url, no --demo) gets the first six demo Dots.
DEMO_JOBS = {name: job for name, job, _text in demo_server.DOTS["en"][:6]}


def language() -> str:
    """"pt" when the app will speak Portuguese (gettext reads LANGUAGE, then LANG)."""
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "pt" if value.lower().startswith("pt") else "en"
    return "en"


def fictional_audio() -> None:
    """Made-up microphones and speakers: the pictures never name this computer's."""
    builtin = "Áudio interno Estéreo analógico" if language() == "pt" \
        else "Built-in Audio Analog Stereo"
    devices = {"source": [voice.AudioDevice("demo-builtin-mic", builtin)],
               "sink": [voice.AudioDevice("demo-builtin-speaker", builtin)]}
    voice.list_devices = lambda kind: list(devices[kind])
    voice.default_device_name = lambda kind: builtin


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


def wait(predicate, timeout: float = 20.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


FRAME = 4  # px around the window drawn by GTK when there is no compositor


def capture(window: Gtk.Window, path: Path) -> None:
    node = None
    for _attempt in range(10):  # a page switch can leave one frame empty
        pump(0.4)
        width, height = window.get_width(), window.get_height()
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            break
    if node is None:
        raise RuntimeError("nothing was drawn")
    # Without a compositor (Xvfb) GTK frames the window with a 1 px line and 3 px of
    # padding outside its own border; drop them so the picture ends at the window edge.
    rect = Graphene.Rect().init(FRAME, FRAME, width - 2 * FRAME, height - 2 * FRAME)
    texture = window.get_renderer().render_texture(node, rect)
    texture.save_to_png(str(path))
    print(path)


def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fake = demo = None
    url, token = args.url, args.token or ""
    if args.demo:
        demo = demo_server.serve(Path(args.demo), 0, language())
        url, token = f"http://127.0.0.1:{demo.server_port}", demo_server.TOKEN
    if not args.smoke:
        fictional_audio()
    if not url:
        fake = FakeOpenDots(name="OpenDots Demo", dots=list(DEMO_JOBS), opendots=True,
                            instructions={n: f"# {n} — {j}\n\nHelps the owner."
                                          for n, j in DEMO_JOBS.items()},
                            model_config=MODEL_CONFIG).start()
        fake.tasks = [{"id": "t1", "prompt": "Weekly market summary", "status": "running"}]
        url = fake.url

    app = dotdeck.DotdeckApplication()
    suffix = "-dark" if args.dark else ""
    failures = []

    def scenario():
        try:
            app.settings.set("color-scheme", "dark" if args.dark else "light")
            Gtk.Settings.get_default().set_property("gtk-font-name", f"{FONT} 11")
            app.settings.set("global-shortcut", False)  # no portal dialogs in CI
            window = app.window
            window.set_default_size(args.width, args.height)
            pump(0.8)
            capture(window, out / f"welcome{suffix}.png")

            window.onboarding._open_server("")
            pump(0.5)
            form = window.onboarding.nav.get_visible_page().get_child().get_content() \
                .get_child().get_child().get_child()
            form.address.set_text(url.replace("http://", ""))
            form.name.set_text(args.name)
            if token:
                form.token.set_text(token)
            form.submit()
            if not wait(lambda: window.onboarding.nav.get_visible_page().get_tag() == "ready", 20):
                failures.append("onboarding did not reach the ready page")
            capture(window, out / f"connected{suffix}.png")

            window.onboarding.emit("finished", window.onboarding._profile_id)
            if not wait(lambda: window.view is not None and window.view.loaded_once, 30):
                failures.append("the server page did not load")
            profile = app.profiles.active
            wait(lambda: app.monitors[profile.id].status == "online", 15)
            wait(lambda: len(app.monitors[profile.id].workspace.dots) > 0, 15)
            if not wait(lambda: bool(getattr(window.view, "focus_active", False)), 20):
                failures.append("focus mode did not start on the page")
            pump(1.0)
            dots = app.monitors[profile.id].workspace.dots
            if args.smoke and dots:
                # The page commands of the native sidebar, each answering whether the page
                # showed the result: this is what breaks when the OpenDots client changes.
                index = window.workspace.dot_index
                for what, command in (
                        (f"open {dots[-1].name}",
                         lambda done: window.view.open_dot(index(dots[-1].id), dots[-1].name, done)),
                        ("open Activity", lambda done: window.view.open_activity(done)),
                        ("open Memories", lambda done: window.view.open_memories(done))):
                    answer: list[bool] = []
                    command(answer.append)
                    if not wait(lambda: bool(answer), 20) or not answer[0]:
                        failures.append(f"the page could not {what}")
            if dots:
                window.open_dot(dots[0].id)
            pump(2.5)
            if args.smoke and dots and not wait(
                    lambda: window.view.page_selection == (dots[0].name, "home")
                    or window.view.page_selection == (dots[0].name, "chat"), 15):
                failures.append(f"the page did not show {dots[0].name} "
                                f"(it shows {window.view.page_selection})")
            capture(window, out / f"workspace{suffix}.png")
            if args.smoke:
                # Ctrl+B: the sidebar becomes a rail of pets, then comes back.
                window.activate_action("win.toggle-rail", None)
                pump(0.5)
                if not (window.sidebar.rail and window.split.get_max_sidebar_width() == 88):
                    failures.append("Ctrl+B did not turn the sidebar into a rail")
                window.activate_action("win.toggle-rail", None)
                pump(0.5)
                if window.sidebar.rail:
                    failures.append("Ctrl+B did not bring the sidebar back")

            if dots and not args.smoke:
                window.change_pet(dots[0].id)
                pump(1.2)
                capture(window, out / f"pets{suffix}.png")
                dialog = window.get_visible_dialog()
                if dialog is not None:
                    dialog.force_close()
                pump(0.4)

            app.show_preferences("appearance")
            pump(1.0)
            from src.ui.settings import window as settings_window
            settings = settings_window.current()
            if settings is None:
                failures.append("the Settings window did not open")
            else:
                settings.set_default_size(min(args.width, 1000), min(args.height, 700))
                pump(0.6)
                for page in ("appearance", "dots", "voice", "model", "servers"):
                    settings.show_page(page)
                    pump(0.8)
                    capture(settings, out / f"settings-{page}{suffix}.png")
                settings.close()
                pump(0.3)
            if args.smoke:
                print("smoke: ok" if not failures else f"smoke: FAILED {failures}")
        except Exception as error:  # report and quit instead of hanging
            import traceback
            traceback.print_exc()
            failures.append(repr(error))
        finally:
            app.quit()
        return False

    def started(_app):
        GLib.timeout_add(300, scenario)

    app.connect("startup", lambda _a: None)
    app.connect("activate", lambda _a: None)
    app.connect_after("command-line", lambda *_: None)
    GLib.idle_add(lambda: (started(app), False)[1])
    status = app.run([sys.argv[0]])
    if fake:
        fake.stop()
    if demo:
        demo.shutdown()
    if failures:
        print(f"failures: {failures}")
    return 1 if failures or status else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--demo", metavar="CLIENT",
                        help="serve tools/demo_server.py with this OpenDots dist/client")
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--name", default="Aurora")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--out", default=str(ROOT / "data" / "screenshots"))
    sys.exit(run(parser.parse_args()))
