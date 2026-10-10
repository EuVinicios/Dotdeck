"""The web view that shows one OpenDots server, with Dotdeck's desktop glue.

Each server profile gets its own WebKit network session (cookies and web storage are
not shared between servers), the owner token from the keyring, native notifications,
the native voice bridge, and a navigation policy that keeps the view on the server's
own origin: every other link opens in the default browser.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Protocol

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, JavaScriptCore, WebKit  # noqa: E402

from .. import config  # noqa: E402
from ..core import server, voice  # noqa: E402
from ..core.store import Profile  # noqa: E402
from ..i18n import _  # noqa: E402

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent


def _script(name: str) -> str:
    return (_WEB_DIR / name).read_text("utf-8")


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


# Small scripts run on the OpenDots page for native shortcuts. They depend on the web
# client's accessible labels, so each one reports whether it found its target.
_CLICK = """
const button = document.querySelector(selector);
if (!button) return false;
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


class ServerView(Adw.Bin):
    """A server page with its own loading and error states."""

    __gtype_name__ = "DotdeckServerView"
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, profile: Profile, token: str | None, host: Host, zoom: float = 1.0):
        super().__init__()
        self.profile = profile
        self._host = host
        self._token = token or ""
        self.failed = False
        self.loaded_once = False

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

        self.voice = voice.VoiceBridge(self._send_voice_event, self._ask_microphone,
                                       host.echo_cancellation)

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
        self.content.remove_all_scripts()
        self._install_scripts()
        self.reload()

    def set_zoom(self, zoom: float) -> None:
        self.webview.set_zoom_level(zoom)

    def run_page_action(self, action: str, argument: str = "",
                        done: Callable[[bool], None] | None = None) -> None:
        """Press a button of the OpenDots page: ``activity``, ``new-chat``, ``mute``, ``dot``."""
        if action == "dot":
            body, args = _OPEN_DOT, {"name": GLib.Variant("s", argument)}
        else:
            selector = {
                "activity": 'button[aria-label="Open activity"]',
                "new-chat": 'button.new-chat, button[aria-label="New chat"]',
                "mute": 'button[aria-label="Mute microphone"], '
                        'button[aria-label="Unmute microphone"]',
                "settings": 'button[aria-label="Open settings"]',
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
        """Mute through the page's own button so its UI stays in sync; else mute natively."""
        if not self.voice.in_call:
            return
        self.run_page_action("mute", done=lambda ok: ok or self.voice.set_mic_enabled(
            not self.voice.mic_enabled))

    def clear_website_data(self, done: Callable[[], None]) -> None:
        manager = self._session.get_website_data_manager()
        manager.clear(WebKit.WebsiteDataTypes.ALL, 0, None,
                      lambda m, r: (m.clear_finish(r), done()))

    def destroy(self) -> None:
        self.voice.close_all()
        self.webview.try_close()

    # -- scripts and messages --------------------------------------------------------------

    def _install_scripts(self) -> None:
        # WebKit match patterns cannot hold a port, so the scripts also check the exact
        # origin themselves before doing anything.
        scheme, host = self.profile.url.split("://", 1)
        allow = [f"{scheme}://{host.rsplit(':', 1)[0] if not host.endswith(']') else host}/*"]
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
        if message.get("type") == "token" and isinstance(message.get("token"), str):
            self._token = message["token"]
            self._host.token_from_page(self.profile, message["token"])

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
        script = f"window.__dotdeckVoiceEvent({json.dumps(pc_id)}, {json.dumps(json.dumps(event))});"
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
        if event == WebKit.LoadEvent.FINISHED and not self.failed:
            self.loaded_once = True
            self.emit("state-changed")

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
