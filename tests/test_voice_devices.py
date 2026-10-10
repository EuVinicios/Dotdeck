"""Audio devices, the microphone meter and the speaker test of the voice settings.

They run without audio hardware: tests.helpers sets DOTDECK_TEST_AUDIO=1, which turns
the microphone into a test tone and the speakers into a fake sink. Device lists come
from the real sound server when there is one and may be empty (CI); the selection
logic is also checked with stand-in devices so it does not depend on the machine.
"""

from __future__ import annotations

import gc
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import run_until  # noqa: F401  (sets the test environment first)

from src.core import voice  # noqa: E402

from gi.repository import Gst  # noqa: E402  (after voice, which picks the version)

voice._ensure_gst()


class FakeDevice:
    """Stands in for a ``Gst.Device`` from a PulseAudio or PipeWire device provider."""

    def __init__(self, name: str, device_class: str = "Audio/Source", props: str = "",
                 internal: str | None = None, provider: str = "GstPulseDevice",
                 factory: str = "audiotestsrc"):
        self.name = name
        self.device_class = device_class
        self.props = Gst.Structure.new_from_string(f"props{', ' + props if props else ''}")
        self.internal = internal
        self.__gtype__ = SimpleNamespace(name=provider)
        self.factory = factory
        self.created: list[str | None] = []

    def has_classes(self, classes: str) -> bool:
        return all(part in self.device_class.split("/") for part in classes.split("/"))

    def get_properties(self):
        return self.props

    def get_display_name(self) -> str:
        return self.name

    def get_property(self, name: str):
        if name == "internal-name" and self.internal is not None:
            return self.internal
        raise TypeError(f"no property {name}")

    def create_element(self, name):
        self.created.append(name)
        return Gst.ElementFactory.make(self.factory, name)


def node(name: str) -> str:
    return f"node.name=(string){name}"


class FakeMonitor:
    """Stands in for ``Gst.DeviceMonitor``, with the given devices plugged in."""

    def __init__(self, devices):
        self.devices = list(devices)
        self.started = False

    def add_filter(self, *_args) -> int:
        return 1

    def start(self) -> bool:
        self.started = True
        return True

    def get_devices(self):
        return list(self.devices)

    def stop(self) -> None:
        self.started = False


