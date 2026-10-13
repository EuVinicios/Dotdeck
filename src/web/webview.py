"""The web view that shows one OpenDots server, with Dotdeck's desktop glue.

Each server profile gets its own WebKit network session (cookies and web storage are
not shared between servers), the owner token from the keyring, native notifications,
the native voice bridge, and a navigation policy that keeps the view on the server's
own origin: every other link opens in the default browser.

The page can also run in focus mode (src/web/focus.js and focus.css): Dotdeck's own
sidebar lists the Dots, the page drops its navigation and is driven through commands
such as :meth:`ServerView.open_dot`, and it reports back what it shows. Pages that are
not recognized as the OpenDots web client are left untouched.
"""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, JavaScriptCore, WebKit  # noqa: E402

from .. import config, i18n  # noqa: E402
from ..core import pets, server, voice  # noqa: E402
from ..core.store import Profile  # noqa: E402
from ..i18n import N_, _  # noqa: E402

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=None)
def _script(name: str) -> str:
    """A file of src/web (read once: the scripts are installed again on every change)."""
    return (_WEB_DIR / name).read_text("utf-8")


@functools.lru_cache(maxsize=None)
def page_translations(language: str | None) -> dict[str, str]:
    """The page's words in Dotdeck's language (translations/<language>.json), or none.

    "pt_BR" falls back to "pt". English, a language without a catalog and an unreadable
    catalog leave the page as it is.
    """
    if not language:
        return {}
    for name in dict.fromkeys((language, language.split("_")[0])):
        path = _WEB_DIR / "translations" / f"{name}.json"
        try:
            strings = json.loads(path.read_text("utf-8"))["strings"]
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.warning("Ignoring the page translations in %s: %s", path, error)
            return {}
        if isinstance(strings, dict):
            return {key: value for key, value in strings.items()
                    if isinstance(key, str) and isinstance(value, str)}
    return {}


def _index(value: int | None) -> int | None:
    """A Dot's place in the page's list, or None when it is not known."""
    return value if isinstance(value, int) and value >= 0 else None


class Host(Protocol):
    """What the view needs from the window around it."""

    def toast(self, text: str, button: str | None = None,
              action: Callable[[], None] | None = None) -> None: ...
    def open_external(self, uri: str) -> None: ...
    def web_notification(self, profile: Profile, title: str, body: str) -> None: ...
    def ask_microphone(self, profile: Profile, callback: Callable[[bool], None]) -> None: ...
    def token_from_page(self, profile: Profile, token: str) -> None: ...
    def edit_profile(self, profile: Profile) -> None: ...
    def echo_cancellation(self) -> bool: ...
    # Optional: def voice_options(self) -> dict (microphone, speaker, mic mode...).


# Small scripts run on the OpenDots page for native shortcuts. They depend on the web
# client's accessible labels, so each one reports whether it found its target. Those labels,
# and the page's questions below, are listed in contract.json (tests/test_contract.py).
_CLICK = """
const button = document.querySelector(selector);
if (!button || button.disabled) return false;
button.click();
return true;
"""

_OPEN_DOT = """
const wanted = name.toLocaleLowerCase();
const pick = () => {
  for (const button of document.querySelectorAll('nav[aria-label="Dots"] button.dot-nav')) {
    const label = button.querySelector('span:last-of-type');
    const text = ((label || button).textContent || '').trim().toLocaleLowerCase();
    if (text === wanted) { button.click(); return true; }
  }
  return false;
};
if (pick()) return true;
const expand = document.querySelector('button[aria-label="Expand dots list"]');
if (!expand) return false;
expand.click();
return await new Promise((resolve) => setTimeout(() => resolve(pick()), 150));
"""

# Runs a command of focus.js (window.__dotdeck) and answers whether it worked.
_COMMAND = """
const page = window.__dotdeck;
if (!page || typeof page.run !== 'function') return false;
return await page.run(command, JSON.parse(args));
"""

# Calls a setter of focus.js on the page that is loaded now.
_SETTER = """
const page = window.__dotdeck;
if (!page || typeof page[method] !== 'function') return null;
return page[method](...JSON.parse(args));
"""

SCHEMES = ("system", "light", "dark")
VIEWS = ("chat", "home", "tasks", "memories", "space")
DOT_STATES = ("idle", "working", "paused")

# Pets drawn on the page: the pictures are 256 px, mascots are at most 180 px there, so
# 160 px is sharp on ordinary screens; HiDPI screens get more (see ServerView).
PET_PIXELS = 160
_pet_uris: dict[tuple[str, int], str] = {}


def pet_data_uri(pet_id: str, pixels: int = PET_PIXELS) -> str | None:
    """The pet's picture as a ``data:`` URI (the page's CSP only allows those)."""
    key = (pet_id, pixels)
    if key in _pet_uris:
        return _pet_uris[key]
    if pets.get(pet_id) is None:
        return None
    path = pets.image_path(pet_id)
    try:
        data = _scaled_png(path, pixels)
    except (OSError, MemoryError, ImportError) as error:
        log.debug("Could not scale pet %s (%s), using it as it is", pet_id, error)
        try:
            data = path.read_bytes()
        except OSError as missing:
            log.warning("No picture for pet %s: %s", pet_id, missing)
            return None
    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    _pet_uris[key] = uri
    return uri


