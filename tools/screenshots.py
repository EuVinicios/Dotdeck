#!/usr/bin/env python3
"""Drive Dotdeck through its main screens and save screenshots (also a UI smoke test).

    python3 tools/screenshots.py [--smoke] [--dark] [--url URL] [--out DIR]

Without ``--url`` it serves a stand-in OpenDots page (tests/fake_server.py). Point it at a
demo OpenDots instance to capture the real web client. It never touches your own config
or keyring: everything lives in a temporary folder and tokens stay in memory.
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

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from src import main as dotdeck  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402

DEMO_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>OpenDots</title>
<style>
 body{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f8f7f4;color:#1d1d1f;display:flex;height:100vh}
 @media (prefers-color-scheme: dark){body{background:#17181c;color:#ececf1}nav{background:#1f2026!important}
 .card{background:#22242b!important}}
 nav{width:240px;background:#efede8;padding:16px;box-sizing:border-box}
 nav h4{font-size:11px;letter-spacing:.08em;opacity:.6;margin:18px 0 8px}
 nav button{display:flex;gap:10px;align-items:center;width:100%;border:0;background:none;
  padding:8px;border-radius:10px;font:inherit;color:inherit;cursor:pointer}
 nav button.active{background:rgba(63,132,246,.14)}
 .dot{width:22px;height:22px;border-radius:50%}
 main{flex:1;padding:32px 48px;overflow:auto}
 .card{background:#fff;border-radius:16px;padding:20px 24px;margin:0 0 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .me{margin-left:auto;max-width:70%;background:#3f84f6;color:#fff}
 textarea{width:100%;border-radius:14px;border:1px solid rgba(127,127,127,.3);padding:14px;font:inherit;
  background:transparent;color:inherit;box-sizing:border-box}
</style></head><body>
<nav aria-label="Workspace navigation"><button class="new-chat" aria-label="New chat">＋ New chat</button>
<h4>DOTS (4)</h4>
<nav aria-label="Dots">
 <button class="dot-nav active"><span class="dot" style="background:#3f84f6"></span><span>Chief</span></button>
 <button class="dot-nav"><span class="dot" style="background:#3fd0b6"></span><span>Researcher</span></button>
 <button class="dot-nav"><span class="dot" style="background:#ff7b5c"></span><span>Writer</span></button>
 <button class="dot-nav"><span class="dot" style="background:#ffc94a"></span><span>Analyst</span></button>
</nav>
<h4>SPACES</h4><button>📁 Product launch</button><button>📁 Research notes</button>
<button aria-label="Open activity">⏱ Activity</button></nav>
<main><h2>Chief</h2>
<div class="card">Good morning! The weekly market summary is ready in <b>Research notes</b>,
and Writer is drafting the launch announcement. Want me to schedule a review call at 3 pm?</div>
<div class="card me">Yes, and ask Analyst to add last week's numbers.</div>
<div class="card">Done. Analyst is on it — I'll notify you when the report is saved.</div>
<textarea rows="3" placeholder="Message Chief…"></textarea></main>
</body></html>"""


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
    rect = Graphene.Rect().init(0, 0, width, height)
    texture = window.get_renderer().render_texture(node, rect)
    texture.save_to_png(str(path))
    print(path)


def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fake = None
    url = args.url
    if not url:
        fake = FakeOpenDots(name="OpenDots Demo", dots=["Chief", "Researcher", "Writer", "Analyst"],
                            page=DEMO_PAGE).start()
        fake.tasks = [{"id": "t1", "prompt": "Weekly market summary", "status": "running"}]
        url = fake.url

    app = dotdeck.DotdeckApplication()
    suffix = "-dark" if args.dark else ""
    failures = []

    def scenario():
        try:
            app.settings.set("color-scheme", "dark" if args.dark else "light")
            window = app.window
            window.set_default_size(1240, 780)
            pump(0.8)
            capture(window, out / f"welcome{suffix}.png")

            window.onboarding._open_server("")
            pump(0.5)
            form = window.onboarding.nav.get_visible_page().get_child().get_content() \
                .get_child().get_child().get_child()
            form.address.set_text(url.replace("http://", ""))
            form.name.set_text("Homelab")
            form.submit()
            if not wait(lambda: window.onboarding.nav.get_visible_page().get_tag() == "ready", 20):
                failures.append("onboarding did not reach the ready page")
            capture(window, out / f"connected{suffix}.png")

            ready = window.onboarding.nav.get_visible_page()
            app.settings.set("global-shortcut", False)  # no portal dialogs in CI
            window.onboarding.emit("finished", window.onboarding._profile_id)
            if not wait(lambda: window.view is not None and window.view.loaded_once, 30):
                failures.append("the server page did not load")
            wait(lambda: app.monitors[app.profiles.active.id].status == "online", 15)
            pump(2.0)
            capture(window, out / f"workspace{suffix}.png")
            del ready

            app.show_preferences("general")
            pump(0.8)
            capture(window, out / f"preferences{suffix}.png")
            dialog = window.get_visible_dialog()
            dialog.set_visible_page_name("voice")
            pump(0.5)
            capture(window, out / f"voice{suffix}.png")
            dialog.close()
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
    return 1 if failures or status else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--out", default=str(ROOT / "data" / "screenshots"))
    sys.exit(run(parser.parse_args()))