class DeviceListTest(unittest.TestCase):
    """The real device list: whatever this machine has, possibly nothing."""

    def setUp(self):
        voice._forget_scan()

    def check(self, devices):
        self.assertIsInstance(devices, list)
        ids = [d.id for d in devices]
        self.assertEqual(len(ids), len(set(ids)), "one entry per device")
        for device in devices:
            self.assertIsInstance(device, voice.AudioDevice)
            self.assertTrue(device.id.strip() and device.name.strip())
        names = [d.name.casefold() for d in devices]
        self.assertEqual(names, sorted(names))

    def test_lists_microphones_and_speakers_without_raising(self):
        sources = voice.list_devices("source")
        sinks = voice.list_devices("sink")
        self.check(sources)
        self.check(sinks)
        self.assertFalse(any(d.id.endswith(".monitor") for d in sources))
        for found in voice._scan()["source"]:
            self.assertFalse(voice._is_monitor(found.gst, found.gst.get_properties()))
        for kind in ("source", "sink"):
            default = voice.default_device_name(kind)
            self.assertTrue(default is None or isinstance(default, str))

    def test_one_scan_serves_both_kinds(self):
        with mock.patch.object(voice, "_probe_devices", wraps=voice._probe_devices) as probe:
            voice.list_devices("source")
            voice.list_devices("sink")
            voice.list_devices("source")
        self.assertEqual(probe.call_count, 1)

    def test_unknown_kinds_are_a_programming_error(self):
        for call in (voice.list_devices, voice.default_device_name):
            with self.assertRaises(ValueError):
                call("camera")

    def test_listing_in_the_background(self):
        box = {}

        def done(devices):
            box["devices"] = devices
            box["main"] = threading.current_thread() is threading.main_thread()
        voice.list_devices_async("source", done)
        self.assertTrue(run_until(lambda: "devices" in box, 10))
        self.assertTrue(box["main"])
        self.check(box["devices"])
        self.assertEqual(box["devices"], voice.list_devices("source"))

    def test_without_a_sound_server(self):
        with mock.patch.object(voice, "_shared_monitor", None), \
                mock.patch.object(Gst.DeviceMonitor, "start", return_value=False):
            voice._forget_scan()
            self.assertEqual(voice.list_devices("source"), [])
        voice._forget_scan()
        with mock.patch.object(voice, "_shared_monitor", None), \
                mock.patch.object(voice, "_shared_monitor", None), \
                mock.patch.object(Gst.DeviceMonitor, "new", side_effect=RuntimeError("broken")):
            self.assertEqual(voice.list_devices("sink"), [])
        voice._forget_scan()

    def test_watching_for_new_devices(self):
        watcher = voice.DeviceWatcher()
        changes = []
        watcher.connect("changed", lambda _w: changes.append(1))
        run_until(lambda: False, 0.3)
        watcher.stop()
        watcher.stop()
        self.assertIsInstance(changes, list)

    def test_a_watcher_dropped_without_stop_stops(self):
        real_stop = voice.DeviceWatcher.stop
        with mock.patch.object(voice.DeviceWatcher, "stop", autospec=True,
                               side_effect=real_stop) as stop:
            watcher = voice.DeviceWatcher()
            watcher.connect("changed", lambda _w: None)
            del watcher
            gc.collect()
            self.assertGreaterEqual(stop.call_count, 1)

    def test_a_device_change_is_announced_once(self):
        watcher = voice.DeviceWatcher()
        self.addCleanup(watcher.stop)
        changes = []
        watcher.connect("changed", lambda _w: changes.append(1))
        voice.list_devices("source")  # fills the cache
        for _event in range(3):  # a burst, as when a headset plugs in
            watcher._on_message(None, SimpleNamespace(type=Gst.MessageType.DEVICE_ADDED))
        self.assertIsNone(voice._scan_cache, "the next list looks again")
        self.assertTrue(run_until(lambda: changes, 3))
        run_until(lambda: False, 0.5)
        self.assertEqual(changes, [1])


