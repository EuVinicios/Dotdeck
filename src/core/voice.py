"""Native WebRTC for the OpenDots voice call.

WebKitGTK builds shipped by distributions and by the GNOME Flatpak runtime have no
``RTCPeerConnection``, so the OpenDots call (OpenAI Realtime over WebRTC) cannot start
in a plain web view. ``web/rtc-polyfill.js`` gives the page an ``RTCPeerConnection``
that forwards every call here, and this module runs the real peer connection with
GStreamer's ``webrtcbin``: the chosen microphone and speakers (PipeWire/PulseAudio),
Opus audio, a data channel for the provider's events, and optional voice processing
(echo cancellation, noise suppression and automatic gain).

Only what the voice call needs is implemented: one outgoing audio track, incoming
audio, data channels, offer/answer and ICE candidates. On top of that the bridge reads
the provider's events as they pass by and reports the call's phase (listening,
thinking, speaking) and live captions, so the window can show the call natively.

The module also lists the audio devices (``list_devices``), measures the microphone for
the settings (``MicTest``) and plays a test sound on a speaker (``SpeakerTest``).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
import weakref
from array import array
from functools import lru_cache
from itertools import count
from typing import Callable, NamedTuple

import gi

gi.require_version("Gst", "1.0")
try:
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, GObject, Gst, GstSdp, GstWebRTC  # noqa: E402
    _HAVE_WEBRTC = True
except (ValueError, ImportError):
    from gi.repository import GLib, GObject, Gst  # noqa: E402
    GstSdp = None
    GstWebRTC = None
    _HAVE_WEBRTC = False

from ..i18n import _  # noqa: E402

log = logging.getLogger(__name__)

REQUIRED_ELEMENTS = ("webrtcbin", "nicesrc", "dtlssrtpenc", "sctpenc", "opusenc", "opusdec",
                     "rtpopuspay", "rtpopusdepay", "audiomixer", "volume")
OPUS_CAPS = "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000"

# DOTDECK_TEST_AUDIO=1 replaces the microphone with a tone and the speakers with a
# fake sink, so tests and CI run without audio hardware.
_TEST_AUDIO = os.environ.get("DOTDECK_TEST_AUDIO") == "1"

# The data channel that carries the provider's (OpenAI Realtime style) events.
EVENTS_CHANNEL = "oai-events"

PHASES = ("connecting", "listening", "thinking", "speaking", "ended")
MIC_MODES = ("open", "push-to-talk")

# What a call uses when the host says nothing; keys match the app settings.
DEFAULT_OPTIONS: dict = {
    "microphone-device": "",     # "" = system default
    "speaker-device": "",
    "echo-cancellation": True,
    "noise-suppression": True,
    "gain-control": True,
    "mic-mode": "open",          # open | push-to-talk
    "join-muted": False,
}

# Meters map -60 dB (or quieter) to 0 and 0 dB to 1.
FLOOR_DB = -60.0
MIC_TEST_INTERVAL_NS = 50 * Gst.MSECOND
CALL_LEVEL_INTERVAL_NS = 100 * Gst.MSECOND
# A call that shows "thinking" without hearing anything back for this long goes back to
# "listening" (the provider dropped the turn), unless the Dot is running a compute task.
THINKING_TIMEOUT_S = 20

if _HAVE_WEBRTC:
    _STATES = {
        GstWebRTC.WebRTCPeerConnectionState.NEW: "new",
        GstWebRTC.WebRTCPeerConnectionState.CONNECTING: "connecting",
        GstWebRTC.WebRTCPeerConnectionState.CONNECTED: "connected",
        GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED: "disconnected",
        GstWebRTC.WebRTCPeerConnectionState.FAILED: "failed",
        GstWebRTC.WebRTCPeerConnectionState.CLOSED: "closed",
    }
else:
    _STATES = {}


def _ensure_gst() -> bool:
    if Gst.is_initialized():
        return True
    result = Gst.init_check(None)
    return bool(result[0] if isinstance(result, tuple) else result)


def _have(element: str) -> bool:
    return _ensure_gst() and Gst.ElementFactory.find(element) is not None


def missing_elements() -> list[str]:
    if not _HAVE_WEBRTC:
        return ["gir1.2-gst-plugins-bad-1.0"]
    if not Gst.is_initialized():
        Gst.init(None)
    return [name for name in REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]


def available() -> bool:
    return not missing_elements()


def snap_microphone_blocked() -> bool:
    """In a snap the microphone needs the audio-record interface, which users connect."""
    if not os.environ.get("SNAP_NAME"):
        return False
    import subprocess
    try:
        return subprocess.run(["snapctl", "is-connected", "audio-record"],
                              capture_output=True, timeout=5).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


class VoiceError(Exception):
    pass


def _main(func, *args) -> None:
    """Run ``func(*args)`` on the main loop (GStreamer calls back from its own threads)."""
    def run():
        func(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)


def _weak(method: Callable) -> Callable:
    """``method`` without keeping its object alive (for long-lived signal handlers)."""
    ref = weakref.WeakMethod(method)

    def call(*args):
        bound = ref()
        if bound is not None:
            bound(*args)
    return call


# -- options -------------------------------------------------------------------------------

def call_options(options: dict | None = None) -> dict:
    """``options`` over ``DEFAULT_OPTIONS``, with unknown keys dropped and every value typed."""
    result = dict(DEFAULT_OPTIONS)
    for key, value in (options or {}).items():
        default = DEFAULT_OPTIONS.get(key)
        if isinstance(default, bool):
            result[key] = bool(value)
        elif isinstance(default, str):
            result[key] = value.strip() if isinstance(value, str) else ""
    if result["mic-mode"] not in MIC_MODES:
        result["mic-mode"] = "open"
    return result


def legacy_options(echo_cancellation: bool) -> dict:
    """Options for a host that only knows echo cancellation (Dotdeck 0.2 and older).

    Back then voice processing was all or nothing: one switch for the whole ``webrtcdsp``.
    """
    echo = bool(echo_cancellation)
    return call_options({"echo-cancellation": echo, "noise-suppression": echo,
                         "gain-control": echo})


def starts_muted(options: dict) -> bool:
    """Whether a new call opens with the microphone off (push to talk, or join muted)."""
    return options.get("mic-mode") == "push-to-talk" or bool(options.get("join-muted"))


def _dsp_description(pc_id: str, echo: bool, noise: bool, gain: bool) -> str:
    """The ``webrtcdsp`` stage of the microphone chain, or "" when no processing is on."""
    if not (echo or noise or gain):
        return ""
    flags = [f"echo-cancel={str(echo).lower()}", f"noise-suppression={str(noise).lower()}",
             f"gain-control={str(gain).lower()}"]
    probe = [f"probe=echo-{pc_id}"] if echo else []
    return " ".join([f"webrtcdsp name=dsp-{pc_id}", *probe, *flags]) + " ! audioconvert ! "


# -- audio devices -------------------------------------------------------------------------

class AudioDevice(NamedTuple):
    """A microphone or speaker. ``id`` stays the same across runs (the PipeWire or
    PulseAudio node name) and is what the settings store; ``name`` is for people."""

    id: str
    name: str


class _Found(NamedTuple):
    device: AudioDevice
    gst: Gst.Device
    default: bool


class _Scan(dict):
    """The devices of each kind (``"source"``, ``"sink"``) from one look at the system."""

    # The system's default microphone is the monitor of a speaker: "System default"
    # would record what the speakers play, and the Dot would hear (and answer) itself.
    default_is_monitor = False


_KIND_CLASSES = {"source": "Audio/Source", "sink": "Audio/Sink"}
_SCAN_TTL_S = 2.0
_scan_lock = threading.Lock()
_scan_cache: tuple[float, _Scan] | None = None


def _prop(props: Gst.Structure | None, key: str):
    if props is None or not props.has_field(key):
        return None
    try:
        return props.get_value(key)
    except (TypeError, ValueError):  # a field type PyGObject cannot convert
        return None


def _internal_name(device) -> str:
    """PulseAudio's (and ALSA's) own name for the device, when the provider has one."""
    try:
        value = device.get_property("internal-name")
    except (TypeError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def _device_id(device, props: Gst.Structure | None) -> str:
    for value in (_prop(props, "node.name"), _internal_name(device),
                  _prop(props, "device.name"), _prop(props, "device.string")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (device.get_display_name() or "").strip()


def _is_monitor(device, props: Gst.Structure | None) -> bool:
    """A sink's monitor shows up as a source; it records what the speakers play."""
    if _prop(props, "device.class") == "monitor":
        return True
    media_class = _prop(props, "media.class")
    if isinstance(media_class, str) and media_class.endswith("Sink"):
        return True
    return _internal_name(device).endswith(".monitor")


