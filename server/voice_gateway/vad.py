"""Voice activity detection on 16 kHz mono PCM: when the caller starts and stops talking.

An adaptive energy detector is enough here: the caller's audio has already been
through the browser's (or Dotdeck's) echo cancellation and noise suppression.

The background level is the quietest tenth of the last few seconds, measured all the
time (also while the caller talks). A real headset with automatic gain hums around
-40 dB; a floor learnt only between utterances started at -70 dB, took that hum for
speech from the first frame and never heard the caller stop.

Decisions use the level of the last 100 ms, not of a single 20 ms frame. With automatic
gain the background pumps from -41 to -27 dB frame by frame (12/10/2026): judged frame
by frame, a single loud one reset the 700 ms of silence, the caller was never heard to
stop and three loud frames in a row passed for speech, cancelling every reply. An
utterance also ends when its level falls ``drop_db`` below the speech heard so far, so
loud speech over a loud room still ends.

The caller is the voice near the microphone. Each utterance answered so far teaches how
loud that voice is (``voice_db``); one whose loudest 100 ms stays ``near_db`` below it is a
noise or a distant voice (a TV, a fan, the Dot's own voice leaking past echo cancellation)
and ends empty. The estimate only rises with what it hears: when it followed every
accepted utterance, a fan's bursts just inside the margin dragged it down until all of them
passed. A slow decay lets a caller who starts speaking more softly be heard again.
"""

from __future__ import annotations

import array
import math
from collections import deque

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2

SMOOTH_FRAMES = 5                             # 100 ms

START = "start"
END = "end"


def frame_db(frame: bytes) -> float:
    samples = array.array("h", frame)
    if not samples:
        return -120.0
    energy = sum(s * s for s in samples) / len(samples)
    return 10 * math.log10(energy / (32768.0 ** 2) + 1e-12)


class Vad:
    """Feed PCM; get ``(START,)`` and ``(END, utterance_pcm)`` events."""

    def __init__(self, stop_ms: int = 700, min_speech_ms: int = 250, preroll_ms: int = 300,
                 max_ms: int = 15_000, margin_db: float = 10.0, absolute_db: float = -50.0,
                 floor_window_ms: int = 5000, warmup_ms: int = 300, drop_db: float = 18.0,
                 near_db: float = 10.0, voice_decay_db: float = 0.1):
        self.stop_frames = max(1, stop_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(1, max_ms // FRAME_MS)
        self.margin_db = margin_db
        self.absolute_db = absolute_db
        self.drop_db = drop_db
        self.near_db = near_db
        self.voice_decay = voice_decay_db * FRAME_MS / 1000      # per frame
        self.voice_db = -120.0                # the caller's voice, from accepted utterances
        self.peak_db = -120.0                 # loudest 100 ms of the current utterance
        self.ignored = 0                      # utterances dropped as noise or distant
        self.bias_db = 0.0                    # raised while the bot is talking
        self.floor_db = -70.0
        self._levels: deque[float] = deque(maxlen=max(10, floor_window_ms // FRAME_MS))
        self._recent: deque[float] = deque(maxlen=SMOOTH_FRAMES)   # energy of the last frames
        self._speech_db = -120.0              # how loud this utterance is (slowly decays)
        self.warmup_frames = max(1, warmup_ms // FRAME_MS)
        self._pending = b""
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._loud_run = 0
        self.speaking = False
        self._utterance: list[bytes] = []
        self._voiced = 0
        self._silence = 0

    @property
    def threshold_db(self) -> float:
        return max(self.floor_db + self.margin_db, self.absolute_db) + self.bias_db

    @property
    def stop_db(self) -> float:
        """Below this the caller is silent: under the threshold, or well under their speech."""
        return max(self.threshold_db - 3, self._speech_db - self.drop_db)

    @property
    def near(self) -> bool:
        """The current utterance is as loud as the caller's voice (always, before the
        first one is known)."""
        return self.peak_db >= self.voice_db - self.near_db

    @property
    def confirmed(self) -> bool:
        """True once the current utterance has ``min_speech_ms`` of voice as loud as the
        caller's: real speech, not a click, a gust of background or a distant voice (only
        then may it interrupt the Dot)."""
        return self.speaking and self._voiced >= self.min_frames and self.near

    def feed(self, pcm: bytes) -> list[tuple]:
        events: list[tuple] = []
        data = self._pending + pcm
        usable = len(data) - len(data) % FRAME_BYTES
        self._pending = data[usable:]
        for offset in range(0, usable, FRAME_BYTES):
            event = self._frame(data[offset:offset + FRAME_BYTES])
            if event:
                events.append(event)
        return events

    def _track_floor(self, level: float) -> None:
        self._levels.append(level)
        if len(self._levels) >= 5:
            ordered = sorted(self._levels)
            # Never above -30 dB: a steady sound that loud is someone talking (or a test
            # tone), not a room's background.
            self.floor_db = min(-30.0, max(-90.0, ordered[len(ordered) // 10]))

    def _smoothed(self, frame: bytes) -> float:
        samples = array.array("h", frame)
        energy = sum(x * x for x in samples) / len(samples) if samples else 0.0
        self._recent.append(energy)
        mean = sum(self._recent) / len(self._recent)
        return 10 * math.log10(mean / (32768.0 ** 2) + 1e-12)

    def _frame(self, frame: bytes):
        raw = frame_db(frame)
        level = self._smoothed(frame)
        self._track_floor(level)
        self.voice_db = max(-120.0, self.voice_db - self.voice_decay)
        if not self.speaking:
            self._preroll.append(frame)
            # A frame counts when it is loud and so are the last 100 ms around it: a click
            # is loud for a frame or two, a gust of background never for 100 ms.
            loud = raw > self.threshold_db and level > self.threshold_db
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= 3 and len(self._levels) >= self.warmup_frames:
                self.speaking = True
                self._utterance = list(self._preroll)
                self._voiced = self._loud_run
                self._silence = 0
                self._speech_db = level
                self.peak_db = level
                return (START,)
            return None
        self._utterance.append(frame)
        self._speech_db = max(self._speech_db - 0.05, level)     # 2.5 dB/s
        self.peak_db = max(self.peak_db, level)
        if level > self.stop_db:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.stop_frames or len(self._utterance) >= self.max_frames:
            return self._finish()
        return None

    def _finish(self):
        self.speaking = False
        self._speech_db = -120.0
        self._loud_run = 0
        self._preroll.clear()
        utterance, voiced = b"".join(self._utterance), self._voiced
        self._utterance = []
        if voiced < self.min_frames:
            return (END, b"")
        if not self.near:
            self.ignored += 1
            return (END, b"")
        # The caller's voice: the first utterance sets it and a louder one moves it halfway
        # up. A softer one never moves it down; only the slow decay does.
        if self.voice_db <= -120.0:
            self.voice_db = self.peak_db
        elif self.peak_db > self.voice_db:
            self.voice_db += (self.peak_db - self.voice_db) / 2
        # Keep a little of the trailing silence; drop the rest.
        keep = len(utterance) - max(0, self._silence - 10) * FRAME_BYTES
        return (END, utterance[:keep])

    def reset(self) -> None:
        self.speaking = False
        self._speech_db = -120.0
        self._utterance = []
        self._loud_run = 0
        self._preroll.clear()