class DeviceSelectionTest(unittest.TestCase):
    """How devices become list entries and pipeline elements, with stand-in devices."""

    def setUp(self):
        voice._forget_scan()
        self.addCleanup(voice._forget_scan)

    def collect(self, devices, kind="source"):
        return [found.device for found in voice._collect(devices, kind)]

    def test_monitors_of_speakers_are_not_microphones(self):
        devices = [
            FakeDevice("Monitor of Speakers", props=node("alsa_output.pci.analog-stereo")
                       + ", device.class=(string)monitor, media.class=(string)Audio/Sink"),
            FakeDevice("Monitor of HDMI", internal="alsa_output.hdmi.monitor"),
            FakeDevice("Built-in Microphone", props=node("alsa_input.pci.analog-stereo")
                       + ", device.class=(string)sound, media.class=(string)Audio/Source"),
        ]
        self.assertEqual(self.collect(devices), [
            voice.AudioDevice("alsa_input.pci.analog-stereo", "Built-in Microphone")])

    def test_ids_are_node_names_when_there_are_any(self):
        devices = [
            FakeDevice("A", props=node("node-a") + ", device.name=(string)card-a",
                       internal="pulse-a"),
            FakeDevice("B", props="device.name=(string)card-b", internal="pulse-b"),
            FakeDevice("C", props="device.name=(string)card-c"),
            FakeDevice("D"),
        ]
        self.assertEqual([d.id for d in self.collect(devices)],
                         ["node-a", "pulse-b", "card-c", "D"])

    def test_the_same_device_from_two_providers_is_listed_once(self):
        for order in (1, -1):
            with self.subTest(order=order):
                pipewire = FakeDevice("USB Headset (PipeWire)", props=node("usb-headset"),
                                      provider="GstPipeWireDevice")
                pulse = FakeDevice("USB Headset", props=node("usb-headset"))
                found = voice._collect([pipewire, pulse][::order], "source")
                self.assertEqual(len(found), 1)
                self.assertIs(found[0].gst, pulse)

    def test_raw_alsa_devices_only_without_a_sound_server(self):
        alsa = FakeDevice("HD-Audio Generic at 0xf7400000 irq 81", internal="hw:1,0",
                          provider="GstAlsaDevice", factory="alsasrc")
        pulse = FakeDevice("Built-in Audio Analog Stereo", props=node("alsa_input.analog"))
        pipewire = FakeDevice("Built-in Audio Analog Stereo", props=node("alsa_input.analog"),
                              provider="GstPipeWireDevice")
        self.assertEqual([d.id for d in self.collect([alsa, pulse])], ["alsa_input.analog"])
        self.assertEqual([d.id for d in self.collect([alsa, pipewire])], ["alsa_input.analog"])
        self.assertEqual([d.id for d in self.collect([alsa])], ["hw:1,0"])

    def test_names_are_sorted_and_told_apart(self):
        devices = [FakeDevice("webcam", props=node("cam")),
                   FakeDevice("USB Headset", props=node("usb-1")),
                   FakeDevice("Analog  Input\n", props=node("analog")),
                   FakeDevice("USB Headset", props=node("usb-2"))]
        self.assertEqual([(d.id, d.name) for d in self.collect(devices)], [
            ("analog", "Analog Input"), ("usb-1", "USB Headset"),
            ("usb-2", "USB Headset (2)"), ("cam", "webcam")])

    def test_kinds_and_the_default_device(self):
        devices = [FakeDevice("Speakers", "Audio/Sink",
                              node("speakers") + ", is-default=(boolean)true"),
                   FakeDevice("HDMI", "Audio/Sink", node("hdmi")),
                   FakeDevice("Microphone", "Audio/Source", node("mic"))]
        with mock.patch.object(voice, "_probe_devices", return_value={
                kind: voice._collect(devices, kind) for kind in ("source", "sink")}):
            self.assertEqual([d.id for d in voice.list_devices("sink")], ["hdmi", "speakers"])
            self.assertEqual([d.id for d in voice.list_devices("source")], ["mic"])
            self.assertEqual(voice.default_device_name("sink"), "Speakers")
            self.assertIsNone(voice.default_device_name("source"))

    def test_the_chosen_device_or_the_system_default(self):
        headset = FakeDevice("USB Headset", props=node("usb-headset"))
        speakers = FakeDevice("Speakers", "Audio/Sink", node("speakers"), factory="fakesink")
        found = {"source": voice._collect([headset], "source"),
                 "sink": voice._collect([speakers], "sink")}
        with mock.patch.object(voice, "_TEST_AUDIO", False), \
                mock.patch.object(voice, "_probe_devices", return_value=found):
            element, name = voice.audio_element("source", "usb-headset", "microphone")
            self.assertEqual(element.get_factory().get_name(), "audiotestsrc")
            self.assertEqual((element.get_name(), name), ("microphone", "USB Headset"))
            self.assertEqual(headset.created, ["microphone"])
            element, name = voice.audio_element("sink", "speakers")
            self.assertEqual((element.get_factory().get_name(), name), ("fakesink", "Speakers"))

            for device_id in ("", "unplugged-headset"):
                with self.subTest(device_id=device_id):
                    element, name = voice.audio_element("source", device_id)
                    self.assertEqual((element.get_factory().get_name(), name),
                                     ("autoaudiosrc", ""))
                    element, name = voice.audio_element("sink", device_id)
                    self.assertEqual((element.get_factory().get_name(), name),
                                     ("autoaudiosink", ""))
            # A device that cannot make its element falls back too.
            with mock.patch.object(headset, "create_element", side_effect=RuntimeError), \
                    self.assertLogs("src.core.voice", "WARNING"):
                element, name = voice.audio_element("source", "usb-headset")
                self.assertEqual((element.get_factory().get_name(), name), ("autoaudiosrc", ""))

    def test_a_device_plugged_in_a_moment_ago_is_found(self):
        headset = FakeDevice("USB Headset", props=node("usb-headset"))
        scans = [{"source": [], "sink": []},
                 {"source": voice._collect([headset], "source"), "sink": []}]
        with mock.patch.object(voice, "_TEST_AUDIO", False), \
                mock.patch.object(voice, "_probe_devices", side_effect=scans):
            voice.list_devices("source")  # the settings listed devices before the plug
            element, name = voice.audio_element("source", "usb-headset")
        self.assertEqual(name, "USB Headset")

    def test_system_default_never_records_the_speakers(self):
        # The owner's machine: PulseAudio's default "microphone" is the monitor of the
        # digital output, so a call would send the Dot its own voice back.
        monitor = FakeDevice(
            "Monitor of Digital Output", internal="alsa_output.iec958-stereo.monitor",
            props=node("alsa_output.iec958-stereo") + ", device.class=(string)monitor, "
            "media.class=(string)Audio/Sink, is-default=(boolean)true")
        mic = FakeDevice("Analog Input", props=node("alsa_input.analog-stereo")
                         + ", device.class=(string)sound, media.class=(string)Audio/Source")
        speakers = FakeDevice("Digital Output", "Audio/Sink", factory="fakesink",
                              props=node("alsa_output.iec958-stereo")
                              + ", is-default=(boolean)true")
        with mock.patch.object(voice, "_TEST_AUDIO", False), \
                mock.patch.object(voice, "_shared_monitor", None), \
                mock.patch.object(Gst.DeviceMonitor, "new",
                                  return_value=FakeMonitor([monitor, mic, speakers])):
            self.assertEqual(voice.list_devices("source"),
                             [voice.AudioDevice("alsa_input.analog-stereo", "Analog Input")])
            self.assertTrue(voice._scan().default_is_monitor)
            self.assertEqual(voice.default_device_name("source"), "Analog Input")
            self.assertEqual(voice.default_device_name("sink"), "Digital Output")
            with self.assertLogs("src.core.voice", "INFO"):
                element, name = voice.audio_element("source", "", "microphone")
            self.assertEqual((element.get_factory().get_name(), name),
                             ("audiotestsrc", "Analog Input"))
            self.assertEqual((mic.created, monitor.created), (["microphone"], []))
            # A chosen microphone that is gone falls back to the real one too.
            self.assertEqual(voice.audio_element("source", "unplugged-headset")[1],
                             "Analog Input")
            # Speakers keep the system's choice.
            element, name = voice.audio_element("sink", "")
            self.assertEqual((element.get_factory().get_name(), name), ("autoaudiosink", ""))
            # The settings' meter measures what a call would record.
            meter = voice.MicTest()
            self.addCleanup(meter.stop)
            levels = []
            meter.start(levels.append)
            self.assertTrue(run_until(lambda: levels, 5))
            self.assertEqual(meter.device_name, "Analog Input")
            meter.stop()

    def test_without_a_real_microphone_the_sound_server_decides(self):
        def monitor(default: bool) -> FakeDevice:
            return FakeDevice("Monitor of Speakers", internal="speakers.monitor",
                              props=node("speakers") + ", device.class=(string)monitor, "
                              f"is-default=(boolean){str(default).lower()}")
        mic = FakeDevice("Headset", props=node("headset") + ", is-default=(boolean)true")
        # Only a monitor: nothing better to use. A real default microphone: all is well.
        for devices, default in (([monitor(True)], None), ([monitor(False), mic], "Headset")):
            with self.subTest(default=default), \
                    mock.patch.object(voice, "_TEST_AUDIO", False), \
                    mock.patch.object(voice, "_shared_monitor", None), \
                    mock.patch.object(Gst.DeviceMonitor, "new",
                                      return_value=FakeMonitor(devices)):
                voice._forget_scan()
                self.assertEqual(voice.default_device_name("source"), default)
                element, name = voice.audio_element("source", "")
                self.assertEqual((element.get_factory().get_name(), name), ("autoaudiosrc", ""))

    def test_test_audio_never_touches_devices(self):
        with mock.patch.object(voice, "_probe_devices") as probe:
            element, name = voice.audio_element("source", "anything")
            self.assertEqual(name, "")
            sink, _name = voice.audio_element("sink", "anything")
        probe.assert_not_called()
        self.assertEqual(sink.get_factory().get_name(), "fakesink")
        self.assertIsNotNone(element.get_static_pad("src"))


