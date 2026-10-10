"""The call sounds: how they are made, how they stream, and when a call plays them.

The player runs without audio hardware: tests.helpers sets DOTDECK_TEST_AUDIO=1, which
turns the speakers into a fake sink (played in real time here).
"""

from __future__ import annotations

import math
import threading
import unittest
from array import array
from unittest import mock

from tests.helpers import run_until  # noqa: F401  (sets the test environment first)

from src.core import call_sounds, voice  # noqa: E402
from tests.test_voice import StubPeer  # noqa: E402

RATE = call_sounds.RATE
FULL = 32767
NOTES = {"ringback": call_sounds._RINGBACK, "connected": call_sounds._CONNECTED,
         "hang-up": call_sounds._HANG_UP, "error": call_sounds._ERROR}


def max_step(notes) -> float:
    """The largest change from one sample to the next that the notes allow: a sine of
    amplitude A at f Hz moves at most A·2πf/RATE per sample. More than that is a click."""
    return FULL * sum(n.level * 2 * math.pi * max(n.pitches) / RATE for n in notes) * 1.1


def steps(samples) -> int:
    return max(abs(samples[i + 1] - samples[i]) for i in range(len(samples) - 1))


def pitch(samples) -> float:
    """Rough pitch (Hz) from the zero crossings of the loud part."""
    peak = max(abs(s) for s in samples)
    loud = [s for s in samples if abs(s) > 0.05 * peak] or list(samples)
    crossings = sum(1 for a, b in zip(loud, loud[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(loud) / RATE)


def halves(samples):
    middle = len(samples) // 2
    return samples[:middle], samples[middle:]


class SoundShapeTest(unittest.TestCase):
    def test_lengths(self):
        ring = call_sounds.ringback()
        self.assertEqual(len(ring), int(3.2 * RATE))
        loud = [i for i, s in enumerate(ring) if s]
        self.assertLess(loud[-1] / RATE, 1.21)          # ~1.2 s of ringing…
        self.assertTrue(all(s == 0 for s in ring[int(1.2 * RATE):]))  # …then 2 s of quiet
        # Two rings with a short breath between them.
        self.assertTrue(all(abs(s) < 50 for s in ring[int(0.51 * RATE):int(0.69 * RATE)]))
        self.assertLessEqual(len(call_sounds.connected()), int(0.18 * RATE))
        self.assertTrue(0.25 <= len(call_sounds.hang_up()) / RATE <= 0.35)
        self.assertTrue(0.25 <= len(call_sounds.error_tone()) / RATE <= 0.45)
        self.assertNotEqual(call_sounds.error_tone(), call_sounds.hang_up())

    def test_soft_and_never_clipping(self):
        limits = {"ringback": (0.1, 0.3), "connected": (0.05, 0.15), "hang-up": (0.08, 0.25),
                  "error": (0.08, 0.25)}
        for name, make in call_sounds.SOUNDS.items():
            peak = max(abs(s) for s in make()) / FULL
            low, high = limits[name]
            self.assertTrue(low <= peak <= high, f"{name}: peak {peak:.3f}")

    def test_smooth_edges_and_no_clicks(self):
        for name, make in call_sounds.SOUNDS.items():
            samples = make()
            peak = max(abs(s) for s in samples)
            self.assertEqual((samples[0], samples[-1]), (0, 0), name)
            rise, fall = RATE // 2000, RATE // 500  # 0.5 ms, 2 ms
            self.assertLess(max(abs(s) for s in samples[:rise]), 0.1 * peak, f"{name} fades in")
            self.assertLess(max(abs(s) for s in samples[-fall:]), 0.05 * peak,
                            f"{name} fades out")
            self.assertLessEqual(steps(samples), max_step(NOTES[name]), name)
        ring = call_sounds.ringback()
        self.assertLessEqual(steps(ring + ring), max_step(NOTES["ringback"]), "loop seam")

    def test_connected_rises_and_the_endings_fall(self):
        first, second = halves(call_sounds.connected())
        self.assertGreater(pitch(second), pitch(first) * 1.2)
        for name in ("hang-up", "error"):
            first, second = halves(call_sounds.SOUNDS[name]())
            self.assertLess(pitch(second), pitch(first) * 0.9, name)
        self.assertLess(pitch(call_sounds.error_tone()), pitch(call_sounds.hang_up()))

    def test_render_mixes_notes_and_clamps(self):
        loud = call_sounds.Note(0.0, 0.01, (1000.0,), 3.0, attack=0.001, release=0.001)
        samples = call_sounds.render((loud,), 0.02)
        self.assertEqual(len(samples), int(0.02 * RATE))
        self.assertEqual(max(samples), FULL)
        self.assertEqual(min(samples), -FULL)
        self.assertTrue(all(s == 0 for s in samples[int(0.01 * RATE):]))

    def test_each_sound_is_made_once_even_when_two_threads_ask(self):
        made = []
        gate = threading.Event()

        def slow():
            gate.wait(1)
            made.append(array("h", [0, 1, 0]))
            return made[-1]

        get = call_sounds._once(slow)
        results = []
        workers = [threading.Thread(target=lambda: results.append(get())) for _ in range(3)]
        for worker in workers:
            worker.start()
        gate.set()
        for worker in workers:
            worker.join(5)
        self.assertEqual(len(made), 1)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result is made[0] for result in results))
        self.assertIs(get(), made[0])


