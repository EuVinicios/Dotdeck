"""The sounds of a voice call, like a phone app: it rings while the call connects, a short
chime says the call is live, and a tone says it is over.

Every sound is made here, in code: soft sine notes whose edges rise and fall smoothly,
so nothing clicks and there are no audio files to ship or license. They play on the
call's speaker (Settings › Voice Calls › Speaker) in a pipeline of their own, never
through the call itself.

- ``ringback()``: two soft rings (1.2 s), then 2 s of quiet; it loops while the call
  connects, for at most ``CallSounds.RING_LIMIT_S``.
- ``connected()``: two quick rising notes, 140 ms and quiet. Once the call is live the
  microphone may hear the speakers, and the voice gateway's speech detector must not
  take the chime for the caller talking over the Dot's greeting.
- ``hang_up()``: two falling notes (~300 ms) when the call ends, whoever ends it.
- ``error_tone()``: two lower notes when the call ends with an error.

``CallSounds`` plays them as a ``VoiceBridge`` reports the call's phases; ``TonePlayer``
does the playing, without ever blocking the main loop.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from array import array
from functools import wraps
from typing import Callable, NamedTuple

from . import voice  # first: it picks the GStreamer version

from gi.repository import GLib, GObject, Gst  # noqa: E402

log = logging.getLogger(__name__)

RATE = 48000
_FULL = 32767


# -- the sounds ----------------------------------------------------------------------------

class Note(NamedTuple):
    """One note of a sound: ``pitches`` (Hz) sound together, at most ``level`` loud."""

    start: float                  # seconds from the start of the sound
    length: float                 # seconds
    pitches: tuple[float, ...]
    level: float                  # peak, as a fraction of full scale
    attack: float = 0.006         # smooth rise (s)
    release: float = 0.04         # smooth fall to silence at the end (s)
    decay: float = 0.0            # time constant of a bell-like fade (s); 0 = steady
    floor: float = 0.0            # where that fade levels off (0..1)


def render(notes: tuple[Note, ...], length: float) -> array:
    """Mono 16-bit samples at ``RATE`` (native byte order), ``length`` seconds long.

    Every note starts and ends at silence on a raised-cosine curve, so the sound never
    clicks, and the mix is clamped (the notes are kept well under full scale anyway).
    """
    total = int(round(length * RATE))
    mix = [0.0] * total
    for note in notes:
        first = int(round(note.start * RATE))
        count = min(int(round(note.length * RATE)), total - first)
        attack = max(1, int(note.attack * RATE))
        release = max(1, int(note.release * RATE))
        steps = [2 * math.pi * pitch / RATE for pitch in note.pitches]
        scale = note.level / len(steps)
        fall = math.exp(-1.0 / (note.decay * RATE)) if note.decay > 0 else 1.0
        bell = 1.0
        for i in range(count):
            envelope = note.floor + (1.0 - note.floor) * bell
            bell *= fall
            if i < attack:
                envelope *= 0.5 - 0.5 * math.cos(math.pi * i / attack)
            left = count - 1 - i
            if left < release:
                envelope *= 0.5 - 0.5 * math.cos(math.pi * left / release)
            mix[first + i] += scale * envelope * sum(math.sin(step * i) for step in steps)
    return array("h", (max(-_FULL, min(_FULL, round(v * _FULL))) for v in mix))


RING_ON_S = 1.2
RING_CYCLE_S = 3.2
# A4 and C#5 together, a major third: a ring that sounds like a phone, but soft.
_RING_CHORD = (440.0, 554.37)
_RINGBACK = (
    Note(0.0, 0.5, _RING_CHORD, 0.2, attack=0.03, release=0.08, decay=0.5, floor=0.6),
    Note(0.7, 0.5, _RING_CHORD, 0.2, attack=0.03, release=0.08, decay=0.5, floor=0.6),
)
# Short and quiet on purpose: it plays as the call goes live, when the microphone already
# sends what it hears, and the voice gateway takes 60 ms of loud sound for the caller
# starting to talk (which would cut the Dot's greeting short). The gateway's detector
# warms up during the first 300 ms of a call: 140 ms, plus the ~0.1 s the ringing takes
# to give way, ends before it listens. Only just: a speaker with more latency (Bluetooth)
# heard by the microphone ends it after, and the greeting can be cut; see the gateway.
CONNECTED_S = 0.14
_CONNECTED = (    # A5 then E6, rising
    Note(0.0, 0.07, (880.0,), 0.1, attack=0.004, release=0.025, decay=0.05, floor=0.25),
    Note(0.055, 0.085, (1318.51,), 0.1, attack=0.004, release=0.04, decay=0.05, floor=0.15),
)
HANG_UP_S = 0.3
_HANG_UP = (      # E5 then A4, falling
    Note(0.0, 0.15, (659.25,), 0.16, attack=0.005, release=0.04, decay=0.12, floor=0.3),
    Note(0.13, 0.17, (440.0,), 0.16, attack=0.005, release=0.06, decay=0.12, floor=0.2),
)
ERROR_S = 0.38
_ERROR = (        # F4 then D4, lower and a little longer
    Note(0.0, 0.16, (349.23,), 0.17, attack=0.006, release=0.04, decay=0.15, floor=0.4),
    Note(0.17, 0.21, (293.66,), 0.17, attack=0.006, release=0.07, decay=0.15, floor=0.3),
)


_making = threading.Lock()


def _once(make: Callable[[], array]) -> Callable[[], array]:
    """``make()`` runs once, even when the warm-up thread and a call ask at the same time:
    every caller gets the very same samples."""
    made: list[array] = []

    @wraps(make)
    def get() -> array:
        if not made:
            with _making:
                if not made:
                    made.append(make())
        return made[0]
    return get


@_once
def ringback() -> array:
    """One ring cycle: two soft rings in the first 1.2 s, then quiet up to 3.2 s."""
    return render(_RINGBACK, RING_CYCLE_S)


@_once
def connected() -> array:
    return render(_CONNECTED, CONNECTED_S)


@_once
def hang_up() -> array:
    return render(_HANG_UP, HANG_UP_S)


@_once
def error_tone() -> array:
    return render(_ERROR, ERROR_S)


SOUNDS: dict[str, Callable[[], array]] = {
    "ringback": ringback, "connected": connected, "hang-up": hang_up, "error": error_tone}


def prepare() -> None:
    """Make every sound now (~0.1 s of work) on a worker thread, so the first ring of a
    call never makes the window wait."""
    def work():
        for make in SOUNDS.values():
            make()
    threading.Thread(target=work, name="dotdeck-call-sounds", daemon=True).start()


# -- streaming -----------------------------------------------------------------------------

FADE = RATE // 100           # 10 ms: how fast a sound gives way to the next one
CHUNK = RATE // 50           # 20 ms of audio per buffer


class SoundFeed:
    """The samples a player streams: one sound (maybe looping), then maybe another.

    Thread-safe plain Python: the main loop calls ``switch`` and ``finish``, the audio
    thread calls ``read``. Both first fade the sound that is playing out over ``FADE``
    samples, so nothing clicks; a sound nobody heard yet is replaced at once.
    """

    def __init__(self, samples: array, loop: bool = False):
        self._lock = threading.Lock()
        self._sound: array | None = None
        self._loop = False
        self._pos = 0
        self._fresh = True        # nothing of the current sound was read yet
        self._fade_left = 0
        self._next: tuple[array | None, bool] = (None, False)
        self.ended = False        # read() said there is nothing more
        self._set(samples, loop)

    def _set(self, samples: array | None, loop: bool) -> None:
        self._sound = samples if samples is not None and len(samples) else None
        self._loop = loop
        self._pos = 0
        self._fresh = True
        self._fade_left = 0
        self._next = (None, False)

    def switch(self, samples: array, loop: bool = False) -> bool:
        """Play ``samples`` next, after a short fade; False once the feed has ended."""
        with self._lock:
            if self.ended:
                return False
            if self._sound is None or self._fresh:
                self._set(samples, loop)
            else:
                self._next = (samples, loop)
                self._fade_left = self._fade_left or FADE
            return True

    def finish(self) -> None:
        """Fade out and end."""
        with self._lock:
            if self.ended:
                return
            if self._sound is None or self._fresh:
                self._set(None, False)
            else:
                self._next = (None, False)
                self._fade_left = self._fade_left or FADE

    def read(self, count: int) -> array | None:
        """Up to ``count`` samples, or None when the feed is over."""
        with self._lock:
            if self.ended:
                return None
            out = array("h")
            while len(out) < count and self._sound is not None:
                sound = self._sound
                remaining = len(sound) - self._pos
                if remaining <= 0:
                    if self._loop:
                        self._pos = 0
                    else:
                        self._set(*self._next)
                    continue
                self._fresh = False
                take = min(count - len(out), remaining)
                if self._fade_left:
                    take = min(take, self._fade_left)
                    start, left = self._pos, self._fade_left
                    out.extend(int(sound[start + k] * (left - k) / FADE) for k in range(take))
                    self._fade_left -= take
                    self._pos += take
                    if not self._fade_left:
                        self._set(*self._next)
                    continue
                out.extend(sound[self._pos:self._pos + take])
                self._pos += take
            if not out:
                self.ended = True
                return None
            return out


# How much audio the speaker queues ahead. Small, so a switch (the ringing giving way to
# the connected chime) is heard at once; 80 ms still rides out a busy moment.
SINK_BUFFER_US = 80_000
SINK_LATENCY_US = 10_000
DRAIN_TIMEOUT_S = 3


def _tune_sink(element: Gst.Element) -> None:
    factory = element.get_factory()
    if factory is None or "Sink" not in (factory.get_metadata("klass") or ""):
        return
    if element.find_property("buffer-time") is not None:
        element.set_property("buffer-time", SINK_BUFFER_US)
        if element.find_property("latency-time") is not None:
            element.set_property("latency-time", SINK_LATENCY_US)
    if factory.get_name() == "fakesink":
        element.set_property("sync", True)  # tests: play in real time, not in a busy loop


class _Stream:
    """One pipeline (appsrc → speaker) that plays a ``SoundFeed`` until it ends."""

    def __init__(self, feed: SoundFeed, device_id: str, on_end: Callable[[_Stream], None]):
        self.feed = feed
        self._on_end = on_end
        self._offset = 0
        self._closed = False
        self._timeout = 0
        if not voice._ensure_gst():
            raise voice.VoiceError("GStreamer is not available.")
        order = "S16LE" if sys.byteorder == "little" else "S16BE"
        pipeline = Gst.Pipeline.new("dotdeck-call-sounds")
        # autoaudiosink makes the real sink only when it starts: tune it then.
        pipeline.connect("deep-element-added", lambda _p, _bin, element: _tune_sink(element))
        chain = Gst.parse_bin_from_description(
            f"appsrc name=sounds format=time caps=audio/x-raw,format={order},rate={RATE},"
            "channels=1,layout=interleaved ! audioconvert ! audioresample", True)
        sink, _name = voice.audio_element("sink", device_id, "call-sounds-out")
        _tune_sink(sink)
        pipeline.add(chain)
        pipeline.add(sink)
        if not chain.link(sink):
            pipeline.set_state(Gst.State.NULL)
            raise voice.VoiceError("The speakers could not be opened.")
        self._appsrc = chain.get_by_name("sounds")
        self._data_handler = self._appsrc.connect("need-data", self._on_need_data)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self.pipeline = pipeline

    def start(self) -> bool:
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log.info("Could not start the call sounds")
            self.close()
            return False
        return True

    def _on_need_data(self, appsrc, _length) -> None:
        # GStreamer's streaming thread.
        chunk = self.feed.read(CHUNK)
        if chunk is None:
            appsrc.emit("end-of-stream")
            return
        buffer = Gst.Buffer.new_wrapped(chunk.tobytes())
        buffer.pts = Gst.util_uint64_scale(self._offset, Gst.SECOND, RATE)
        buffer.duration = Gst.util_uint64_scale(len(chunk), Gst.SECOND, RATE)
        self._offset += len(chunk)
        appsrc.emit("push-buffer", buffer)

    def _on_message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            log.info("Call sound failed: %s", message.parse_error()[0].message)
            self.close()
        elif message.type == Gst.MessageType.EOS:
            self.close()

    def close_after(self, seconds: float) -> None:
        """A stream fading out ends by itself; this makes sure of it."""
        if not self._closed and not self._timeout:
            self._timeout = GLib.timeout_add(int(seconds * 1000), self._timed_out)

    def _timed_out(self) -> bool:
        self._timeout = 0
        self.close()
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        self.feed.finish()
        bus = self.pipeline.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self._appsrc.disconnect(self._data_handler)
        self._on_end(self)


class TonePlayer:
    """Plays sounds on a speaker without ever blocking the main loop.

    ``play(samples, loop)`` starts a sound or, while one plays, switches to it after a
    10 ms fade in the same pipeline: the speaker stays open, nothing clicks and the new
    sound is heard at once. ``stop()`` fades out; ``close()`` silences everything now.
    ``device()`` names the speaker ("" = the system default) each time one opens.
    """

    def __init__(self, device: Callable[[], str] = lambda: ""):
        self._device = device
        self._stream: _Stream | None = None
        self._ending: set[_Stream] = set()

    @property
    def playing(self) -> bool:
        return self._stream is not None

    @property
    def busy(self) -> bool:
        """Something still plays or fades out."""
        return self._stream is not None or bool(self._ending)

    def play(self, samples: array, loop: bool = False) -> None:
        if self._stream is not None and self._stream.feed.switch(samples, loop):
            return
        self._retire(self._stream)
        self._stream = None
        try:
            device = str(self._device() or "")
        except Exception:
            log.exception("Could not read the speaker setting")
            device = ""
        try:
            stream = _Stream(SoundFeed(samples, loop), device, self._stream_ended)
        except Exception:  # no GStreamer, no speaker: a call never fails over a sound
            log.info("Could not play a call sound", exc_info=True)
            return
        self._stream = stream
        stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.feed.finish()
            self._retire(stream)

    def close(self) -> None:
        for stream in [self._stream, *self._ending]:
            if stream is not None:
                stream.close()
        self._stream = None
        self._ending.clear()

    def _retire(self, stream: _Stream | None) -> None:
        if stream is not None:
            self._ending.add(stream)
            stream.close_after(DRAIN_TIMEOUT_S)

    def _stream_ended(self, stream: _Stream) -> None:
        if stream is self._stream:
            self._stream = None
        self._ending.discard(stream)


# -- the call ------------------------------------------------------------------------------

_LIVE = ("listening", "thinking", "speaking")


class CallSounds:
    """Plays the call sounds as a ``VoiceBridge`` reports a call's phases.

    - "connecting" before the call was ever live: ringing, looping, for at most
      ``RING_LIMIT_S`` (a reconnection in the middle of a call stays quiet);
    - the first "listening", "thinking" or "speaking": the ringing stops and the
      connected chime plays;
    - "ended": the hang-up tone, or the error tone when a call error came right before.

    ``enabled()`` is asked before each sound (the "call-sounds" setting); ``refresh()``
    says it changed, and silences what plays when it is now off. ``player`` needs
    ``play(samples, loop)``, ``stop()`` and ``close()`` (a ``TonePlayer`` on
    ``speaker()`` by default). ``close()`` silences it for good (the app is quitting).
    """

    RING_LIMIT_S = 45.0
    # A call error this close before "ended" makes it end with the error tone.
    ERROR_GRACE_S = 2.0

    def __init__(self, enabled: Callable[[], bool] = lambda: True,
                 speaker: Callable[[], str] = lambda: "", player=None):
        self._enabled = enabled
        self.player = player if player is not None else TonePlayer(speaker)
        self.state = "idle"               # idle | ringing | live
        self._ring_timer = 0
        self._error_at: float | None = None
        self._closed = False
        prepare()

    @classmethod
    def for_settings(cls, settings: GObject.Object, player=None) -> CallSounds:
        """Sounds that follow the app settings: "call-sounds" turns them on and off (off
        silences a ring at once) and "speaker-device" is where they play."""
        sounds = cls(lambda: bool(settings.get("call-sounds")),
                     lambda: settings.get("speaker-device"), player)

        def changed(_settings, key: str) -> None:
            if key == "call-sounds":
                sounds.refresh()
        settings.connect("changed", changed)
        return sounds

    def refresh(self) -> None:
        """The setting changed: when it is off now, whatever plays fades out."""
        if not self._closed and not self._allowed():
            self.player.stop()

    def watch(self, bridge: GObject.Object) -> None:
        """Follow ``bridge``'s "call-phase" and "call-error" signals."""
        kind = type(bridge)
        if GObject.signal_lookup("call-phase", kind):
            bridge.connect("call-phase", lambda _bridge, phase: self.phase(phase))
        if GObject.signal_lookup("call-error", kind):
            bridge.connect("call-error", lambda _bridge, message: self.error(message))

    def phase(self, phase: str) -> None:
        if self._closed:
            return
        if phase == "connecting":
            if self.state == "idle":
                self.state = "ringing"
                self._error_at = None
                self._play(ringback(), loop=True)
                self._ring_timer = GLib.timeout_add(int(self.RING_LIMIT_S * 1000),
                                                    self._ring_too_long)
        elif phase in _LIVE:
            if self.state != "live":
                self._stop_ring_timer()
                self.state = "live"
                self._play(connected())
        elif phase == "ended":
            if self.state != "idle":
                self._stop_ring_timer()
                errored = self._error_at is not None and \
                    time.monotonic() - self._error_at <= self.ERROR_GRACE_S
                self.state = "idle"
                self._error_at = None
                self._play(error_tone() if errored else hang_up())

    def error(self, _message: str = "") -> None:
        if not self._closed and self.state != "idle":
            self._error_at = time.monotonic()

    def close(self) -> None:
        self._closed = True
        self._stop_ring_timer()
        self.state = "idle"
        self.player.close()

    def _allowed(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            log.exception("Could not read the call sounds setting")
            return False

    def _play(self, samples: array, loop: bool = False) -> None:
        if self._allowed():
            self.player.play(samples, loop)
        else:
            self.player.stop()  # turned off while it was ringing

    def _ring_too_long(self) -> bool:
        self._ring_timer = 0
        if self.state == "ringing":
            self.player.stop()
        return GLib.SOURCE_REMOVE

    def _stop_ring_timer(self) -> None:
        if self._ring_timer:
            GLib.source_remove(self._ring_timer)
            self._ring_timer = 0
