"""YOUR DOTS › Voice Calls: readiness, devices, sound processing and the Dots' voice."""

from __future__ import annotations

import inspect
import logging
import threading
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ...core import store, voice
from ...i18n import _
from .common import SettingsPage, escape, status_icon, string_model, suffix_label

log = logging.getLogger(__name__)

MIC_CHOICES = [store.MIC_ASK, store.MIC_ALLOW, store.MIC_DENY]
# The OpenAI Realtime voices; the Dotdeck voice gateway maps them for OpenRouter.
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
          "verse")


def is_gateway(config: dict | None) -> bool:
    """Calls go through the Dotdeck voice gateway (an OpenRouter model id has a "/")."""
    if not config:
        return False
    return "/" in str(config.get("voiceModel") or "") or bool(config.get("voiceGateway"))


class VoicePage(SettingsPage):
    name = "voice"
    title = _("Voice Calls")
    icon = "call-start-symbolic"

    def build(self) -> None:
        self._mic_test = None
        self._speaker_test = None
        self._visible = False
        self._devices: dict[str, list[str]] = {"source": [""], "sink": [""]}
        self._default_names: dict[str, str | None] = {"source": None, "sink": None}
        self._device_request = 0
        self._after_devices: list[Callable[[], None]] = []
        self._suspend_handler = 0
        self._syncing = False
        self._voice_signature = None
        self._voice_rows: list[Gtk.Widget] = []

        top = self.group()
        self.selector = self.add(top, self.win.server_selector(), ("server",))
        top.set_visible(self.selector.get_visible())
        self.selector.connect("notify::visible",
                              lambda row, _p: top.set_visible(row.get_visible()))

        self.status = self.group(_("Status"))
        self.server_row = Adw.ActionRow(title=_("Server"), subtitle_lines=3)
        self.server_icon = status_icon("content-loading-symbolic")
        self.server_row.add_prefix(self.server_icon)
        self.add(self.status, self.server_row, ("ready", "configured", "gateway", "openrouter"))
        missing = voice.missing_elements()
        engine = Adw.ActionRow(
            title=_("Voice Engine"), subtitle_lines=3,
            subtitle=_("Ready: GStreamer with WebRTC, through PipeWire or PulseAudio")
            if not missing else _("Missing GStreamer plugins: {names}").format(
                names=", ".join(missing)))
        engine.add_prefix(status_icon("object-select-symbolic" if not missing
                                      else "dialog-warning-symbolic",
                                      "success" if not missing else "warning"))
        self.add(self.status, engine, ("gstreamer", "webrtc", "plugins", "engine"))
        # Shown when the background check finds the snap's microphone not connected.
        self.snap_row = Adw.ActionRow(title=_("Microphone Blocked by the Snap"),
                                      subtitle="snap connect dotdeck:audio-record",
                                      subtitle_selectable=True, visible=False)
        self.snap_row.add_prefix(status_icon("dialog-warning-symbolic", "warning"))
        self.add(self.status, self.snap_row, ("snap", "audio-record", "permission"))

        devices = self.group(_("Microphone and Speaker"),
                             _("Changes apply to the next call."))
        self.mic_combo = self._device_row(devices, "source", "microphone-device",
                                          _("Microphone"), ("input", "mic", "headset"))
        self.speaker_combo = self._device_row(devices, "sink", "speaker-device",
                                              _("Speaker"), ("output", "headphones",
                                                             "speakers"))
        self.level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        self.level.add_css_class("mic-level")
        self.level.update_property([Gtk.AccessibleProperty.LABEL], [_("Microphone level")])
        self.test_row = Adw.ActionRow(title=_("Test Microphone"),
                                      subtitle=_("Speak: the bar should move"))
        self.test_row.add_suffix(self.level)
        self.test_row.set_visible(hasattr(voice, "MicTest"))
        self.add(devices, self.test_row, ("test", "level", "meter", "check", "hear"))
        self.speaker_row = Adw.ActionRow(title=_("Test Speaker"),
                                         subtitle=_("Plays a short chime"))
        self.play_button = Gtk.Button(label=_("Play"), valign=Gtk.Align.CENTER)
        self.play_button.connect("clicked", lambda _b: self.play_test_sound())
        self.speaker_row.add_suffix(self.play_button)
        self.speaker_row.set_activatable_widget(self.play_button)
        self.speaker_row.set_visible(hasattr(voice, "SpeakerTest"))
        self.add(devices, self.speaker_row, ("test", "sound", "chime", "speakers", "hear"))

        processing = self.group(_("Sound Processing"))
        self.switch(processing, "echo-cancellation", _("Echo Cancellation"),
                    _("Recommended with speakers instead of headphones"),
                    ("echo", "aec", "feedback"))
        self.switch(processing, "noise-suppression", _("Noise Suppression"),
                    _("Softens fans, keyboards and street noise"), ("noise", "background"))
        self.switch(processing, "gain-control", _("Automatic Gain"),
                    _("Keeps your voice at a steady volume"), ("agc", "volume", "loudness"))

        mic = self.group(_("Microphone"))
        self.choice(mic, "mic-mode", _("Microphone Mode"),
                    [("open", _("Always On")), ("push-to-talk", _("Push to Talk"))],
                    _("Push to Talk: hold the microphone button or its global shortcut to "
                      "speak"),
                    ("push to talk", "ptt", "open mic", "hold"))
        self.switch(mic, "join-muted", _("Join Calls Muted"),
                    _("Unmute with Ctrl+Shift+M when you are ready to speak"),
                    ("mute", "silent"))
        self.permission = Adw.ComboRow(
            title=_("Microphone Access"),
            model=string_model([_("Ask"), _("Allow"), _("Don't Allow")]))
        self.permission.connect("notify::selected", self._on_permission)
        self.add(mic, self.permission, ("permission", "allow", "deny", "ask", "microphone"))

        self.voice_group = self.group(_("The Dots' Voice"))
        if hasattr(Gtk.Window.props, "suspended"):
            # Minimized or covered: the microphone meter stops until the window is back.
            self._suspend_handler = self.win.connect("notify::suspended",
                                                     lambda *_: self._on_suspended())
        self.refresh()
        self.model_changed()

    # -- devices ---------------------------------------------------------------------------

    def _device_row(self, group, kind: str, key: str, title: str, keywords) -> Adw.ComboRow:
        row = Adw.ComboRow(title=title, model=string_model([_("System Default")]))
        row.connect("notify::selected", lambda r, _p: self._on_device(r, kind, key))
        row.set_visible(hasattr(voice, "list_devices"))
        self.add(group, row, keywords)
        # Chosen elsewhere (or here): list again, later and off the main thread.
        self.win.bind(key, lambda _value: self._device_setting_changed(kind))
        return row

    def _load_devices(self, then: Callable[[], None] | None = None) -> None:
        """List microphones and speakers on a worker thread, then show them.

        Asking the sound server can take a moment (and the scan is shared behind a lock),
        so nothing here runs it on the main loop; ``then`` runs once the lists are in, when
        a fresh scan makes opening a device quick.
        """
        if self.win._closing:
            return
        if not hasattr(voice, "list_devices"):
            if then is not None:
                then()
            return
        if then is not None:
            self._after_devices.append(then)
        self._device_request += 1
        request = self._device_request

        def work() -> None:
            found: dict[str, tuple[list, str | None]] = {}
            for kind in ("source", "sink"):
                try:
                    devices = list(voice.list_devices(kind))
                except Exception:      # a broken audio stack must not break the page
                    log.exception("Could not list the %s devices", kind)
                    devices = []
                default = None
                if hasattr(voice, "default_device_name"):
                    try:
                        default = voice.default_device_name(kind)
                    except Exception:
                        log.debug("Could not tell the default %s", kind, exc_info=True)
                found[kind] = (devices, default)
            try:
                blocked = bool(voice.snap_microphone_blocked())
            except Exception:
                blocked = False
            GLib.idle_add(lambda: (self._devices_loaded(request, found, blocked), False)[1])

        threading.Thread(target=work, name="dotdeck-settings-audio", daemon=True).start()

    def _devices_loaded(self, request: int, found: dict, blocked: bool) -> None:
        if self.win._closing or request != self._device_request:
            return         # a newer listing is on its way and runs what waits
        self.snap_row.set_visible(blocked)
        for row, kind, key in ((self.mic_combo, "source", "microphone-device"),
                               (self.speaker_combo, "sink", "speaker-device")):
            devices, default = found.get(kind, ([], None))
            self._default_names[kind] = default
            self._fill_devices(row, kind, key, devices)
        waiting, self._after_devices = self._after_devices, []
        for then in waiting:
            then()

    def _fill_devices(self, row: Adw.ComboRow, kind: str, key: str, found: list) -> None:
        ids = [""] + [device.id for device in found]
        labels = [_("System Default")] + [device.name for device in found]
        saved = self.app.settings.get(key)
        if saved and saved not in ids:
            ids.append(saved)
            labels.append(_("{name} (not connected)").format(name=saved))
        self._devices[kind] = ids
        self._syncing = True
        try:
            row.set_model(string_model(labels))
            row.set_selected(ids.index(saved) if saved in ids else 0)
        finally:
            self._syncing = False
        self._describe_default(row, kind)

    def _describe_default(self, row: Adw.ComboRow, kind: str) -> None:
        """Under "System Default", which device that is now (from the last scan)."""
        name = self._default_names.get(kind) if row.get_selected() == 0 else None
        row.set_subtitle(escape(_("Now: {name}").format(name=name)) if name else "")

    def _on_device(self, row: Adw.ComboRow, kind: str, key: str) -> None:
        if self._syncing:
            return
        ids = self._devices.get(kind, [""])
        index = row.get_selected()
        if 0 <= index < len(ids):
            self._describe_default(row, kind)
            self.win.set_setting(key, ids[index])

    def _device_setting_changed(self, kind: str) -> None:
        restart = kind == "source" and self._mic_test is not None
        if restart:
            self._stop_test()
        self._load_devices(self._start_test if restart else None)

    # -- tests -----------------------------------------------------------------------------

    def _start_test(self) -> None:
        if self._mic_test is not None or not self._visible or self.win._closing or \
                not hasattr(voice, "MicTest") or self._suspended():
            return
        try:
            self._mic_test = voice.MicTest(self.app.settings.get("microphone-device"))
            self.test_row.set_subtitle(_("Speak: the bar should move"))
            start = self._mic_test.start
            if "on_error" in inspect.signature(start).parameters:
                start(self._on_level, on_error=self._on_mic_error)
            else:
                start(self._on_level)
        except Exception as error:
            log.warning("Could not test the microphone: %s", error)
            self._mic_test = None
            self.test_row.set_subtitle(_("The microphone could not be opened"))

    def _on_mic_error(self, message: str) -> None:
        self.test_row.set_subtitle(escape(message or _("The microphone could not be opened")))

    def _on_level(self, value: float) -> None:
        self.level.set_value(max(0.0, min(1.0, float(value))))

    def _stop_test(self) -> None:
        if self._mic_test is not None:
            try:
                self._mic_test.stop()
            except Exception:
                log.exception("Could not stop the microphone test")
            self._mic_test = None
        self.level.set_value(0)

    def _suspended(self) -> bool:
        return bool(self._suspend_handler and self.win.get_property("suspended"))

    def _on_suspended(self) -> None:
        if self._suspended():
            self._stop_test()
        elif self._visible:
            self._load_devices(self._start_test)

    def play_test_sound(self) -> None:
        """Play the chime on the chosen speaker (after a fresh scan, so it opens at once)."""
        if not hasattr(voice, "SpeakerTest"):
            return
        self.play_button.set_sensitive(False)

        def play() -> None:
            if self.win._closing:
                return
            if self._speaker_test is not None:
                self._speaker_test.stop()
            self._speaker_test = voice.SpeakerTest(self.app.settings.get("speaker-device"))
            self._speaker_test.play(played)

        def played(ok: bool) -> None:
            if self.win._closing:
                return
            self.play_button.set_sensitive(True)
            if not ok:
                self.win.toast(_("The test sound could not be played"))

        self._load_devices(play)

    def shown(self) -> None:
        self._visible = True
        self._load_devices(self._start_test)

    def hidden(self) -> None:
        self._visible = False
        self._stop_test()

    # -- server ----------------------------------------------------------------------------

    def refresh(self) -> None:
        profile = self.win.profile
        mon = self.win.monitor
        name = profile.name if profile else ""
        self.status.set_title(_("Voice Calls on {name}").format(name=escape(name)) if profile
                              else _("Status"))
        if profile is None:
            text, icon, css = _("Add a server to call its Dots."), "dialog-information-symbolic", ""
        elif mon is None or mon.status == "unknown":
            text, icon, css = _("Checking the server…"), "content-loading-symbolic", ""
        elif mon.status != "online":
            text, icon, css = (_("Unknown while the server cannot be reached"),
                               "dialog-warning-symbolic", "warning")
        elif mon.workspace.voice_ready or mon.voice_ready:
            text, icon, css = (_("Ready. One call at a time, up to 15 minutes."),
                               "object-select-symbolic", "success")
        else:
            text, icon, css = (_("Not configured. With an OpenRouter key, install the Dotdeck "
                                 "voice gateway on the server (server/README.md)."),
                               "dialog-information-symbolic", "")
        self.server_row.set_subtitle(escape(text))
        self.server_icon.set_from_icon_name(icon)
        for name_ in ("success", "warning"):
            self.server_icon.remove_css_class(name_)
        if css:
            self.server_icon.add_css_class(css)
        self._syncing = True
        try:
            self.permission.set_sensitive(profile is not None)
            self.permission.set_subtitle(_("For voice calls with the Dots on {name}").format(
                name=escape(name)) if profile else "")
            if profile is not None and profile.microphone in MIC_CHOICES:
                self.permission.set_selected(MIC_CHOICES.index(profile.microphone))
        finally:
            self._syncing = False

    def _on_permission(self, row, _pspec) -> None:
        if self._syncing:
            return
        profile = self.win.profile
        if profile is None:
            return
        current = self.app.profiles.get(profile.id)
        value = MIC_CHOICES[row.get_selected()]
        if current is not None and current.microphone != value:
            current.microphone = value
            self.app.profiles.update(current)

    # -- the Dots' voice (needs /api/model-config) -----------------------------------------

    def model_changed(self) -> None:
        config = self.win.model_config()
        state = config["state"]
        data = config.get("data") or {}
        if state == "loading" and data:
            state = "ok"          # asking again: keep showing the last answer meanwhile
        signature = (self.win.profile_id, state, data.get("voiceModel"), data.get("voiceName"),
                     bool(data.get("hasVoiceKey")), is_gateway(data))
        if signature == self._voice_signature:
            return
        self._voice_signature = signature
        for row in self._voice_rows:
            self.voice_group.remove(row)
        self._voice_rows = []
        self.voice_group.set_description(None)

        def add(row, keywords=()):
            self.add(self.voice_group, row, keywords)
            self._voice_rows.append(row)

        if state in ("unknown", "loading"):
            row = Adw.ActionRow(title=_("Voice"), subtitle=_("Checking the server…"))
            row.add_prefix(Adw.Spinner(valign=Gtk.Align.CENTER))
            add(row, ("voice", "timbre"))
            return
        if state == "unsupported":
            row = Adw.ActionRow(
                title=_("Voice"), subtitle=_("Set on the server (VOICE_NAME in its .env file). "
                                             "This server does not let Dotdeck change it."))
            row.set_subtitle_lines(3)
            add(row, ("voice", "timbre", "voice_name"))
            return
        if state == "error":
            row = Adw.ActionRow(title=_("Voice"), subtitle=escape(
                _("Could not read the server's voice settings: {error}").format(
                    error=config.get("error") or "")))
            row.add_suffix(self._retry_button())
            add(row, ("voice", "timbre"))
            return

        model = str(data.get("voiceModel") or "")
        gateway = is_gateway(data)
        model_row = Adw.ActionRow(title=_("Voice Model"), subtitle=escape(model or "—"),
                                  subtitle_selectable=True)
        if gateway:
            model_row.add_suffix(suffix_label(_("Through the Dotdeck voice gateway")))
        add(model_row, ("model", "realtime", "gateway"))

        current = str(data.get("voiceName") or "marin")
        names = list(VOICES) if current in VOICES else [current, *VOICES]
        timbre = Adw.ComboRow(title=_("Voice"),
                              subtitle=_("How every Dot sounds in calls on this server"),
                              model=string_model([n.capitalize() for n in names]))
        timbre.set_selected(names.index(current))
        timbre.connect("notify::selected", lambda row, _p: self._on_voice(row, names))
        add(timbre, ("timbre", "voice", "sound", *VOICES))

        if not gateway and model:
            # OpenAI Realtime called directly needs its own key; never offered for the
            # gateway, which uses the server's OpenRouter key.
            key = Adw.PasswordEntryRow(title=_("New OpenAI API Key"), show_apply_button=True)
            key.connect("apply", self._on_voice_key)
            add(key, ("key", "openai", "api key"))
            self.voice_group.set_description(
                _("A key is configured on the server. Type a new one to replace it.")
                if data.get("hasVoiceKey") else _("No key is configured on the server yet."))

    def _retry_button(self) -> Gtk.Button:
        button = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Try Again"))
        button.add_css_class("flat")
        button.connect("clicked", lambda _b: self.win.request_model_config(force=True))
        return button

    def _on_voice(self, row: Adw.ComboRow, names: list[str]) -> None:
        index = row.get_selected()
        if not 0 <= index < len(names):
            return
        value = names[index]
        data = self.win.model_config().get("data") or {}
        if value == data.get("voiceName"):
            return
        row.set_sensitive(False)

        def done(ok: bool, error: str | None) -> None:
            row.set_sensitive(True)
            if ok:
                self.win.toast(_("The Dots now speak with the {name} voice").format(
                    name=value.capitalize()))
            else:
                self.win.toast(_("Could not change the voice: {error}").format(error=error))
                self._voice_signature = None
                self.model_changed()

        self.win.change_model_config(done, voiceName=value)

    def _on_voice_key(self, row: Adw.PasswordEntryRow) -> None:
        key = row.get_text().strip()
        if not key:
            return
        row.set_sensitive(False)

        def done(ok: bool, error: str | None) -> None:
            row.set_sensitive(True)
            if ok:
                row.set_text("")
                self.win.toast(_("Voice key saved on the server"))
            else:
                self.win.toast(_("Could not save the key: {error}").format(error=error))

        self.win.change_model_config(done, voiceKey=key)

    def destroy(self) -> None:
        self._visible = False
        self._device_request += 1          # drop lists still on their way
        self._after_devices = []
        self._stop_test()
        if self._speaker_test is not None:
            self._speaker_test.stop()
            self._speaker_test = None
        if self._suspend_handler:
            self.win.disconnect(self._suspend_handler)
            self._suspend_handler = 0
