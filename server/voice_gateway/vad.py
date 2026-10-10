"""Voice activity detection on 16 kHz mono PCM: when the caller starts and stops talking.

An adaptive energy detector is enough here: the caller's audio has already been
through the browser's (or Dotdeck's) echo cancellation and noise suppression.
"""

from __future__ import annotations

import array
import math
from collections import deque

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2

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
                 max_ms: int = 30_000, margin_db: float = 10.0, absolute_db: float = -50.0):
        self.stop_frames = max(1, stop_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(1, max_ms // FRAME_MS)
        self.margin_db = margin_db
        self.absolute_db = absolute_db
        self.bias_db = 0.0                    # raised while the bot is talking
        self.floor_db = -70.0
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

    def _frame(self, frame: bytes):
        level = frame_db(frame)
        loud = level > self.threshold_db
        if not self.speaking:
            if not loud:
                # Track the background level, quicker downwards than upwards.
                weight = 0.3 if level < self.floor_db else 0.02
                self.floor_db += (level - self.floor_db) * weight
            self._preroll.append(frame)
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= 3:
                self.speaking = True
                self._utterance = list(self._preroll)
                self._voiced = self._loud_run
                self._silence = 0
                return (START,)
            return None
        self._utterance.append(frame)
        if level > self.threshold_db - 3:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.stop_frames or len(self._utterance) >= self.max_frames:
            return self._finish()
        return None

    def _finish(self):
        self.speaking = False
        self._loud_run = 0
        self._preroll.clear()
        utterance, voiced = b"".join(self._utterance), self._voiced
        self._utterance = []
        if voiced < self.min_frames:
            return (END, b"")
        # Keep a little of the trailing silence; drop the rest.
        keep = len(utterance) - max(0, self._silence - 10) * FRAME_BYTES
        return (END, utterance[:keep])

    def reset(self) -> None:
        self.speaking = False
        self._utterance = []
        self._loud_run = 0
        self._preroll.clear()