def read_all(feed: call_sounds.SoundFeed, limit: int = 10 * RATE) -> array:
    out = array("h")
    while len(out) < limit:
        chunk = feed.read(call_sounds.CHUNK)
        if chunk is None:
            break
        out.extend(chunk)
    return out


class SoundFeedTest(unittest.TestCase):
    def test_plays_a_sound_then_ends(self):
        feed = call_sounds.SoundFeed(call_sounds.connected())
        self.assertEqual(read_all(feed), call_sounds.connected())
        self.assertTrue(feed.ended)
        self.assertIsNone(feed.read(10))
        self.assertFalse(feed.switch(call_sounds.hang_up()))

    def test_loops_until_told_otherwise(self):
        ring = call_sounds.ringback()
        feed = call_sounds.SoundFeed(ring, loop=True)
        out = read_all(feed, limit=2 * len(ring) + 1000)
        self.assertEqual(out[:2 * len(ring)], ring + ring)
        self.assertFalse(feed.ended)

    def test_switching_fades_the_ring_into_the_chime(self):
        ring, chime = call_sounds.ringback(), call_sounds.connected()
        feed = call_sounds.SoundFeed(ring, loop=True)
        before = read_all(feed, limit=int(0.3 * RATE))  # in the middle of the first ring
        cut = len(before)
        self.assertTrue(feed.switch(chime))
        out = before + read_all(feed)
        fade = out[cut:cut + call_sounds.FADE]
        self.assertTrue(all(abs(o) <= abs(r) for o, r in zip(fade, ring[cut:])))
        self.assertGreater(max(abs(s) for s in ring[cut:cut + call_sounds.FADE]), 1000)
        self.assertEqual(out[cut + call_sounds.FADE:], chime)
        self.assertLessEqual(steps(out), max_step(NOTES["ringback"] + NOTES["connected"]))

    def test_a_sound_nobody_heard_is_replaced_at_once(self):
        feed = call_sounds.SoundFeed(call_sounds.ringback(), loop=True)
        feed.switch(call_sounds.connected())
        self.assertEqual(read_all(feed), call_sounds.connected())

    def test_finish_fades_out_and_ends(self):
        ring = call_sounds.ringback()
        feed = call_sounds.SoundFeed(ring, loop=True)
        before = read_all(feed, limit=int(0.3 * RATE))
        feed.finish()
        tail = read_all(feed)
        self.assertEqual(len(tail), call_sounds.FADE)
        self.assertLess(abs(tail[-1]), 20)
        self.assertLessEqual(steps(before + tail), max_step(NOTES["ringback"]))
        self.assertTrue(feed.ended)

    def test_an_empty_sound_ends_at_once(self):
        feed = call_sounds.SoundFeed(array("h"), loop=True)
        self.assertIsNone(feed.read(call_sounds.CHUNK))