def _scaled_png(path: Path, pixels: int) -> bytes:
    import cairo

    source = cairo.ImageSurface.create_from_png(str(path))
    width, height = source.get_width(), source.get_height()
    if not width or not height:
        raise OSError("empty picture")
    scale = min(1.0, pixels / max(width, height))
    if scale >= 1.0:
        return path.read_bytes()
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, round(width * scale)),
                                max(1, round(height * scale)))
    context = cairo.Context(target)
    context.scale(scale, scale)
    context.set_source_surface(source, 0, 0)
    context.get_source().set_filter(cairo.FILTER_BEST)
    context.paint()
    buffer = io.BytesIO()
    target.write_to_png(buffer)
    return buffer.getvalue()


def pets_style_sheet(pet_ids: set[str] | frozenset[str], pixels: int = PET_PIXELS) -> str:
    """CSS that draws each pet instead of the page's mascots stamped with it (focus.js)."""
    rules = []
    for pet_id in sorted(pet_ids):
        uri = pet_data_uri(pet_id, pixels)
        if uri:
            rules.append(f'span.mascot[data-dotdeck-pet="{pet_id}"] > img.dot-body '
                         f'{{ content: url("{uri}") !important; }}')
    return "\n".join(rules)


class _Known(NamedTuple):
    """How Dotdeck words one of the page's window.confirm/prompt questions."""

    pattern: str        # the page's English message (a full-match regular expression)
    heading: str
    body: str           # may use the pattern's named groups, e.g. {title}
    accept: str
    appearance: Adw.ResponseAppearance
    entry: str | None = None   # prompts: the title of the text field
    check: str = ""            # prompts: "required" (not empty) or "number" (0 or more)


_DESTRUCTIVE = Adw.ResponseAppearance.DESTRUCTIVE
_SUGGESTED = Adw.ResponseAppearance.SUGGESTED

# The questions the OpenDots page asks (src/client upstream), shown with native wording.
_KNOWN_DIALOGS = tuple((re.compile(known.pattern, re.DOTALL), known) for known in (
    _Known(r"Leave your unsaved page draft\?", N_("Leave Unsaved Draft?"),
           N_("The changes to this page will be lost."), N_("_Leave"), _DESTRUCTIVE),
    _Known(r"Page title", N_("Save as Page"),
           N_("The conversation becomes a page in its Space."), N_("_Save"), _SUGGESTED,
           N_("Title"), "required"),
    _Known(r"Repeat interval in minutes \(0 removes the schedule\)", N_("Repeat Task"),
           N_("Minutes between runs. Use 0 to stop repeating."), N_("_Save"), _SUGGESTED,
           N_("Minutes"), "number"),
    _Known(r"Link URL \(https:// or an internal page link\)", N_("Edit Link"),
           N_("A web address (https://) or a link to another page. Leave it empty to remove "
              "the link."), N_("_Apply"), _SUGGESTED, N_("Address")),
    _Known(r"Load the latest saved page and replace this draft\?.*", N_("Replace Draft?"),
           N_("The latest saved version of the page replaces this draft. Download the draft "
              "first to keep it."), N_("_Replace"), _DESTRUCTIVE),
    _Known(r'Delete "(?P<title>.*)"\? This can.t be undone\..*', N_("Delete Page?"),
           N_("“{title}” will be permanently deleted. Its subpages move to its parent page."),
           N_("_Delete"), _DESTRUCTIVE),
    _Known(r"Remove (?P<name>.+) from this Dot\?", N_("Remove Connection?"),
           N_("This Dot will no longer use {name}."), N_("_Remove"), _DESTRUCTIVE),
))
# Other questions that start with one of these verbs get it as a destructive button.
_DESTRUCTIVE_VERBS = {
    "Delete": N_("_Delete"), "Remove": N_("_Remove"), "Discard": N_("_Discard"),
    "Replace": N_("_Replace"), "Reset": N_("_Reset"), "Clear": N_("_Clear"),
    "Leave": N_("_Leave"),
}
_SHORT_MESSAGE = 72


def _known_dialog(message: str) -> tuple[_Known, dict[str, str]] | None:
    for pattern, known in _KNOWN_DIALOGS:
        match = pattern.fullmatch(message)
        if match:
            return known, match.groupdict()
    return None