class LevelTest(unittest.TestCase):
    def test_decibels_to_meter(self):
        cases = [(-120.0, 0.0), (-60.0, 0.0), (-45.0, 0.25), (-30.0, 0.5), (-15.0, 0.75),
                 (0.0, 1.0), (6.0, 1.0), (float("-inf"), 0.0), (float("nan"), 0.0), (None, 0.0)]
        for db, expected in cases:
            with self.subTest(db=db):
                self.assertAlmostEqual(voice.level_from_db(db), expected)

    def test_level_messages(self):
        structure = Gst.Structure.new_from_string(
            "level, peak=(double)<-6.0, -12.0>, decay=(double)<-30.0, -15.0>, "
            "rms=(double)<-20.0, -40.0>")
        self.assertAlmostEqual(voice.level_from_structure(structure), 0.75)  # loudest decay
        structure = Gst.Structure.new_from_string("level, rms=(double)<-30.0>")
        self.assertAlmostEqual(voice.level_from_structure(structure), 0.5)
        structure = Gst.Structure.new_from_string("level, peak=(double)<-inf, -inf>")
        self.assertEqual(voice.level_from_structure(structure), 0.0)
        self.assertEqual(voice.level_from_structure(Gst.Structure.new_empty("level")), 0.0)


class MicTestTest(unittest.TestCase):
    def setUp(self):
        self.levels: list[float] = []
        self.errors: list[str] = []
        self.threads: set[bool] = set()

    def on_level(self, level: float) -> None:
        self.levels.append(level)
        self.threads.add(threading.current_thread() is threading.main_thread())

    def measure(self, mic: voice.MicTest, seconds: float = 0.5) -> list[float]:
        start = len(self.levels)
        run_until(lambda: False, seconds)
        return self.levels[start:]

    def test_reports_the_test_tone_about_twenty_times_a_second(self):
        mic = voice.MicTest()
        self.addCleanup(mic.stop)
        started = time.monotonic()
        mic.start(self.on_level, self.errors.append)
        self.assertTrue(mic.running)
        self.assertTrue(run_until(lambda: len(self.levels) >= 10, 5))
        rate = len(self.levels) / (time.monotonic() - started)
        self.assertGreater(rate, 8)
        self.assertLess(rate, 40)
        self.assertTrue(all(0.0 <= level <= 1.0 for level in self.levels))
        self.assertGreater(min(self.levels[2:]), 0.6)  # a sine at volume 0.2 is about -14 dB
        self.assertEqual(self.threads, {True})
        self.assertEqual(self.errors, [])
        self.assertIsNone(mic.error)

    def test_louder_sounds_read_higher(self):
        averages = []
        for volume in (0.003, 0.03, 0.3):
            mic = voice.MicTest(source=f"audiotestsrc is-live=true wave=sine volume={volume}")
            self.addCleanup(mic.stop)
            mic.start(self.on_level)
            self.assertTrue(run_until(lambda: len(self.levels) >= 3, 5))
            levels = self.measure(mic, 0.3)
            mic.stop()
            self.levels.clear()
            averages.append(sum(levels) / len(levels))
        self.assertEqual(averages, sorted(averages))
        self.assertGreater(averages[0], 0.05)
        self.assertGreater(averages[1] - averages[0], 0.2)
        self.assertGreater(averages[2] - averages[1], 0.2)

    def test_the_meter_follows_a_rising_voice(self):
        mic = voice.MicTest(source="audiotestsrc name=tone is-live=true wave=sine volume=0.002")
        self.addCleanup(mic.stop)
        mic.start(self.on_level)
        tone = mic.pipeline.get_by_name("tone")
        readings = []
        for volume in (0.002, 0.02, 0.2, 0.9):
            tone.set_property("volume", volume)
            self.measure(mic, 0.15)  # let the change through
            readings.append(self.measure(mic, 0.2)[-1])
        self.assertEqual(readings, sorted(readings), readings)
        self.assertLess(readings[0], 0.15)
        self.assertGreater(readings[-1], 0.9)

    def test_start_and_stop_as_often_as_needed(self):
        mic = voice.MicTest("no-such-microphone")
        self.addCleanup(mic.stop)
        mic.stop()  # before any start
        for _round in range(3):
            mic.start(self.on_level)
            mic.start(self.on_level)  # a second start restarts
            self.assertTrue(run_until(lambda: len(self.levels) >= 2, 5))
            mic.stop()
            mic.stop()
            self.assertFalse(mic.running)
            self.assertIsNone(mic.pipeline)
            count = len(self.levels)
            run_until(lambda: False, 0.2)
            self.assertEqual(len(self.levels), count, "no levels after stop()")
            self.levels.clear()

    def test_a_meter_dropped_without_stop_releases_the_microphone(self):
        mic = voice.MicTest()
        mic.start(self.on_level)
        self.assertTrue(run_until(lambda: self.levels, 5))
        pipeline = mic.pipeline
        del mic
        gc.collect()
        self.assertEqual(pipeline.get_state(0)[1], Gst.State.NULL)
        count = len(self.levels)
        run_until(lambda: False, 0.2)
        self.assertEqual(len(self.levels), count)

    def test_a_missing_microphone_reads_zero(self):
        for source in ("audiotestsrc ! no-such-element-dotdeck",
                       "filesrc location=/nonexistent/dotdeck.wav ! wavparse"):
            with self.subTest(source=source):
                self.levels.clear()
                self.errors.clear()
                mic = voice.MicTest(source=source)
                self.addCleanup(mic.stop)
                mic.start(self.on_level, self.errors.append)  # must not raise
                self.assertTrue(run_until(lambda: self.errors, 5))
                run_until(lambda: False, 0.1)
                self.assertEqual(self.levels, [0.0])
                self.assertEqual(len(self.errors), 1)
                self.assertTrue(mic.error)
                self.assertFalse(mic.running)
                mic.stop()

    def test_a_failure_reported_after_stop_is_dropped(self):
        mic = voice.MicTest(source="audiotestsrc ! no-such-element-dotdeck")
        mic.start(self.on_level, self.errors.append)
        mic.stop()
        run_until(lambda: False, 0.2)
        self.assertEqual((self.levels, self.errors), ([], []))

    def test_works_without_the_level_element_reporting_why(self):
        real_find = Gst.ElementFactory.find
        with mock.patch.object(Gst.ElementFactory, "find",
                               side_effect=lambda n: None if n == "level" else real_find(n)):
            mic = voice.MicTest()
            mic.start(self.on_level, self.errors.append)
        self.assertTrue(run_until(lambda: self.errors, 5))
        self.assertEqual(self.levels, [0.0])
        self.assertIn("level", mic.error)