def _display_name(device, props: Gst.Structure | None, fallback: str) -> str:
    for value in (device.get_display_name(), _prop(props, "device.description")):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return fallback


_SERVER_DEVICES = ("GstPulseDevice", "GstPipeWireDevice")


def _preferred(device) -> bool:
    """PulseAudio devices win over the same node seen by the PipeWire provider: they work
    in every sandbox and are what ``autoaudiosrc`` would pick."""
    return device.__gtype__.name == "GstPulseDevice"


def _from_sound_server(device) -> bool:
    return device.__gtype__.name in _SERVER_DEVICES


def _unique_names(found: list[_Found]) -> list[_Found]:
    seen: dict[str, int] = {}
    result = []
    for entry in found:
        key = entry.device.name.casefold()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            name = f"{entry.device.name} ({seen[key]})"
            entry = entry._replace(device=entry.device._replace(name=name))
        result.append(entry)
    return result


def _usable(devices) -> list:
    """With a sound server running, raw ALSA (and OSS) devices are left out: they repeat
    the server's devices under hardware names, and the server keeps them busy anyway."""
    devices = list(devices)
    if any(_from_sound_server(device) for device in devices):
        return [device for device in devices if _from_sound_server(device)]
    return devices


def _default_source_is_monitor(devices) -> bool:
    for device in _usable(devices):
        if device.has_classes(_KIND_CLASSES["source"]):
            props = device.get_properties()
            if _prop(props, "is-default") is True and _is_monitor(device, props):
                return True
    return False


def _collect(devices, kind: str) -> list[_Found]:
    """The devices of one kind: no sink monitors, one entry per id, sorted by name."""
    chosen: dict[str, _Found] = {}
    for device in _usable(devices):
        if not device.has_classes(_KIND_CLASSES[kind]):
            continue
        props = device.get_properties()
        if kind == "source" and _is_monitor(device, props):
            continue
        device_id = _device_id(device, props)
        if not device_id:
            continue
        entry = _Found(AudioDevice(device_id, _display_name(device, props, device_id)), device,
                       _prop(props, "is-default") is True)
        previous = chosen.get(device_id)
        if previous is None or (_preferred(device) and not _preferred(previous.gst)):
            chosen[device_id] = entry
    ordered = sorted(chosen.values(), key=lambda f: (f.device.name.casefold(), f.device.id))
    return _unique_names(ordered)


_shared_monitor: Gst.DeviceMonitor | bool | None = None
_monitor_lock = threading.Lock()


def _device_monitor() -> Gst.DeviceMonitor | None:
    """One device monitor for the whole process, started once and never stopped.

    GStreamer 1.28 (Ubuntu 26.04) aborts with a double free when a device monitor is
    stopped and a later one is stopped again (a provider that failed to start, such as
    PulseAudio without a server, is released twice). One long-lived monitor avoids
    that, and also keeps scans cheap. ``None`` when no monitor can run.
    """
    global _shared_monitor
    with _monitor_lock:
        if _shared_monitor is None:
            _shared_monitor = False
            try:
                monitor = Gst.DeviceMonitor.new()
                for device_class in _KIND_CLASSES.values():
                    monitor.add_filter(device_class, None)
                if monitor.start():
                    _shared_monitor = monitor
            except Exception:
                log.debug("Could not start the audio device monitor", exc_info=True)
        return _shared_monitor or None


def _probe_devices() -> _Scan:
    empty = _Scan({kind: [] for kind in _KIND_CLASSES})
    if not _ensure_gst():
        return empty
    try:
        monitor = _device_monitor()
        if monitor is None:
            return empty
        devices = monitor.get_devices() or []
        found = _Scan({kind: _collect(devices, kind) for kind in _KIND_CLASSES})
        found.default_is_monitor = _default_source_is_monitor(devices)
    except Exception:  # a broken provider must not break the settings or a call
        log.debug("Could not list the audio devices", exc_info=True)
        return empty
    return found


def _scan(max_age: float = _SCAN_TTL_S) -> _Scan:
    """Every device, from one scan shared by both kinds for a couple of seconds."""
    global _scan_cache
    with _scan_lock:
        if _scan_cache is not None and time.monotonic() - _scan_cache[0] < max_age:
            return _scan_cache[1]
        found = _probe_devices()
        _scan_cache = (time.monotonic(), found)
        return found


def _forget_scan() -> None:
    global _scan_cache
    with _scan_lock:
        _scan_cache = None


def _check_kind(kind: str) -> None:
    if kind not in _KIND_CLASSES:
        raise ValueError(f"Unknown audio device kind: {kind!r}")


def list_devices(kind: str) -> list[AudioDevice]:
    """The microphones (``"source"``) or speakers (``"sink"``), sorted by name.

    The system default is not listed: its id is ``""``. Returns ``[]`` when there is no
    sound server (headless CI) and never raises for a broken device provider.
    """
    _check_kind(kind)
    return [found.device for found in _scan()[kind]]


def list_devices_async(kind: str, callback: Callable[[list[AudioDevice]], None]) -> None:
    """``list_devices`` on a worker thread; ``callback(devices)`` runs on the main loop."""
    _check_kind(kind)

    def work():
        try:
            devices = list_devices(kind)
        except Exception:
            log.debug("Could not list the audio devices", exc_info=True)
            devices = []
        _main(callback, devices)
    threading.Thread(target=work, name="dotdeck-audio-devices", daemon=True).start()


def _stand_in_microphone(scan: dict) -> _Found | None:
    """The microphone that "System default" uses when the system's own default would
    record the speakers (a sink monitor); ``None`` when the sound server's choice is fine
    or there is no real microphone to use instead."""
    if getattr(scan, "default_is_monitor", False) and scan["source"]:
        return scan["source"][0]
    return None


def default_device_name(kind: str) -> str | None:
    """The name of the device that "System default" uses, when the sound server says.

    For microphones this is never a speaker's monitor: when the system's default
    microphone records the speakers, calls use the first real microphone instead.
    """
    _check_kind(kind)
    scan = _scan()
    stand_in = _stand_in_microphone(scan) if kind == "source" else None
    if stand_in is not None:
        return stand_in.device.name
    return next((found.device.name for found in scan[kind] if found.default), None)