class TonePlayerTest(unittest.TestCase):
    def setUp(self):
        self.player = call_sounds.TonePlayer()
        self.addCleanup(self.player.close)

    def test_plays_a_sound_to_the_end(self):
        self.player.play(call_sounds.connected())
        self.assertTrue(self.player.playing)
        self.assertTrue(run_until(lambda: not self.player.busy, 5))

    def test_the_chime_follows_the_ring_in_the_same_pipeline(self):
        self.player.play(call_sounds.ringback(), loop=True)
        stream = self.player._stream
        run_until(lambda: False, 0.3)
        self.assertTrue(self.player.playing)
        self.player.play(call_sounds.connected())
        self.assertIs(self.player._stream, stream)
        self.assertTrue(run_until(lambda: not self.player.busy, 5))
        self.assertGreater(stream._offset, len(call_sounds.connected()))

    def test_stop_fades_out_and_close_is_at_once(self):
        self.player.play(call_sounds.ringback(), loop=True)
        run_until(lambda: False, 0.2)
        self.player.stop()
        self.assertFalse(self.player.playing)
        self.assertTrue(run_until(lambda: not self.player.busy, 3))
        self.player.play(call_sounds.ringback(), loop=True)
        stream = self.player._stream
        self.player.close()
        self.assertFalse(self.player.busy)
        self.assertEqual(stream.pipeline.get_state(0)[1], call_sounds.Gst.State.NULL)
        self.player.close()  # twice is fine

    def test_a_new_sound_after_the_end_opens_a_new_pipeline(self):
        self.player.play(call_sounds.connected())
        first = self.player._stream
        self.assertTrue(run_until(lambda: not self.player.busy, 5))
        self.player.play(call_sounds.hang_up())
        self.assertIsNot(self.player._stream, first)
        self.assertTrue(run_until(lambda: not self.player.busy, 5))

    def test_the_chosen_speaker_is_used_and_failures_stay_quiet(self):
        asked = []
        real = voice.audio_element

        def element(kind, device_id="", name=None):
            asked.append((kind, device_id))
            return real(kind, device_id, name)

        player = call_sounds.TonePlayer(lambda: "alsa_output.usb-headset")
        self.addCleanup(player.close)
        with mock.patch.object(voice, "audio_element", element):
            player.play(call_sounds.connected())
        self.assertEqual(asked, [("sink", "alsa_output.usb-headset")])
        self.assertTrue(run_until(lambda: not player.busy, 5))
        with mock.patch.object(voice, "audio_element",
                               mock.Mock(side_effect=voice.VoiceError("no speakers"))):
            player.play(call_sounds.connected())  # never raises
        self.assertFalse(player.busy)


class FakePlayer:
    def __init__(self):
        self.calls: list[tuple] = []

    def play(self, samples, loop=False):
        name = next(n for n, make in call_sounds.SOUNDS.items() if make() is samples)
        self.calls.append(("play", name, loop))

    def stop(self):
        self.calls.append(("stop",))

    def close(self):
        self.calls.append(("close",))

    def plays(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "play"]


class CallSoundsTest(unittest.TestCase):
    def setUp(self):
        self.enabled = True
        self.player = FakePlayer()
        self.sounds = call_sounds.CallSounds(lambda: self.enabled, player=self.player)
        self.addCleanup(self.sounds.close)

    def phases(self, *phases):
        for phase in phases:
            self.sounds.phase(phase)

    def test_rings_while_connecting_then_chimes_once_live(self):
        self.phases("connecting")
        self.assertEqual(self.player.calls, [("play", "ringback", True)])
        self.assertTrue(self.sounds._ring_timer)
        self.phases("connecting")  # no new ring
        self.phases("listening")
        self.assertEqual(self.player.calls[-1], ("play", "connected", False))
        self.assertFalse(self.sounds._ring_timer)
        self.phases("thinking", "speaking", "listening")
        self.assertEqual(self.player.plays(), ["ringback", "connected"])
        self.phases("ended")
        self.assertEqual(self.player.plays(), ["ringback", "connected", "hang-up"])
        self.assertEqual(self.sounds.state, "idle")
        self.phases("ended")  # nothing more
        self.assertEqual(len(self.player.plays()), 3)

    def test_a_reconnection_mid_call_stays_quiet(self):
        self.phases("connecting", "listening", "connecting", "listening", "speaking")
        self.assertEqual(self.player.plays(), ["ringback", "connected"])

    def test_hanging_up_while_it_rings(self):
        self.phases("connecting", "ended")
        self.assertEqual(self.player.plays(), ["ringback", "hang-up"])
        self.assertFalse(self.sounds._ring_timer)

    def test_a_call_error_ends_with_the_error_tone(self):
        self.phases("connecting", "listening")
        self.sounds.error("The voice connection dropped.")
        self.phases("ended")
        self.assertEqual(self.player.plays(), ["ringback", "connected", "error"])
        # The next call starts clean.
        self.phases("connecting", "ended")
        self.assertEqual(self.player.plays()[-1], "hang-up")

    def test_a_call_that_fails_to_connect_ends_with_the_error_tone(self):
        self.phases("connecting")
        self.sounds.error("The voice connection dropped.")
        self.phases("ended")
        self.assertEqual(self.player.plays(), ["ringback", "error"])

    def test_an_old_error_is_forgotten(self):
        self.phases("connecting", "listening")
        self.sounds.error("The voice provider reported an error.")  # the call went on
        self.sounds._error_at -= call_sounds.CallSounds.ERROR_GRACE_S + 1
        self.phases("ended")
        self.assertEqual(self.player.plays()[-1], "hang-up")

    def test_errors_and_endings_without_a_call_are_quiet(self):
        self.sounds.error("Unknown data channel.")
        self.phases("ended", "listening")
        self.assertEqual(self.player.plays(), ["connected"])  # a call the page started live
        self.phases("ended")
        self.assertEqual(self.player.plays(), ["connected", "hang-up"])

    def test_nothing_plays_when_the_setting_is_off(self):
        self.enabled = False
        self.phases("connecting", "listening", "ended")
        self.assertEqual(self.player.plays(), [])
        self.enabled = True  # turned on in the middle of a call: from the next sound on
        self.phases("connecting", "listening")
        self.enabled = False
        self.phases("ended")
        self.assertEqual(self.player.plays(), ["ringback", "connected"])

    def test_turning_it_off_while_it_rings_stops_the_ring(self):
        self.phases("connecting")
        self.enabled = False
        self.phases("listening")
        self.assertEqual(self.player.calls, [("play", "ringback", True), ("stop",)])

    def test_the_ring_gives_up_after_the_limit(self):
        with mock.patch.object(call_sounds.CallSounds, "RING_LIMIT_S", 0.05):
            self.phases("connecting")
            self.assertTrue(run_until(lambda: ("stop",) in self.player.calls, 2))
        self.assertEqual(self.sounds.state, "ringing")
        self.phases("listening")  # it connected after all
        self.assertEqual(self.player.plays(), ["ringback", "connected"])

    def test_close_silences_it_for_good(self):
        self.phases("connecting")
        self.sounds.close()
        self.assertEqual(self.player.calls[-1], ("close",))
        self.assertFalse(self.sounds._ring_timer)
        self.phases("listening", "ended")
        self.sounds.error("x")
        self.assertEqual(self.player.plays(), ["ringback"])

    def test_a_setting_that_cannot_be_read_means_quiet(self):
        sounds = call_sounds.CallSounds(mock.Mock(side_effect=KeyError("call-sounds")),
                                        player=self.player)
        self.addCleanup(sounds.close)
        with self.assertLogs(call_sounds.log, "ERROR"):
            sounds.phase("connecting")
        self.assertEqual(self.player.plays(), [])