class SpeakerTestTest(unittest.TestCase):
    def test_plays_the_chime_to_the_end(self):
        box = []
        speaker = voice.SpeakerTest("no-such-speaker")
        self.addCleanup(speaker.stop)
        speaker.play(box.append)
        self.assertTrue(speaker.playing)
        self.assertTrue(run_until(lambda: box, 10))
        self.assertEqual(box, [True])
        self.assertFalse(speaker.playing)
        speaker.play(box.append)  # again
        self.assertTrue(run_until(lambda: len(box) == 2, 10))

    def test_stop_cuts_it_short_quietly(self):
        box = []
        speaker = voice.SpeakerTest()
        speaker.play(box.append)
        speaker.stop()
        speaker.stop()
        run_until(lambda: False, 0.3)
        self.assertEqual(box, [])
        self.assertFalse(speaker.playing)

    def test_the_chime(self):
        pcm = voice._chime()
        self.assertEqual(len(pcm), int(48000 * 0.9) * 2)
        samples = memoryview(pcm).cast("h")
        self.assertLess(max(abs(s) for s in samples), 32767 * 0.6)  # soft, never clipping
        self.assertGreater(max(abs(s) for s in samples), 32767 * 0.1)
        self.assertLess(abs(samples[-1]), 500)  # fades out


if __name__ == "__main__":
    unittest.main()