def _find_device(kind: str, device_id: str) -> _Found | None:
    if not device_id:
        return None
    cache = _scan_cache
    cached = cache is not None and time.monotonic() - cache[0] < _SCAN_TTL_S
    found = next((f for f in _scan()[kind] if f.device.id == device_id), None)
    if found is None and cached:  # plugged in a moment ago: look again
        found = next((f for f in _scan(0.0)[kind] if f.device.id == device_id), None)
    return found


def _test_element(kind: str) -> Gst.Element:
    if kind == "source":
        return Gst.parse_bin_from_description(
            "audiotestsrc is-live=true wave=sine freq=440 volume=0.2", True)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", False)
    return sink


def audio_element(kind: str, device_id: str = "", name: str | None = None
                  ) -> tuple[Gst.Element, str]:
    """The element that records from (``"source"``) or plays to (``"sink"``) a device.

    Uses the device with ``device_id``; the system default (``autoaudiosrc`` or
    ``autoaudiosink``) when the id is ``""`` or the device is gone. A microphone never
    falls back to a speaker's monitor (see ``default_device_name``). Returns the element
    and the name of the device it opens (``""`` when the sound server picks).
    """
    _check_kind(kind)
    if _TEST_AUDIO:
        return _test_element(kind), ""
    found = _find_device(kind, device_id)
    if found is None and device_id:
        log.info("The audio device %s is not connected; using the system default", device_id)
    candidates = [found] if found is not None else []
    stand_in = _stand_in_microphone(_scan()) if kind == "source" else None
    if stand_in is not None and stand_in is not found:
        candidates.append(stand_in)
    for candidate in candidates:
        try:
            element = candidate.gst.create_element(name)
        except Exception:
            element = None
        if element is not None:
            if candidate is stand_in:
                log.info("The system's default microphone records the speakers; using %s",
                         candidate.device.id)
            return element, candidate.device.name
        log.warning("Could not open the audio device %s; using the system default",
                    candidate.device.id)
    factory = "autoaudiosrc" if kind == "source" else "autoaudiosink"
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise VoiceError(_("GStreamer's audio elements are missing."))
    return element, ""


class DeviceWatcher(GObject.Object):
    """Emits ``changed`` (debounced) when microphones or speakers come and go.

    Keep one alive only while device lists are on screen: it holds a sound-server
    connection. ``stop()`` is safe to call more than once; a watcher dropped without
    it stops by itself.
    """

    __gtype_name__ = "DotdeckDeviceWatcher"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self._monitor = None
        self._bus = None
        self._handler = 0
        self._timeout = 0
        if not _ensure_gst():
            return
        try:
            # The shared monitor (see _device_monitor): watching never starts or stops one.
            monitor = _device_monitor()
            if monitor is None:
                return
            bus = monitor.get_bus()
            bus.add_signal_watch()
            # Weakly, so a forgotten watcher can be collected (and stop, see __del__).
            self._handler = bus.connect("message", _weak(self._on_message))
            self._bus = bus
            self._monitor = monitor
        except Exception:
            log.debug("Could not watch the audio devices", exc_info=True)
            self.stop()

    def _on_message(self, _bus, message: Gst.Message) -> None:
        if message.type in (Gst.MessageType.DEVICE_ADDED, Gst.MessageType.DEVICE_REMOVED,
                            Gst.MessageType.DEVICE_CHANGED):
            _forget_scan()
            if not self._timeout:
                self._timeout = GLib.timeout_add(300, self._changed)

    def _changed(self) -> bool:
        self._timeout = 0
        self.emit("changed")
        return GLib.SOURCE_REMOVE

    def __del__(self):
        try:
            self.stop()
        except Exception:  # interpreter shutdown
            pass

    def stop(self) -> None:
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        if self._bus is not None:
            self._bus.disconnect(self._handler)
            self._bus.remove_signal_watch()
            self._bus = None
        self._monitor = None  # shared and long-lived: never stopped here


# -- levels --------------------------------------------------------------------------------

def level_from_db(db: float | None) -> float:
    """-60 dB or quieter is 0.0, 0 dB is 1.0, linear in decibels in between."""
    if db is None or math.isnan(db):
        return 0.0
    return min(1.0, max(0.0, (db - FLOOR_DB) / -FLOOR_DB))


_NUMBER_RE = re.compile(r"[-+]?(?:inf|nan|\d+(?:\.\d*)?(?:e[-+]?\d+)?)", re.IGNORECASE)


def _doubles(structure: Gst.Structure, field: str) -> list[float]:
    try:
        return [float(v) for v in structure.get_value(field)]
    except (TypeError, ValueError):
        pass
    # PyGObject cannot convert every array type (GstValueArray): read the text form.
    match = re.search(rf"(?:^|[ ,]){re.escape(field)}=\([^)]*\)<([^>]*)>", structure.to_string())
    if match is None:
        return []
    return [float(n) for n in _NUMBER_RE.findall(re.sub(r"\([^)]*\)", "", match.group(1)))]


def level_from_structure(structure: Gst.Structure) -> float:
    """The loudest channel of a ``level`` message, as 0..1.

    Uses the peak with its slow fall (``decay``), which reads well on a meter; plain
    ``peak`` and ``rms`` are fallbacks.
    """
    for field in ("decay", "peak", "rms"):
        if structure.has_field(field):
            values = _doubles(structure, field)
            if values:
                return level_from_db(max(values))
    return 0.0


class MicTest:
    """A live microphone meter for the settings.

    ``start(on_level)`` opens the microphone and calls ``on_level(0..1)`` about 20 times
    a second on the main loop until ``stop()``, which releases the device. Start and stop
    as often as needed. It never raises: without a usable microphone the level reads 0
    once, ``error`` says why and ``on_error(message)`` is called when given.
    """

    def __init__(self, device_id: str = "", source: str | None = None):
        self.device_id = device_id or ""
        # A GStreamer description replacing the microphone (tests, demos).
        self._source = source
        self.pipeline: Gst.Pipeline | None = None
        self.error: str | None = None
        self.device_name = ""
        self._bus_handler = 0
        self._generation = 0

    @property
    def running(self) -> bool:
        return self.pipeline is not None

    def start(self, on_level: Callable[[float], None],
              on_error: Callable[[str], None] | None = None) -> None:
        self.stop()
        generation = self._generation
        self.error = None
        try:
            pipeline = self._build()
        except Exception as error:  # no device, no plugin, a bad description
            log.debug("Microphone test could not start", exc_info=True)
            message = error.message if isinstance(error, GLib.Error) else str(error)
            self._report_failure(generation, message or _("The microphone could not be opened."),
                                 on_level, on_error)
            return
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        # Weakly: a meter dropped without stop() still releases the microphone (__del__).
        self._bus_handler = bus.connect("message", _weak(self._on_message), generation,
                                        on_level, on_error)
        self.pipeline = pipeline
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown()
            self._report_failure(generation, _("The microphone could not be opened."),
                                 on_level, on_error)

    def stop(self) -> None:
        self._generation += 1  # drops callbacks that are already queued
        self._teardown()

    def __del__(self):
        try:
            self._teardown()
        except Exception:  # interpreter shutdown
            pass

    def _build(self) -> Gst.Pipeline:
        if not _ensure_gst():
            raise VoiceError(_("GStreamer is not available."))
        if not self._source and not _TEST_AUDIO and snap_microphone_blocked():
            raise VoiceError(_("The microphone is not connected to the Dotdeck snap. Run: "
                               "snap connect dotdeck:audio-record"))
        if Gst.ElementFactory.find("level") is None:
            raise VoiceError(_("GStreamer's level meter is missing."))
        pipeline = Gst.Pipeline.new("dotdeck-mic-test")
        if self._source:
            source = Gst.parse_bin_from_description(self._source, True)
            self.device_name = ""
        else:
            source, self.device_name = audio_element("source", self.device_id, "microphone")
        meter = Gst.parse_bin_from_description(
            f"audioconvert ! level name=level interval={MIC_TEST_INTERVAL_NS} "
            "post-messages=true ! fakesink sync=false async=false", True)
        pipeline.add(source)
        pipeline.add(meter)
        if not source.link(meter):
            raise VoiceError(_("The microphone could not be opened."))
        return pipeline

    def _on_message(self, _bus, message: Gst.Message, generation: int,
                    on_level: Callable[[float], None],
                    on_error: Callable[[str], None] | None) -> None:
        if generation != self._generation:
            return
        if message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "level":
                on_level(level_from_structure(structure))
        elif message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            log.info("Microphone test stopped: %s", error.message)
            self._teardown()
            self.error = error.message or _("The microphone stopped working.")
            on_level(0.0)
            if on_error is not None:
                on_error(self.error)

    def _report_failure(self, generation: int, message: str, on_level: Callable[[float], None],
                        on_error: Callable[[str], None] | None) -> None:
        self.error = message

        def report():
            if generation == self._generation:
                on_level(0.0)
                if on_error is not None:
                    on_error(message)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(report)

    def _teardown(self) -> None:
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        if self._bus_handler:
            bus.disconnect(self._bus_handler)
            self._bus_handler = 0
        bus.remove_signal_watch()
        pipeline.set_state(Gst.State.NULL)