def _fill(template: str, values: dict[str, str]) -> str:
    """A translated text with the page's values in it (a broken translation shows as is)."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


class ServerView(Adw.Bin):
    """A server page with its own loading and error states.

    Signals:

    - ``focus-changed(active)``: focus mode is on (the page was recognized and its own
      navigation is hidden) or off. Each page load reports once its probe settles (after
      at most ``PROBE_TIMEOUT_MS``), and again whenever it changes. OpenDots' unlock and
      loading screens count as recognized: they have no navigation to hide.
    - ``page-selection(dot_name, view)``: what the page shows now; ``view`` is ``chat``
      (a conversation of ``dot_name``), ``home`` (the empty new-chat screen of
      ``dot_name``), ``tasks``, ``memories`` or ``space`` (``dot_name`` empty).
    - ``dot-state(dot_name, state)``: the open Dot is ``idle``, ``working`` or ``paused``.
    - ``page-thread(thread_id)``: the conversation the page shows ("" for none), for its
      cost in the header.
    """

    __gtype_name__ = "DotdeckServerView"
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "focus-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "page-selection": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "dot-state": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "page-thread": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    # How long a page has to look like the OpenDots web client before focus mode gives up.
    PROBE_TIMEOUT_MS = 8000
    # The pet of mascots that belong to no Dot (loading and unlock screens).
    BRAND_PET = "sky"

    def __init__(self, profile: Profile, token: str | None, host: Host, zoom: float = 1.0):
        super().__init__()
        self.profile = profile
        self._host = host
        self._token = token or ""
        self.failed = False
        self.loaded_once = False

        # Focus mode, selection sync, pets and colour scheme (see focus.js).
        self._focus_wanted = False
        self._focus_active = False
        self.page_recognized: bool | None = None   # None until the page's probe settles
        self.page_selection: tuple[str, str] = ("", "")
        self.page_thread = ""
        self.page_dot_state: tuple[str, str] = ("", "")
        self._scheme = "system"
        self._offline = False
        self._pets: dict[str, str] = {}
        self._pets_enabled = False
        self._pets_css = ""
        self._pets_sheet: WebKit.UserStyleSheet | None = None
        self._pending: list[Callable[[bool], None]] = []
        self._script_dialog: Adw.AlertDialog | None = None
        self._script_entry: Adw.EntryRow | None = None
        self._script_request: WebKit.ScriptDialog | None = None
        self._closing = False

        session = WebKit.NetworkSession.new(str(config.profile_data_dir(profile.id)),
                                            str(config.profile_cache_dir(profile.id)))
        session.set_tls_errors_policy(WebKit.TLSErrorsPolicy.FAIL)
        session.connect("download-started", self._on_download_started)
        self._session = session

        settings = WebKit.Settings()
        settings.set_enable_developer_extras(config.DEVEL)
        settings.set_enable_media_stream(True)
        settings.set_enable_webrtc(True)
        settings.set_media_playback_requires_user_gesture(False)
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_back_forward_navigation_gestures(True)
        settings.set_user_agent_with_application_details(config.APP_NAME, config.VERSION)

        self.content = WebKit.UserContentManager()
        self._install_scripts()
        # Inert until focus.js marks a recognized page with data-dotdeck-focus.
        self.content.add_style_sheet(WebKit.UserStyleSheet.new(
            _script("focus.css"), WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserStyleLevel.USER, self._allow_list(), None))

        self.webview = WebKit.WebView(network_session=session, settings=settings,
                                      user_content_manager=self.content)
        self.webview.set_zoom_level(zoom)
        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("create", self._on_create)
        self.webview.connect("permission-request", self._on_permission_request)
        self.webview.connect("show-notification", self._on_show_notification)
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("load-failed", self._on_load_failed)
        self.webview.connect("load-failed-with-tls-errors", self._on_tls_error)
        self.webview.connect("web-process-terminated", self._on_crashed)
        self.webview.connect("notify::estimated-load-progress", self._on_progress)
        self.webview.connect("script-dialog", self._on_script_dialog)
        self.connect("notify::scale-factor", lambda *_: self._update_pets_sheet())

        self.voice = voice.VoiceBridge(self._send_voice_event, self._ask_microphone,
                                       host.echo_cancellation,
                                       voice_options=getattr(host, "voice_options", None))

        self._progress = Gtk.ProgressBar(valign=Gtk.Align.START, hexpand=True, visible=False)
        self._progress.add_css_class("osd")
        overlay = Gtk.Overlay(child=self.webview)
        overlay.add_overlay(self._progress)

        self._error = Adw.StatusPage(icon_name="network-offline-symbolic")
        retry = Gtk.Button(label=_("Try Again"), halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda _b: self.reload())
        edit = Gtk.Button(label=_("Edit Server…"), halign=Gtk.Align.CENTER)
        edit.add_css_class("pill")
        edit.connect("clicked", lambda _b: self._host.edit_profile(self.profile))
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        buttons.append(retry)
        buttons.append(edit)
        self._error.set_child(buttons)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(overlay, "web")
        self._stack.add_named(self._error, "error")
        self.set_child(self._stack)

        self.webview.load_uri(f"{profile.url}/")

    # -- public ----------------------------------------------------------------------------

    def reload(self) -> None:
        self.failed = False
        self._stack.set_visible_child_name("web")
        uri = self.webview.get_uri()
        if uri and server.origin_of(uri) == self.profile.url and self.loaded_once:
            self.webview.reload()
        else:
            self.webview.load_uri(f"{self.profile.url}/")
        self.emit("state-changed")

    def set_token(self, token: str | None) -> None:
        """Use a new token (after it was changed in Preferences) and reload."""
        self._token = token or ""
        self._reinstall_scripts()
        self.reload()

    def set_zoom(self, zoom: float) -> None:
        self.webview.set_zoom_level(zoom)

    def run_page_action(self, action: str, argument: str = "",
                        done: Callable[[bool], None] | None = None) -> None:
        """Press a button of the OpenDots page and tell whether it was there to press.

        ``activity``, ``new-chat``, ``mute``, ``settings``, ``dot`` (``argument`` = name), and
        for the open conversation, whose own header focus mode hides: ``save-page`` (save it
        as a page of its Space) and ``schedule`` (schedule a task in it).
        """
        if action == "dot":
            body, args = _OPEN_DOT, {"name": GLib.Variant("s", argument)}
        else:
            selector = {
                "activity": 'button[aria-label="Open activity"]',
                "new-chat": 'button.new-chat, button[aria-label="New chat"]',
                "mute": 'button[aria-label="Mute microphone"], '
                        'button[aria-label="Unmute microphone"]',
                "settings": 'button[aria-label="Open settings"]',
                "save-page": '.chat-persona-actions '
                             'button[aria-label="Save conversation as page"]',
                "schedule": '.chat-persona-actions '
                            'button[aria-label="Schedule a task in this conversation"]',
            }.get(action)
            if selector is None:
                raise ValueError(action)
            body, args = _CLICK, {"selector": GLib.Variant("s", selector)}

        def finished(view, result):
            ok = False
            try:
                value = view.call_async_javascript_function_finish(result)
                ok = bool(value and value.is_boolean() and value.to_boolean())
            except GLib.Error as error:
                log.debug("Page action %s failed: %s", action, error.message)
            if done:
                done(ok)

        self.webview.call_async_javascript_function(body, -1, GLib.Variant("a{sv}", args), None,
                                                    None, None, finished)

    def toggle_mute(self) -> None:
        """Mute through the page's own button so its UI stays in sync; else mute natively.

        With push-to-talk or "join muted" the native microphone starts muted while the page
        still shows it on, so the page's button is only pressed when it agrees with us.
        """
        if not self.voice.in_call:
            return
        want_enabled = not self.voice.mic_enabled

        def native() -> None:
            self.voice.set_mic_enabled(want_enabled)

        def got_state(view, result) -> None:
            page_on = None
            try:
                value = view.call_async_javascript_function_finish(result)
                if value is not None and value.is_boolean():
                    page_on = value.to_boolean()
            except GLib.Error:
                pass
            # page_on: the page believes the microphone is on. Its button flips it.
            if page_on is None or page_on == want_enabled:
                native()
            else:
                self.run_page_action("mute", done=lambda ok: ok or native())

        self.webview.call_async_javascript_function(
            "const b = document.querySelector('button[aria-label=\"Mute microphone\"], "
            "button[aria-label=\"Unmute microphone\"]');"
            " return b ? b.getAttribute('aria-label') === 'Mute microphone' : null;",
            -1, None, None, None, None, got_state)

    def clear_website_data(self, done: Callable[[], None]) -> None:
        manager = self._session.get_website_data_manager()
        manager.clear(WebKit.WebsiteDataTypes.ALL, 0, None,
                      lambda m, r: (m.clear_finish(r), done()))

    def destroy(self) -> None:
        self._closing = True
        self._dismiss_script_dialog()
        self._flush_pending(False)
        self.voice.close_all()
        self.webview.try_close()

    # -- focus mode, page commands, pets and theme -----------------------------------------

    @property
    def focus_active(self) -> bool:
        """Whether the page is recognized and runs without its own navigation."""
        return self._focus_active

    def set_focus_mode(self, enabled: bool) -> None:
        """Hide the page's navigation (once recognized), or bring it back, live."""
        enabled = bool(enabled)
        if enabled == self._focus_wanted:
            return
        self._focus_wanted = enabled
        self._reinstall_scripts()

        def applied(_result: Any) -> None:
            # Turning focus on is answered by the page (now, or when its probe settles);
            # turning it off always works, even on a page that is not driven.
            if not enabled and self._focus_active:
                self._focus_active = False
                self.emit("focus-changed", False)
        self._page_set("setFocus", [enabled], applied)

    def open_dot(self, index: int, name: str, done: Callable[[bool], None] | None = None) -> None:
        """Open a Dot: ``index`` in the page's list, checked by name.

        The page shows the Dot's latest conversation, or keeps the conversation of a call
        in progress with that Dot (leaving it would end the call).
        """
        self._command("openDot", [_index(index), name or ""], done)

    def open_conversation(self, conversation_id: str, dot_index: int, dot_name: str,
                          done: Callable[[bool], None] | None = None) -> None:
        """Open one conversation (the page selects its Dot by itself).

        A conversation the page does not know (yet) is waited for briefly; when it never
        shows up, the Dot is opened instead and ``done(False)``.
        """
        self._command("openConversation", [conversation_id, _index(dot_index), dot_name or ""],
                      done)

    def new_chat(self, dot_index: int, dot_name: str,
                 done: Callable[[bool], None] | None = None) -> None:
        """Show a Dot's empty composer; the conversation is created when the owner writes."""
        self._command("newChat", [_index(dot_index), dot_name or ""], done)

    def open_space(self, space_id: str, page_id: str | None = None,
                   done: Callable[[bool], None] | None = None) -> None:
        self._command("openSpace", [space_id, page_id or ""], done)

    def open_activity(self, done: Callable[[bool], None] | None = None) -> None:
        self._command("openActivity", [], done)

    def open_memories(self, done: Callable[[bool], None] | None = None) -> None:
        self._command("openMemories", [], done)

    def open_web_settings(self, done: Callable[[bool], None] | None = None) -> None:
        """The page's own settings dialog (it has no sections to open directly)."""
        self._command("openWebSettings", [], done)

    def edit_dot_in_page(self, name: str, done: Callable[[bool], None] | None = None) -> None:
        """The page's own editor of a Dot (its MCP connections and skills live there)."""
        self._command("editDot", [name or ""], done)

    def toggle_computer(self, done: Callable[[bool], None] | None = None) -> None:
        """Show or hide the panel with the Dot's computer and findings."""
        self._command("toggleComputer", [], done)

    def start_call(self, conversation_id: str | None, dot_index: int, dot_name: str,
                   done: Callable[[bool], None] | None = None) -> None:
        """Open the conversation (or the Dot's latest one) and press its call button.

        The button is enabled once voice is set up, the Dots are not paused and the chat
        has loaded; ``done(False)`` when that does not happen within 10 seconds.
        """
        self._command("startCall", [conversation_id or "", _index(dot_index), dot_name or ""],
                      done)

    def end_call(self, done: Callable[[bool], None] | None = None) -> None:
        """Hang up through the page, which then saves the call's transcript."""
        self._command("endCall", [], done)

    def set_pets(self, pets_by_name: dict[str, str], enabled: bool) -> None:
        """Draw each Dot's pet instead of the page's mascots (``{dot name: pet id}``)."""
        mapping = {str(name): pet for name, pet in (pets_by_name or {}).items()
                   if isinstance(name, str) and name and pets.get(pet) is not None}
        enabled = bool(enabled)
        if mapping == self._pets and enabled == self._pets_enabled:
            return
        self._pets, self._pets_enabled = mapping, enabled
        self._update_pets_sheet()
        self._reinstall_scripts()
        self._page_set("setPets", [mapping, enabled])

    def _update_pets_sheet(self) -> None:
        """The style sheet with the pictures of the pets in use, sharp at the screen's scale."""
        pixels = min(256, PET_PIXELS * max(1, self.get_scale_factor()))
        css = pets_style_sheet(set(self._pets.values()) | {self.BRAND_PET}, pixels) \
            if self._pets_enabled else ""
        if css == self._pets_css:
            return
        if self._pets_sheet is not None:
            self.content.remove_style_sheet(self._pets_sheet)
            self._pets_sheet = None
        if css:
            self._pets_sheet = WebKit.UserStyleSheet.new(
                css, WebKit.UserContentInjectedFrames.TOP_FRAME, WebKit.UserStyleLevel.USER,
                self._allow_list(), None)
            self.content.add_style_sheet(self._pets_sheet)
        self._pets_css = css

    def sync_color_scheme(self, scheme: str) -> None:
        """Make the page follow Dotdeck's colour scheme (``system`` lets it follow the desktop)."""
        scheme = scheme if scheme in SCHEMES else "system"
        if scheme == self._scheme:
            return
        self._scheme = scheme
        self._reinstall_scripts()
        self._page_set("setTheme", [scheme])

    def set_offline(self, offline: bool) -> None:
        """Dotdeck says the server is out of reach: the page's own error bar steps aside."""
        offline = bool(offline)
        if offline == self._offline:
            return
        self._offline = offline
        self._reinstall_scripts()
        self._page_set("setOffline", [offline])

    def _command(self, command: str, args: list, done: Callable[[bool], None] | None) -> None:
        """Run a focus.js command once the page has loaded; ``done(ok)`` exactly once."""
        def finish(ok: bool) -> None:
            if done is not None:
                done(ok)

        def run(ready: bool) -> None:
            if not ready or not self._trusted_page():
                finish(False)
                return
            variant = GLib.Variant("a{sv}", {"command": GLib.Variant("s", command),
                                             "args": GLib.Variant("s", json.dumps(args))})

            def finished(view, result) -> None:
                ok = False
                try:
                    value = view.call_async_javascript_function_finish(result)
                    ok = bool(value and value.is_boolean() and value.to_boolean())
                except GLib.Error as error:
                    log.debug("Page command %s failed: %s", command, error.message)
                if not ok:
                    log.debug("Page command %s did not work", command)
                finish(ok)
            self.webview.call_async_javascript_function(_COMMAND, -1, variant, None, None, None,
                                                        finished)

        if self.failed:
            GLib.idle_add(lambda: finish(False) or GLib.SOURCE_REMOVE)
        elif self.webview.is_loading():
            self._pending.append(run)
        else:
            run(True)

    def _flush_pending(self, ready: bool) -> None:
        pending, self._pending = self._pending, []
        for run in pending:
            run(ready)

    def _page_set(self, method: str, args: list,
                  done: Callable[[Any], None] | None = None) -> None:
        """Apply a setting to the page loaded now (later loads get it from the scripts)."""
        if not self._trusted_page():
            if done:
                done(None)
            return
        variant = GLib.Variant("a{sv}", {"method": GLib.Variant("s", method),
                                         "args": GLib.Variant("s", json.dumps(args))})

        def finished(view, result) -> None:
            value = None
            try:
                answer = view.call_async_javascript_function_finish(result)
                if answer is not None and not answer.is_undefined() and not answer.is_null():
                    value = json.loads(answer.to_json(0))
            except (GLib.Error, TypeError, ValueError) as error:
                log.debug("Page setting %s failed: %s", method, error)
            if done:
                done(value)
        self.webview.call_async_javascript_function(_SETTER, -1, variant, None, None, None,
                                                    finished)

    def _on_focus_report(self, message: dict) -> None:
        active = bool(message.get("active"))
        self.page_recognized = bool(message.get("recognized"))
        changed = active != self._focus_active
        self._focus_active = active
        # A settled probe (or a request to turn focus on) is always answered, so the
        # window learns about pages it cannot drive.
        if changed or (self._focus_wanted and message.get("reason") in ("probe", "set")):
            self.emit("focus-changed", active)

    def _on_selection_report(self, message: dict) -> None:
        dot, view = message.get("dot"), message.get("view")
        if not isinstance(dot, str) or view not in VIEWS:
            return
        if (dot, view) != self.page_selection:
            self.page_selection = (dot, view)
            self.emit("page-selection", dot, view)
        thread = message.get("thread")
        thread = thread[:200] if isinstance(thread, str) and view == "chat" else ""
        if thread != self.page_thread:
            self.page_thread = thread
            self.emit("page-thread", thread)

    def _on_dot_state_report(self, message: dict) -> None:
        dot, state = message.get("dot"), message.get("state")
        if not isinstance(dot, str) or not isinstance(state, str):
            return
        state = state if state in DOT_STATES else "idle"
        if (dot, state) != self.page_dot_state:
            self.page_dot_state = (dot, state)
            self.emit("dot-state", dot, state)

    # -- native dialogs for window.alert/confirm/prompt ------------------------------------

    def _on_script_dialog(self, _view, request: WebKit.ScriptDialog) -> bool:
        kind = request.get_dialog_type()
        types = WebKit.ScriptDialogType
        if kind not in (types.ALERT, types.CONFIRM, types.PROMPT, types.BEFORE_UNLOAD_CONFIRM):
            return False
        if self._closing or self.get_root() is None:
            # The view is going away (try_close() asks the page first): nobody is there to
            # answer, so the page may close, and anything else it asks is declined.
            if kind in (types.CONFIRM, types.BEFORE_UNLOAD_CONFIRM):
                request.confirm_set_confirmed(kind == types.BEFORE_UNLOAD_CONFIRM)
            return True
        self._dismiss_script_dialog()
        # Holding a reference makes the page wait until close() (WebKit's async dialogs).
        held = request.ref()
        message = (request.get_message() or "").strip()
        found = _known_dialog(message) if kind != types.ALERT else None
        short = len(message) <= _SHORT_MESSAGE and "\n" not in message
        appearance, entry_title, check = _SUGGESTED, None, ""

        if kind == types.BEFORE_UNLOAD_CONFIRM:
            heading, body = _("Leave This Page?"), _("Changes you made may not be saved.")
            accept, appearance = _("_Leave"), _DESTRUCTIVE
        elif found is not None:
            known, values = found
            heading, body, accept = _(known.heading), _fill(_(known.body), values), _(known.accept)
            appearance, check = known.appearance, known.check
            entry_title = _(known.entry) if known.entry else None
        else:
            heading = message if short else self.profile.name
            body = "" if short else message
            verb = _DESTRUCTIVE_VERBS.get(message.split(" ", 1)[0]) \
                if kind == types.CONFIRM else None
            accept, appearance = (_(verb), _DESTRUCTIVE) if verb else (_("_OK"), _SUGGESTED)
        if not heading:
            heading = self.profile.name

        dialog = Adw.AlertDialog(heading=heading, body=body)
        entry = None
        if kind == types.ALERT:
            dialog.add_response("ok", _("_OK"))
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
        else:
            dialog.add_response("cancel", _("_Cancel") if kind != types.BEFORE_UNLOAD_CONFIRM
                                else _("_Stay"))
            dialog.add_response("ok", accept)
            dialog.set_response_appearance("ok", appearance)
            # A destructive answer is never the one Enter picks.
            dialog.set_default_response("cancel" if appearance == _DESTRUCTIVE else "ok")
            dialog.set_close_response("cancel")
        if kind == types.PROMPT:
            entry = Adw.EntryRow(title=entry_title or "", activates_default=True)
            if check == "number":
                entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
            entry.set_text(request.prompt_get_default_text() or "")
            if check:
                def validate(row: Adw.EntryRow) -> None:
                    ok = self._prompt_valid(row.get_text(), check)
                    dialog.set_response_enabled("ok", ok)
                    if ok or not row.get_text():
                        row.remove_css_class("error")
                    else:
                        row.add_css_class("error")
                entry.connect("changed", validate)
                validate(entry)
            group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            group.add_css_class("boxed-list")
            group.append(entry)
            dialog.set_extra_child(group)
        dialog.connect("response", self._on_script_response, held, entry)
        self._script_dialog, self._script_entry, self._script_request = dialog, entry, held
        dialog.present(self)
        if entry is not None:
            entry.grab_focus()
        return True

    @staticmethod
    def _prompt_valid(text: str, check: str) -> bool:
        text = text.strip()
        if check == "number":
            if not re.fullmatch(r"[0-9]*[.,]?[0-9]+|[0-9]+[.,]?", text):
                return False
            value = float(text.replace(",", "."))
            return math.isfinite(value) and value >= 0
        return bool(text) if check == "required" else True

    def _on_script_response(self, dialog: Adw.AlertDialog, response: str,
                            held: WebKit.ScriptDialog, entry: Adw.EntryRow | None) -> None:
        if self._script_dialog is not dialog:
            return  # Already answered (closing after a response emits one more).
        self._script_dialog = self._script_entry = self._script_request = None
        accepted = response == "ok"
        kind = held.get_dialog_type()
        types = WebKit.ScriptDialogType
        if kind == types.PROMPT:
            if accepted and entry is not None:
                text = entry.get_text()
                # The page reads numbers with Number(), which wants a dot.
                held.prompt_set_text(text.strip().replace(",", ".")
                                     if entry.get_input_purpose() == Gtk.InputPurpose.NUMBER
                                     else text)
        elif kind in (types.CONFIRM, types.BEFORE_UNLOAD_CONFIRM):
            held.confirm_set_confirmed(accepted)
        held.close()

    def _dismiss_script_dialog(self) -> None:
        """Close an open page question as cancelled, so the page does not wait forever.

        ``force_close()`` emits no response, so the page is answered here.
        """
        dialog, held = self._script_dialog, self._script_request
        self._script_dialog = self._script_entry = self._script_request = None
        if held is not None:
            if held.get_dialog_type() in (WebKit.ScriptDialogType.CONFIRM,
                                          WebKit.ScriptDialogType.BEFORE_UNLOAD_CONFIRM):
                held.confirm_set_confirmed(False)
            held.close()
        if dialog is not None:
            dialog.force_close()

    # -- scripts and messages --------------------------------------------------------------

    def _allow_list(self) -> list[str]:
        # WebKit match patterns cannot hold a port, so the scripts also check the exact
        # origin themselves before doing anything.
        scheme, host = self.profile.url.split("://", 1)
        return [f"{scheme}://{host.rsplit(':', 1)[0] if not host.endswith(']') else host}/*"]

    def _reinstall_scripts(self) -> None:
        """Scripts are fixed once added: replace them so the next page load gets new settings."""
        self.content.remove_all_scripts()
        self._install_scripts()

    def _install_scripts(self) -> None:
        allow = self._allow_list()
        origin = json.dumps(self.profile.url)
        bridge = _script("bridge.js").replace("__DOTDECK_CONFIG__", json.dumps({
            "origin": self.profile.url, "token": self._token,
            "storageKey": config.TOKEN_STORAGE_KEY}))
        frames = WebKit.UserContentInjectedFrames.TOP_FRAME
        start = WebKit.UserScriptInjectionTime.START
        self.content.add_script(WebKit.UserScript.new(bridge, frames, start, allow, None))
        if voice.available():
            polyfill = _script("rtc-polyfill.js").replace("__DOTDECK_ORIGIN__", origin)
            self.content.add_script(WebKit.UserScript.new(polyfill, frames, start, allow, None))
        focus = _script("focus.js").replace("__DOTDECK_FOCUS_CONFIG__", json.dumps({
            "origin": self.profile.url, "focus": self._focus_wanted, "theme": self._scheme,
            "offline": self._offline, "pets": self._pets, "petsEnabled": self._pets_enabled,
            "brandPet": self.BRAND_PET, "probeTimeout": self.PROBE_TIMEOUT_MS,
            "storageKey": config.TOKEN_STORAGE_KEY,
            "translations": page_translations(i18n.language())}))
        self.content.add_script(WebKit.UserScript.new(focus, frames, start, allow, None))
        if not getattr(self, "_handlers_ready", False):
            self.content.register_script_message_handler("dotdeck", None)
            self.content.connect("script-message-received::dotdeck", self._on_message)
            self.content.register_script_message_handler_with_reply("dotdeckVoice", None)
            self.content.connect("script-message-with-reply-received::dotdeckVoice",
                                 self._on_voice_message)
            self._handlers_ready = True

    def _trusted_page(self) -> bool:
        uri = self.webview.get_uri() if hasattr(self, "webview") else None
        return bool(uri) and server.origin_of(uri) == self.profile.url

    def _on_message(self, _manager, value: JavaScriptCore.Value) -> None:
        if not self._trusted_page():
            return
        try:
            message = json.loads(value.to_string())
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "token" and isinstance(message.get("token"), str):
            self._token = message["token"]
            self._host.token_from_page(self.profile, message["token"])
        elif kind == "focus":
            self._on_focus_report(message)
        elif kind == "selection":
            self._on_selection_report(message)
        elif kind == "dot-state":
            self._on_dot_state_report(message)

    def _on_voice_message(self, _manager, value: JavaScriptCore.Value,
                          reply: WebKit.ScriptMessageReply) -> bool:
        context = value.get_context()

        def respond(result, error):
            if error:
                reply.return_error_message(error)
            else:
                reply.return_value(JavaScriptCore.Value.new_string(context,
                                                                   json.dumps(result or {})))

        if not self._trusted_page():
            respond(None, "Not allowed on this page.")
            return True
        try:
            request = json.loads(value.to_string())
        except (TypeError, ValueError):
            respond(None, "Malformed request.")
            return True
        self.voice.handle(request, respond)
        return True

    def _send_voice_event(self, pc_id: str, event: dict) -> None:
        payload = json.dumps(json.dumps(event))
        script = f"window.__dotdeckVoiceEvent({json.dumps(pc_id)}, {payload});"
        self.webview.evaluate_javascript(script, -1, None, None, None, None)

    def _ask_microphone(self, callback: Callable[[bool], None]) -> None:
        self._host.ask_microphone(self.profile, callback)

    # -- WebKit signals --------------------------------------------------------------------

    def _on_decide_policy(self, _view, decision, kind) -> bool:
        if kind not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION,
                        WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            if kind == WebKit.PolicyDecisionType.RESPONSE and not decision.is_mime_type_supported():
                decision.download()
                return True
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri()
        if uri.startswith(("about:", "blob:", "data:")) and kind == \
                WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        if server.origin_of(uri) == self.profile.url:
            if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                self.webview.load_uri(uri)
                return True
            return False
        decision.ignore()
        if action.is_user_gesture() or kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            self._host.open_external(uri)
        return True

    def _on_create(self, _view, action: WebKit.NavigationAction):
        """window.open(): same-origin pages load here, anything else in the browser."""
        uri = action.get_request().get_uri()
        if server.origin_of(uri) == self.profile.url:
            self.webview.load_uri(uri)
        elif uri and not uri.startswith(("about:", "blob:", "data:", "javascript:")):
            self._host.open_external(uri)
        return None

    def _on_permission_request(self, _view, request) -> bool:
        if isinstance(request, WebKit.NotificationPermissionRequest):
            request.allow()
        elif isinstance(request, WebKit.ClipboardPermissionRequest):
            request.allow()
        elif isinstance(request, WebKit.UserMediaPermissionRequest) and \
                WebKit.user_media_permission_is_for_audio_device(request) and \
                not WebKit.user_media_permission_is_for_video_device(request):
            self._host.ask_microphone(self.profile,
                                      lambda ok: request.allow() if ok else request.deny())
        else:
            request.deny()
        return True

    def _on_show_notification(self, _view, notification: WebKit.Notification) -> bool:
        self._host.web_notification(self.profile, notification.get_title() or config.APP_NAME,
                                    notification.get_body() or "")
        return True

    def _on_load_changed(self, view, event) -> None:
        if event == WebKit.LoadEvent.COMMITTED:
            self.page_recognized = None
            # A new document (F5, a new token, a link): the page that ran the calls is gone
            # and nothing would end them, so the native call would keep the microphone and
            # the speakers going with no page. They end here.
            if self.voice.has_calls:
                self.voice.close_all()
        if event == WebKit.LoadEvent.FINISHED:
            if not self.failed:
                self.loaded_once = True
                self.emit("state-changed")
            self._flush_pending(not self.failed)

    def _on_progress(self, view, _pspec) -> None:
        progress = view.get_estimated_load_progress()
        self._progress.set_fraction(progress)
        self._progress.set_visible(0.0 < progress < 1.0)

    def _show_error(self, title: str, description: str, icon: str) -> None:
        self.failed = True
        self.voice.close_all()
        self._error.set_icon_name(icon)
        self._error.set_title(title)
        self._error.set_description(description)
        self._stack.set_visible_child_name("error")
        self._flush_pending(False)
        self.emit("state-changed")

    def _on_load_failed(self, _view, _event, uri, error: GLib.Error) -> bool:
        if error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or \
                error.matches(WebKit.policy_error_quark(),
                              WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
            return False
        if server.origin_of(uri) != self.profile.url:
            return False
        self._show_error(_("Can't Reach {name}").format(name=self.profile.name),
                         server.describe(server.UNREACHABLE, self.profile.url),
                         "network-offline-symbolic")
        return True

    def _on_tls_error(self, _view, uri, _certificate, _errors) -> bool:
        self._show_error(_("Untrusted Connection"),
                         _("The security certificate of {host} is not valid, so Dotdeck did not "
                           "open it. Use a trusted certificate, for example with Tailscale "
                           "HTTPS.").format(host=server.host_label(self.profile.url)),
                         "channel-insecure-symbolic")
        return True

    def _on_crashed(self, _view, reason) -> None:
        if reason == WebKit.WebProcessTerminationReason.TERMINATED_BY_API:
            return
        self._show_error(_("The Page Stopped"),
                         _("The page of {name} closed unexpectedly. Try again to reload it.")
                         .format(name=self.profile.name), "computer-fail-symbolic")

    def _on_download_started(self, _session, download: WebKit.Download) -> None:
        download.connect("decide-destination", self._on_decide_destination)
        download.connect("finished", lambda d: self._host.toast(
            _("Saved {name}").format(name=Path(d.get_destination() or "").name or _("file"))))

    def _on_decide_destination(self, download: WebKit.Download, suggested: str) -> bool:
        dialog = Gtk.FileDialog(initial_name=suggested or "download")
        window = self.get_root()

        def chosen(dialog, result):
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                download.cancel()
                return
            download.set_destination(file.get_path())

        dialog.save(window if isinstance(window, Gtk.Window) else None, None, chosen)
        return True