class BridgeSoundsTest(unittest.TestCase):
    """The sounds follow a real ``VoiceBridge`` (with a stand-in peer, no WebRTC)."""

    def setUp(self):
        self.player = FakePlayer()
        self.sounds = call_sounds.CallSounds(player=self.player)
        self.addCleanup(self.sounds.close)
        self.bridge = voice.VoiceBridge(lambda pc, e: None, lambda cb: cb(True), lambda: True)
        self.addCleanup(self.bridge.close_all)
        self.sounds.watch(self.bridge)
        self.bridge._peers["1"] = StubPeer("1")
        tracker = self.bridge._trackers["1"] = voice.CallTracker()
        self.bridge._apply("1", tracker.start())

    def op(self, **request):
        box = {}
        self.bridge.handle(dict(request, pc="1"), lambda r, e: box.update(result=r, error=e))
        self.assertIsNone(box["error"])

    def connection(self, state: str):
        self.bridge._relay("1", {"type": "connectionstatechange", "state": state})

    def test_ring_chime_and_hang_up(self):
        self.assertEqual(self.player.plays(), ["ringback"])
        self.connection("connected")
        self.assertEqual(self.player.plays(), ["ringback", "connected"])
        # useVoice.ts end(): microphone off and audio paused, then ~40 s saving the call.
        self.op(op="setMicEnabled", enabled=False)
        self.op(op="setSpeakerMuted", muted=True, paused=True)
        self.assertEqual(self.player.plays(), ["ringback", "connected", "hang-up"])
        self.connection("failed")
        self.op(op="close")
        self.assertEqual(self.player.plays(), ["ringback", "connected", "hang-up"])

    def test_a_dropped_connection_ends_with_the_error_tone(self):
        self.connection("connected")
        self.connection("disconnected")  # back to "connecting": no ringing mid-call
        self.connection("failed")
        self.assertEqual(self.player.plays(), ["ringback", "connected", "error"])

    def test_the_server_hanging_up_first_is_a_plain_hang_up(self):
        self.connection("connected")
        self.op(op="setMicEnabled", enabled=False)
        self.op(op="setSpeakerMuted", muted=True)
        self.connection("failed")
        self.assertEqual(self.player.plays(), ["ringback", "connected", "hang-up"])

    def test_closing_the_page_hangs_up(self):
        self.bridge.close_all()  # another server, or the view went away
        self.assertEqual(self.player.plays(), ["ringback", "hang-up"])


if __name__ == "__main__":
    unittest.main()