@lru_cache(maxsize=1)
def _chime(rate: int = 48000) -> bytes:
    """A short, soft two-note chime (mono, 16-bit, native byte order)."""
    notes = ((659.25, 0.0), (987.77, 0.12))  # E5 then B5
    length = int(rate * 0.9)
    mix = [0.0] * length
    for frequency, start in notes:
        first = int(start * rate)
        step = 2 * math.pi * frequency / rate
        for i in range(first, length):
            t = (i - first) / rate
            envelope = min(1.0, t * 200.0) * math.exp(-t * 5.5)
            mix[i] += 0.22 * envelope * math.sin(step * (i - first))
    return array("h", (max(-32767, min(32767, int(v * 32767))) for v in mix)).tobytes()


class SpeakerTest:
    """Plays a short chime on a speaker, for the settings' "Test" button.

    ``play(done)`` calls ``done(ok)`` once on the main loop when the sound has played or
    could not play; ``stop()`` cuts it short (``done`` is not called then).
    """

    def __init__(self, device_id: str = ""):
        self.device_id = device_id or ""
        self.pipeline: Gst.Pipeline | None = None
        self.device_name = ""
        self._bus_handler = 0
        self._generation = 0

    @property
    def playing(self) -> bool:
        return self.pipeline is not None

    def play(self, done: Callable[[bool], None] | None = None) -> None:
        self.stop()
        generation = self._generation
        finish = done or (lambda ok: None)
        try:
            pipeline, appsrc = self._build()
        except Exception:
            log.info("Could not play the test sound", exc_info=True)
            _main(lambda: generation == self._generation and finish(False))
            return
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message, generation, finish)
        self.pipeline = pipeline
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown()
            _main(lambda: generation == self._generation and finish(False))
            return
        pcm = _chime()
        buffer = Gst.Buffer.new_wrapped(pcm)
        buffer.pts = 0
        buffer.duration = Gst.util_uint64_scale(len(pcm) // 2, Gst.SECOND, 48000)
        appsrc.emit("push-buffer", buffer)
        appsrc.emit("end-of-stream")

    def stop(self) -> None:
        self._generation += 1
        self._teardown()

    def _build(self) -> tuple[Gst.Pipeline, Gst.Element]:
        if not _ensure_gst():
            raise VoiceError(_("GStreamer is not available."))
        order = "S16LE" if sys.byteorder == "little" else "S16BE"
        pipeline = Gst.Pipeline.new("dotdeck-speaker-test")
        chain = Gst.parse_bin_from_description(
            f"appsrc name=chime format=time caps=audio/x-raw,format={order},rate=48000,"
            "channels=1,layout=interleaved ! audioconvert ! audioresample", True)
        sink, self.device_name = audio_element("sink", self.device_id, "speaker")
        pipeline.add(chain)
        pipeline.add(sink)
        if not chain.link(sink):
            raise VoiceError(_("The speakers could not be opened."))
        return pipeline, chain.get_by_name("chime")

    def _on_message(self, _bus, message: Gst.Message, generation: int,
                    finish: Callable[[bool], None]) -> None:
        if generation != self._generation:
            return
        if message.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
            ok = message.type == Gst.MessageType.EOS
            if not ok:
                log.info("Test sound failed: %s", message.parse_error()[0].message)
            self._generation += 1
            self._teardown()
            finish(ok)

    def _teardown(self) -> None:
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        if self._bus_handler:
            bus.disconnect(self._bus_handler)
            self._bus_handler = 0
        bus.remove_signal_watch()
        pipeline.set_state(Gst.State.NULL)


# -- call phases and captions --------------------------------------------------------------

class CallEvent(NamedTuple):
    """What one provider event means for the call (see ``parse_call_event``)."""

    type: str
    phase: str | None = None   # the phase the event moves the call to
    role: str | None = None    # "you" or "dot" when the event carries caption text
    text: str = ""             # caption text (a piece of it unless ``final``), or the error
    final: bool = False        # the caption is complete
    compute: bool = False      # the Dot asked OpenDots to work on something (ask_compute)
    error: bool = False


_DOT_DELTAS = ("response.output_audio_transcript.delta", "response.audio_transcript.delta")
_DOT_DONE = ("response.output_audio_transcript.done", "response.audio_transcript.done")
_YOU_DELTA = "conversation.item.input_audio_transcription.delta"
_YOU_DONE = "conversation.item.input_audio_transcription.completed"
_AUDIO_STOPPED = ("output_audio_buffer.stopped", "output_audio_buffer.cleared")
_INCOMING = frozenset({
    "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
    "response.created", "response.done", "response.function_call_arguments.done",
    "output_audio_buffer.started", *_AUDIO_STOPPED, *_DOT_DELTAS, *_DOT_DONE,
    _YOU_DELTA, _YOU_DONE, "error"})
_OUTGOING = frozenset({"response.create", "response.cancel"})
_TYPE_RE = re.compile(r'"type"\s*:\s*"([A-Za-z0-9_.]+)"')
_MAX_EVENT_CHARS = 256 * 1024
_MAX_ERROR_CHARS = 300
# OpenAI Realtime reports these as errors, but the call goes on unharmed (a cancel or a
# reply request that came a moment too late, an empty audio commit).
_HARMLESS_ERRORS = frozenset({"response_cancel_not_active",
                              "conversation_already_has_active_response",
                              "input_audio_buffer_commit_empty"})


def _error_message(value) -> str:
    if isinstance(value, dict):
        value = value.get("message") or value.get("code") or ""
    return " ".join(value.split())[:_MAX_ERROR_CHARS] if isinstance(value, str) else ""


def parse_call_event(data: str, outgoing: bool = False) -> CallEvent | None:
    """Reads one message of the events channel; ``None`` when it does not matter here.

    ``outgoing`` is for what the page sends to the provider. Pure and cheap: a message
    is only decoded when it names one of the few event types the call view needs.
    """
    wanted = _OUTGOING if outgoing else _INCOMING
    if not isinstance(data, str) or len(data) > _MAX_EVENT_CHARS:
        return None
    if not any(match.group(1) in wanted for match in _TYPE_RE.finditer(data)):
        return None
    try:
        event = json.loads(data)
    except ValueError:
        return None
    kind = event.get("type") if isinstance(event, dict) else None
    if kind not in wanted:
        return None
    if outgoing:
        return CallEvent(kind, phase="thinking" if kind == "response.create" else "listening")
    if kind == "input_audio_buffer.speech_started":
        return CallEvent(kind, phase="listening")
    if kind in ("input_audio_buffer.speech_stopped", "response.created"):
        return CallEvent(kind, phase="thinking")
    if kind == "output_audio_buffer.started":
        return CallEvent(kind, phase="speaking")
    if kind in _AUDIO_STOPPED:
        return CallEvent(kind, phase="listening")
    if kind in _DOT_DELTAS or kind == _YOU_DELTA:
        delta = event.get("delta")
        role = "you" if kind == _YOU_DELTA else "dot"
        return CallEvent(kind, phase="speaking" if role == "dot" else None, role=role,
                         text=delta if isinstance(delta, str) else "")
    if kind in _DOT_DONE or kind == _YOU_DONE:
        transcript = event.get("transcript")
        return CallEvent(kind, role="you" if kind == _YOU_DONE else "dot",
                         text=transcript.strip() if isinstance(transcript, str) else "",
                         final=True)
    if kind == "response.function_call_arguments.done":
        if event.get("name") != "ask_compute":
            return None
        return CallEvent(kind, phase="thinking", compute=True)
    if kind == "response.done":
        response = event.get("response")
        if isinstance(response, dict) and response.get("status") == "failed":
            details = response.get("status_details")
            message = _error_message(details.get("error")) if isinstance(details, dict) else ""
            return CallEvent(kind, text=message, error=True)
        return CallEvent(kind)
    # "error"
    details = event.get("error")
    if isinstance(details, dict) and details.get("code") in _HARMLESS_ERRORS:
        return None
    return CallEvent(kind, text=_error_message(details or event.get("message")), error=True)


class CallTracker:
    """Turns one call's events and connection states into phases and captions.

    Plain Python: every method returns the signals to emit, as tuples
    ``("call-phase", phase)``, ``("caption", role, text, final)`` and
    ``("call-error", message)``. Captions are whole lines: pieces are joined here.
    """

    def __init__(self):
        self.phase = ""
        self.ended = False
        self.computing = False
        self.seen = 0  # events read so far, for the "thinking" watchdog
        self._captions = {"you": "", "dot": ""}
        # Providers that never say when the reply audio starts and stops end
        # "speaking" with the response itself.
        self._audio_events = False

    def _phase(self, phase: str) -> list[tuple]:
        if phase == self.phase or self.ended:
            return []
        self.phase = phase
        return [("call-phase", phase)]

    def start(self) -> list[tuple]:
        return self._phase("connecting") if not self.phase else []

    def end(self) -> list[tuple]:
        if self.ended:
            return []
        updates = self._phase("ended")
        self.ended = True
        return updates

    def stale(self) -> list[tuple]:
        """Called when "thinking" went on too long without any news."""
        if self.phase == "thinking" and not self.computing:
            return self._phase("listening")
        return []

    def connection(self, state: str, hanging_up: bool = False) -> list[tuple]:
        """A connection state of the call; ``hanging_up`` when the page is ending it."""
        if self.ended:
            return []
        if state in ("new", "connecting"):
            return self._phase("connecting") if self.phase in ("", "connecting") else []
        if state == "connected":
            return self._phase("listening") if self.phase in ("", "connecting") else []
        if state == "disconnected":
            return [] if hanging_up else self._phase("connecting")
        if state == "failed":
            # A failed connection never comes back, and the page hangs up. When the page
            # was hanging up already this is no error: the server hangs up the provider
            # first, and only then (up to ~40 s later) does the page close its side.
            error = [] if hanging_up else [("call-error", _("The voice connection dropped."))]
            return error + self.end()
        if state == "closed":
            return self.end()
        return []

    def error(self, message: str) -> list[tuple]:
        return [] if self.ended else [("call-error", message)]

    def feed(self, data: str, outgoing: bool = False) -> list[tuple]:
        if self.ended:
            return []
        self.seen += 1
        event = parse_call_event(data, outgoing)
        if event is None:
            return []
        if event.type in ("response.created", "response.create", "response.cancel"):
            self.computing = False
        if event.type == "response.created":
            self._captions["dot"] = ""
        elif event.type == "input_audio_buffer.speech_started":
            self._captions["you"] = ""
        if event.compute:
            self.computing = True
        if event.type == "output_audio_buffer.started" or event.type in _AUDIO_STOPPED:
            self._audio_events = True

        phase = event.phase
        if event.compute and self.phase == "speaking" and self._audio_events:
            phase = None  # let the Dot finish saying "let me check"; its audio stop says when
        if event.type in _AUDIO_STOPPED and self.computing:
            phase = "thinking"  # the Dot said "let me check" and is still working
        if (event.type == "response.done" or event.error) and self.phase == "thinking" \
                and not self.computing:
            phase = "listening"  # a turn that ended without saying anything
        if event.type == "response.done" and self.phase == "speaking" \
                and not self._audio_events:
            phase = "thinking" if self.computing else "listening"
        updates = self._phase(phase) if phase else []

        if event.role is not None:
            if event.final:
                text = event.text or self._captions[event.role].strip()
                self._captions[event.role] = ""
                if text:
                    updates.append(("caption", event.role, text, True))
            elif event.text:
                self._captions[event.role] += event.text
                updates.append(("caption", event.role, self._captions[event.role].strip(),
                                False))
        if event.error:
            updates.append(("call-error",
                            event.text or _("The voice provider reported an error.")))
        return updates


# -- the peer connection -------------------------------------------------------------------

_LEVEL_ROLES = {"you-level": "you", "dot-level": "dot"}


class PeerConnection:
    """One webrtcbin pipeline. All public methods run on the main thread.

    ``options`` are call options (see ``DEFAULT_OPTIONS``); without them only
    ``echo_cancellation`` is used, the way Dotdeck 0.2 did. ``on_level(role, level)``
    receives the microphone ("you") and remote ("dot") levels about 10 times a second.
    """

    def __init__(self, pc_id: str, emit: Callable[[dict], None], echo_cancellation: bool = True,
                 options: dict | None = None,
                 on_level: Callable[[str, float], None] | None = None):
        self.id = pc_id
        self._emit = emit
        self._on_level = on_level
        self._closed = False
        self._channels: dict[str, GstWebRTC.WebRTCDataChannel] = {}
        self._labels: dict[str, str] = {}
        self._channel_ids = count(1)
        self._mic_volume: Gst.Element | None = None
        self._mic_source: Gst.Element | None = None
        self._has_mic = False
        self.options = call_options(options) if options is not None \
            else legacy_options(echo_cancellation)
        self.mic_enabled = not starts_muted(self.options)
        self.speaker_muted = False
        # What the page itself did: switched its microphone track off, paused the
        # call's audio. OpenDots does both at once to hang up.
        self.page_muted = False
        self.page_paused = False
        self.microphone_name = ""   # the device in use; "" = system default
        self.speaker_name = ""
        have_dsp = _have("webrtcdsp")
        self._echo = self.options["echo-cancellation"] and have_dsp \
            and _have("webrtcechoprobe") and not _TEST_AUDIO
        self._dsp = _dsp_description(pc_id, self._echo,
                                     self.options["noise-suppression"] and have_dsp,
                                     self.options["gain-control"] and have_dsp)
        self._meters = on_level is not None and _have("level")

        self.pipeline = Gst.Pipeline.new(f"dotdeck-call-{pc_id}")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.pipeline.add(self.webrtc)

        # Playback: a silent live source keeps the mixer (and the echo probe) running
        # before the remote audio arrives; remote tracks are mixed in as they come.
        probe = f"webrtcechoprobe name=echo-{pc_id} ! " if self._echo else ""
        self._playback = Gst.parse_bin_from_description(
            "audiotestsrc wave=silence is-live=true ! audio/x-raw,rate=48000,channels=2 ! "
            "audiomixer name=mix ! audioconvert ! audioresample ! "
            f"{probe}{self._meter('dot-level')}volume name=speaker", False)
        self.pipeline.add(self._playback)
        self._mixer = self._playback.get_by_name("mix")
        self._speaker = self._playback.get_by_name("speaker")
        self._speaker_sink, self.speaker_name = audio_element(
            "sink", self.options["speaker-device"], "speaker-out")
        self._playback.add(self._speaker_sink)
        if not self._speaker.link(self._speaker_sink):
            raise VoiceError(_("Could not connect the speakers."))

        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("on-data-channel", self._on_remote_data_channel)
        self.webrtc.connect("notify::connection-state", self._on_connection_state)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise VoiceError(_("Could not start the audio pipeline."))

    def _meter(self, name: str) -> str:
        if not self._meters:
            return ""
        return f"level name={name} interval={CALL_LEVEL_INTERVAL_NS} post-messages=true ! "

    # -- media -----------------------------------------------------------------------------

    def add_microphone(self) -> None:
        if self._has_mic:
            return
        # The meter sits before the mute switch, so the window can tell people they are
        # talking while muted.
        mic = Gst.parse_bin_from_description(
            "audioconvert name=mic-in ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
            f"{self._dsp}{self._meter('you-level')}volume name=mic ! audioconvert ! queue ! "
            f"opusenc ! rtpopuspay pt=111 ! queue ! capsfilter name=mic-out caps=\"{OPUS_CAPS}\"",
            False)
        source, self.microphone_name = audio_element("source", self.options["microphone-device"],
                                                     "microphone")
        mic.add(source)
        if not source.link(mic.get_by_name("mic-in")):
            raise VoiceError(_("Could not connect the microphone."))
        mic.add_pad(Gst.GhostPad.new("src", mic.get_by_name("mic-out").get_static_pad("src")))
        self.pipeline.add(mic)
        if not mic.link(self.webrtc):
            raise VoiceError(_("Could not connect the microphone."))
        self._mic_source = source
        self._mic_volume = mic.get_by_name("mic")
        self._mic_volume.set_property("mute", not self.mic_enabled)
        transceiver = self.webrtc.emit("get-transceiver", 0)
        if transceiver is not None:
            transceiver.set_property("direction",
                                     GstWebRTC.WebRTCRTPTransceiverDirection.SENDRECV)
        mic.sync_state_with_parent()
        self._has_mic = True

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = enabled
        if self._mic_volume is not None:
            self._mic_volume.set_property("mute", not enabled)

    def set_speaker_muted(self, muted: bool) -> None:
        self.speaker_muted = muted
        self._speaker.set_property("mute", muted)

    @property
    def hanging_up(self) -> bool:
        """The page silenced the call the way OpenDots does before it hangs up (it then
        waits for the server to save the call, up to ~40 s, before closing it)."""
        return self.page_muted and (self.page_paused or self.speaker_muted)

    def _on_pad_added(self, _webrtc, pad: Gst.Pad) -> None:
        # Runs on a streaming thread and must link before the first packet is pushed,
        # or the stream stops with "not-linked".
        if pad.get_direction() != Gst.PadDirection.SRC or self._closed:
            return
        decoder = Gst.parse_bin_from_description(
            "queue ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=48000,channels=2 ! queue", True)
        self.pipeline.add(decoder)
        decoder.link(self._mixer)
        if pad.link(decoder.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            log.warning("Could not link the remote audio of call %s", self.id)
            return
        decoder.sync_state_with_parent()
        _main(self._emit, {"type": "track", "kind": "audio"})

    # -- data channels ---------------------------------------------------------------------

    def create_data_channel(self, label: str, ordered: bool = True) -> str:
        options = Gst.Structure.new_empty("options")
        options.set_value("ordered", ordered)
        channel = self.webrtc.emit("create-data-channel", label, options)
        if channel is None:
            raise VoiceError(_("Could not create the data channel."))
        return self._track_channel(channel, label)

    def _on_remote_data_channel(self, _webrtc, channel) -> None:
        def adopt():
            dc_id = self._track_channel(channel, channel.props.label or "")
            self._emit({"type": "datachannel", "dc": dc_id, "label": channel.props.label})
        _main(adopt)

    def _track_channel(self, channel, label: str) -> str:
        dc_id = str(next(self._channel_ids))
        self._channels[dc_id] = channel
        self._labels[dc_id] = label
        channel.connect("on-open", lambda _c: _main(self._emit, {"type": "dcopen", "dc": dc_id}))
        channel.connect("on-close", lambda _c: _main(self._emit, {"type": "dcclose", "dc": dc_id}))
        channel.connect("on-error", lambda _c, error: _main(
            self._emit, {"type": "dcerror", "dc": dc_id, "message": str(error)}))
        channel.connect("on-message-string", lambda _c, text: _main(
            self._emit, {"type": "dcmessage", "dc": dc_id, "data": text}))
        channel.connect("on-message-data", lambda _c, data: _main(
            self._emit, {"type": "dcmessage", "dc": dc_id,
                         "data": bytes(data.get_data() or b"").decode("utf-8", "replace")}))
        if channel.props.ready_state == GstWebRTC.WebRTCDataChannelState.OPEN:
            _main(self._emit, {"type": "dcopen", "dc": dc_id})
        return dc_id

    def label(self, dc_id) -> str:
        """The label of a data channel ("" when unknown)."""
        return self._labels.get(str(dc_id), "")

    def send(self, dc_id: str, text: str) -> None:
        channel = self._channels.get(dc_id)
        if channel is None:
            raise VoiceError(_("Unknown data channel."))
        channel.send_string(text)

    def close_channel(self, dc_id: str) -> None:
        channel = self._channels.pop(dc_id, None)
        if channel is not None:
            channel.close()

    # -- negotiation -----------------------------------------------------------------------

    def create_offer(self, done: Callable[[str | None, str | None], None]) -> None:
        def on_reply(promise):
            reply = promise.get_reply()
            offer = reply.get_value("offer") if reply and reply.has_field("offer") else None
            if offer is None:
                _main(done, None, _error_text(reply, "Could not create the offer."))
            else:
                _main(done, offer.sdp.as_text(), None)
        self.webrtc.emit("create-offer", None, Gst.Promise.new_with_change_func(on_reply))

    def create_answer(self, done: Callable[[str | None, str | None], None]) -> None:
        def on_reply(promise):
            reply = promise.get_reply()
            answer = reply.get_value("answer") if reply and reply.has_field("answer") else None
            if answer is None:
                _main(done, None, _error_text(reply, "Could not create the answer."))
            else:
                _main(done, answer.sdp.as_text(), None)
        self.webrtc.emit("create-answer", None, Gst.Promise.new_with_change_func(on_reply))

    def set_description(self, local: bool, sdp_type: str, sdp: str,
                        done: Callable[[str | None], None]) -> None:
        kinds = {"offer": GstWebRTC.WebRTCSDPType.OFFER, "answer": GstWebRTC.WebRTCSDPType.ANSWER,
                 "pranswer": GstWebRTC.WebRTCSDPType.PRANSWER,
                 "rollback": GstWebRTC.WebRTCSDPType.ROLLBACK}
        if sdp_type not in kinds:
            done(f"Unsupported description type: {sdp_type}")
            return
        result, message = GstSdp.SDPMessage.new_from_text(sdp)
        if result != GstSdp.SDPResult.OK:
            done("The session description is not valid SDP.")
            return
        description = GstWebRTC.WebRTCSessionDescription.new(kinds[sdp_type], message)

        def on_reply(promise):
            reply = promise.get_reply()
            _main(done, _error_text(reply, None))
        signal = "set-local-description" if local else "set-remote-description"
        self.webrtc.emit(signal, description, Gst.Promise.new_with_change_func(on_reply))

    def add_ice_candidate(self, mline: int, candidate: str) -> None:
        if candidate:
            self.webrtc.emit("add-ice-candidate", mline, candidate)

    def _on_ice_candidate(self, _webrtc, mline: int, candidate: str) -> None:
        _main(self._emit, {"type": "icecandidate", "mline": mline, "candidate": candidate})

    def _on_connection_state(self, webrtc, _pspec) -> None:
        state = _STATES.get(webrtc.props.connection_state, "new")
        _main(self._emit, {"type": "connectionstatechange", "state": state})

    def _is_device(self, source: Gst.Object | None) -> bool:
        if source is None:
            return False
        for element in (self._mic_source, self._speaker_sink):
            if element is not None and (source is element or source.has_as_ancestor(element)):
                return True
        return source.get_name().startswith(("autoaudio", "pulse", "pipewire"))

    def _on_bus_message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            role = _LEVEL_ROLES.get(message.src.get_name()) if message.src is not None else None
            if role and self._on_level is not None and structure is not None \
                    and structure.get_name() == "level" and not self._closed:
                self._on_level(role, level_from_structure(structure))
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.warning("Call %s: %s (%s)", self.id, error.message, debug)
            if self._is_device(message.src):
                self._emit({"type": "mediaerror", "message": error.message})

    # -- teardown --------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for channel in list(self._channels.values()):
            try:
                channel.close()
            except Exception:  # already closed by the peer
                pass
        self._channels.clear()
        bus = self.pipeline.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)


def _error_text(reply: Gst.Structure | None, default: str | None) -> str | None:
    if reply is not None and reply.has_field("error"):
        error = reply.get_value("error")
        return getattr(error, "message", None) or str(error)
    return default


class VoiceBridge(GObject.Object):
    """Dispatches the polyfill's operations to ``PeerConnection`` objects.

    ``send_event(pc_id, event)`` delivers events back to the page. ``request_microphone``
    asks the user for permission (``callback(allowed)``). Call options come from
    ``voice_options()`` (a dict, see ``DEFAULT_OPTIONS``); without it, from a host's
    ``voice_options`` method when ``echo_cancellation`` is bound to such a host, else
    from ``echo_cancellation()`` alone.

    Signals, all on the main thread: ``calls-changed``; ``call-phase(phase)`` with
    connecting, listening, thinking, speaking or ended; ``caption(role, text, final)``
    with role "you" or "dot" and the whole line so far; ``call-error(message)``; and
    ``audio-level(role, level)`` (0..1, ~10/s while it changes).

    "ended" comes once per call, as soon as the conversation is over: when the page
    hangs up or the connection fails. ``in_call`` follows the page's connection
    instead, which OpenDots keeps open while the server saves the call (up to ~40 s).
    """

    __gtype_name__ = "DotdeckVoiceBridge"
    __gsignals__ = {
        "calls-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "call-phase": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "caption": (GObject.SignalFlags.RUN_FIRST, None, (str, str, bool)),
        "call-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "audio-level": (GObject.SignalFlags.RUN_FIRST, None, (str, float)),
    }

    def __init__(self, send_event: Callable[[str, dict], None],
                 request_microphone: Callable[[Callable[[bool], None]], None],
                 echo_cancellation: Callable[[], bool] = lambda: True,
                 voice_options: Callable[[], dict] | None = None):
        super().__init__()
        self._send_event = send_event
        self._request_microphone = request_microphone
        self._echo = echo_cancellation
        self._voice_options = voice_options
        self._peers: dict[str, PeerConnection] = {}
        self._trackers: dict[str, CallTracker] = {}
        self._watchdogs: dict[str, int] = {}
        self._levels: dict[str, float] = {}
        self._ids = count(1)

    @property
    def in_call(self) -> bool:
        return any(p._has_mic for p in self._peers.values())

    @property
    def has_calls(self) -> bool:
        """A call is open or still being set up or wound down (with or without a microphone)."""
        return bool(self._peers or self._trackers)

    @property
    def mic_enabled(self) -> bool:
        return all(p.mic_enabled for p in self._peers.values() if p._has_mic)

    @property
    def push_to_talk(self) -> bool:
        """The current call is in push-to-talk mode: hold a key to talk."""
        return any(p._has_mic and p.options["mic-mode"] == "push-to-talk"
                   for p in self._peers.values())

    @property
    def phase(self) -> str:
        """The current call's phase, or "" without a call."""
        return next((t.phase for t in reversed(self._trackers.values()) if not t.ended), "")

    def current_options(self) -> dict:
        """The options a call started now would use."""
        source = self._voice_options
        if source is None:
            source = getattr(getattr(self._echo, "__self__", None), "voice_options", None)
        if callable(source):
            try:
                given = source()
            except Exception:
                log.exception("Could not read the voice options")
                given = None
            if isinstance(given, dict):
                return call_options(given)
        try:
            echo = bool(self._echo())
        except Exception:
            log.exception("Could not read the echo cancellation setting")
            echo = True
        return legacy_options(echo)

    def set_mic_enabled(self, enabled: bool) -> None:
        for peer in self._peers.values():
            peer.set_mic_enabled(enabled)
        self.emit("calls-changed")

    def close_all(self) -> None:
        for peer in list(self._peers.values()):
            peer.close()
        self._peers.clear()
        for pc_id in list(self._trackers):
            self._end(pc_id)
        self.emit("calls-changed")

    def handle(self, request: dict, reply: Callable[[dict | None, str | None], None]) -> None:
        """Run one operation from the page; ``reply(result, error)`` exactly once."""
        op = request.get("op")
        try:
            handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
            if handler is None:
                reply(None, f"Unsupported operation: {op}")
                return
            handler(request, reply)
        except VoiceError as error:
            reply(None, str(error))
        except Exception as error:  # never leave the page waiting
            log.exception("Voice operation %s failed", op)
            reply(None, str(error) or error.__class__.__name__)

    # -- events, phases and captions -------------------------------------------------------

    def _relay(self, pc_id: str, event: dict) -> None:
        self._send_event(pc_id, event)
        tracker = self._trackers.get(pc_id)
        if tracker is None:
            return
        kind = event.get("type")
        if kind == "dcmessage":
            peer = self._peers.get(pc_id)
            if peer is not None and peer.label(event.get("dc")) == EVENTS_CHANNEL:
                self._apply(pc_id, tracker.feed(str(event.get("data", ""))))
        elif kind == "connectionstatechange":
            peer = self._peers.get(pc_id)
            self._apply(pc_id, tracker.connection(str(event.get("state", "")),
                                                  peer is not None and peer.hanging_up))
        elif kind == "mediaerror":
            message = str(event.get("message") or "").strip()
            text = _("The microphone or speakers stopped working.")
            self._apply(pc_id, tracker.error(f"{text} ({message})" if message else text))

    def _apply(self, pc_id: str, updates: list[tuple]) -> None:
        for update in updates:
            self.emit(*update)
        tracker = self._trackers.get(pc_id)
        if ("call-phase", "ended") in updates:
            self._stop_watchdog(pc_id)
        elif tracker is not None and ("call-phase", "thinking") in updates:
            self._watch_thinking(pc_id, tracker)

    def _watch_thinking(self, pc_id: str, tracker: CallTracker) -> None:
        """Back to "listening" when "thinking" hears nothing for ``THINKING_TIMEOUT_S``."""
        self._stop_watchdog(pc_id)
        seen = tracker.seen

        def check() -> bool:
            nonlocal seen
            if self._trackers.get(pc_id) is not tracker or tracker.phase != "thinking":
                self._watchdogs.pop(pc_id, None)
                return GLib.SOURCE_REMOVE
            if tracker.seen != seen or tracker.computing:
                seen = tracker.seen  # news, or long research: give it another while
                return GLib.SOURCE_CONTINUE
            self._watchdogs.pop(pc_id, None)
            self._apply(pc_id, tracker.stale())
            return GLib.SOURCE_REMOVE
        self._watchdogs[pc_id] = GLib.timeout_add(int(THINKING_TIMEOUT_S * 1000), check)

    def _stop_watchdog(self, pc_id: str) -> None:
        source = self._watchdogs.pop(pc_id, 0)
        if source:
            GLib.source_remove(source)

    def _end(self, pc_id: str) -> None:
        self._stop_watchdog(pc_id)
        tracker = self._trackers.pop(pc_id, None)
        if tracker is not None:
            self._apply(pc_id, tracker.end())
        if not self._trackers:
            self._levels.clear()

    def _on_level(self, role: str, level: float) -> None:
        previous = self._levels.get(role)
        if previous is not None and abs(previous - level) < 0.01:
            return
        self._levels[role] = level
        self.emit("audio-level", role, level)

    # -- operations ------------------------------------------------------------------------

    def _peer(self, request: dict) -> PeerConnection:
        peer = self._peers.get(str(request.get("pc")))
        if peer is None:
            raise VoiceError(_("This call is no longer active."))
        return peer

    def _op_requestMicrophone(self, _request, reply) -> None:
        # The call comes next: look at the devices meanwhile (the first look takes a
        # moment), to find the chosen ones and to check what "System default" records.
        if not _TEST_AUDIO:
            threading.Thread(target=_scan, name="dotdeck-audio-devices", daemon=True).start()
        self._request_microphone(lambda allowed: reply({"allowed": bool(allowed)}, None))

    def _op_create(self, _request, reply) -> None:
        if not available():
            raise VoiceError(_("Voice calls need the GStreamer WebRTC plugins: {missing}")
                             .format(missing=", ".join(missing_elements())))
        pc_id = str(next(self._ids))
        self._peers[pc_id] = PeerConnection(pc_id, lambda event: self._relay(pc_id, event),
                                            options=self.current_options(),
                                            on_level=self._on_level)
        reply({"pc": pc_id}, None)

    def _op_createDataChannel(self, request, reply) -> None:
        dc_id = self._peer(request).create_data_channel(str(request.get("label", "")),
                                                        bool(request.get("ordered", True)))
        reply({"dc": dc_id}, None)

    def _op_addTrack(self, request, reply) -> None:
        if request.get("kind") != "audio":
            raise VoiceError(_("Only audio tracks are supported."))
        if snap_microphone_blocked():
            raise VoiceError(_("The microphone is not connected to the Dotdeck snap. Run: "
                               "snap connect dotdeck:audio-record"))
        peer = self._peer(request)
        peer.add_microphone()
        # A call that starts muted (push to talk, join muted) shows it at once.
        self.emit("calls-changed")
        tracker = self._trackers.setdefault(peer.id, CallTracker())
        self._apply(peer.id, tracker.start())
        reply({}, None)

    def _op_createOffer(self, request, reply) -> None:
        self._peer(request).create_offer(
            lambda sdp, error: reply({"type": "offer", "sdp": sdp} if sdp else None, error))

    def _op_createAnswer(self, request, reply) -> None:
        self._peer(request).create_answer(
            lambda sdp, error: reply({"type": "answer", "sdp": sdp} if sdp else None, error))

    def _op_setLocalDescription(self, request, reply) -> None:
        self._peer(request).set_description(True, str(request.get("type")), str(request.get("sdp")),
                                            lambda error: reply({} if not error else None, error))

    def _op_setRemoteDescription(self, request, reply) -> None:
        self._peer(request).set_description(False, str(request.get("type")),
                                            str(request.get("sdp")),
                                            lambda error: reply({} if not error else None, error))

    def _op_addIceCandidate(self, request, reply) -> None:
        self._peer(request).add_ice_candidate(int(request.get("mline") or 0),
                                              str(request.get("candidate") or ""))
        reply({}, None)

    def _op_send(self, request, reply) -> None:
        peer = self._peer(request)
        dc_id, data = str(request.get("dc")), str(request.get("data", ""))
        peer.send(dc_id, data)
        reply({}, None)
        tracker = self._trackers.get(peer.id)
        if tracker is not None and peer.label(dc_id) == EVENTS_CHANNEL:
            self._apply(peer.id, tracker.feed(data, outgoing=True))

    def _op_closeChannel(self, request, reply) -> None:
        self._peer(request).close_channel(str(request.get("dc")))
        reply({}, None)

    def _op_setMicEnabled(self, request, reply) -> None:
        peer = self._peer(request)
        enabled = bool(request.get("enabled"))
        peer.page_muted = not enabled
        peer.set_mic_enabled(enabled)
        self.emit("calls-changed")
        reply({}, None)

    def _op_setSpeakerMuted(self, request, reply) -> None:
        peer = self._peer(request)
        muted = bool(request.get("muted"))
        peer.set_speaker_muted(muted)
        # "paused": the page paused the call's audio rather than muting it.
        peer.page_paused = muted and bool(request.get("paused"))
        reply({}, None)
        tracker = self._trackers.get(peer.id)
        if tracker is not None and peer.page_paused and peer.hanging_up:
            self._apply(peer.id, tracker.end())  # the conversation is over for the person

    def _op_close(self, request, reply) -> None:
        pc_id = str(request.get("pc"))
        peer = self._peers.pop(pc_id, None)
        if peer is not None:
            peer.close()
            self.emit("calls-changed")
        self._end(pc_id)
        reply({}, None)
