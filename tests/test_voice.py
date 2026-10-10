"""The native WebRTC bridge, end to end against a local GStreamer peer.

The remote peer behaves like the OpenAI Realtime endpoint: it answers an offer that
carries no ICE candidates with an answer that carries all of its own, and talks over
a data channel created by the caller.

The call phases and captions are checked twice: on their own, by feeding sample events
(in the shape the OpenDots gateway and OpenAI Realtime send) to the pure parser and the
tracker, and through a real loopback call.
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from tests.helpers import run_until  # noqa: F401  (sets the test environment first)

from src.core import voice  # noqa: E402

if voice.available():
    from gi.repository import Gst, GstWebRTC  # noqa: E402


def event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, "event_id": "evt_1", **fields})


class CallEventParserTest(unittest.TestCase):
    """``parse_call_event``: one provider event in, what it means for the call out."""

    def parse(self, data: str, outgoing: bool = False):
        return voice.parse_call_event(data, outgoing)

    def test_turn_events_map_to_phases(self):
        cases = {
            "input_audio_buffer.speech_started": "listening",
            "input_audio_buffer.speech_stopped": "thinking",
            "response.created": "thinking",
            "output_audio_buffer.started": "speaking",
            "output_audio_buffer.stopped": "listening",
            "output_audio_buffer.cleared": "listening",
        }
        for kind, phase in cases.items():
            with self.subTest(kind=kind):
                parsed = self.parse(event(kind, item_id="item_1"))
                self.assertEqual(parsed.type, kind)
                self.assertEqual(parsed.phase, phase)
                self.assertIsNone(parsed.role)
                self.assertFalse(parsed.error)

    def test_the_dots_words_arrive_in_pieces_then_whole(self):
        delta = self.parse(event("response.output_audio_transcript.delta", delta="Oi! "))
        self.assertEqual((delta.phase, delta.role, delta.text, delta.final),
                         ("speaking", "dot", "Oi! ", False))
        done = self.parse(event("response.output_audio_transcript.done",
                                transcript=" Oi! Meu nome é Dot. "))
        self.assertEqual((done.phase, done.role, done.text, done.final),
                         (None, "dot", "Oi! Meu nome é Dot.", True))
        # The OpenAI beta names of the same events.
        self.assertEqual(self.parse(event("response.audio_transcript.delta", delta="a")).role,
                         "dot")
        self.assertTrue(self.parse(event("response.audio_transcript.done", transcript="a")).final)

    def test_what_the_caller_said(self):
        done = self.parse(event("conversation.item.input_audio_transcription.completed",
                                item_id="item_2", content_index=0,
                                transcript="Olá! Qual é o seu nome?"))
        self.assertEqual((done.phase, done.role, done.text, done.final),
                         (None, "you", "Olá! Qual é o seu nome?", True))
        piece = self.parse(event("conversation.item.input_audio_transcription.delta",
                                 delta="Olá"))
        self.assertEqual((piece.phase, piece.role, piece.text, piece.final),
                         (None, "you", "Olá", False))

    def test_ask_compute_means_thinking(self):
        parsed = self.parse(event("response.function_call_arguments.done", call_id="call_1",
                                  name="ask_compute", arguments='{"request":"weather"}'))
        self.assertEqual((parsed.phase, parsed.compute), ("thinking", True))
        self.assertIsNone(self.parse(event("response.function_call_arguments.done",
                                           name="other_tool", arguments="{}")))

    def test_errors_carry_the_providers_message(self):
        parsed = self.parse(event("error", error={"type": "provider_error",
                                                  "message": "OpenRouter returned HTTP 402:\n"
                                                             "  insufficient credits"}))
        self.assertTrue(parsed.error)
        self.assertEqual(parsed.text, "OpenRouter returned HTTP 402: insufficient credits")
        self.assertEqual(self.parse(event("error", error={"type": "x"})).text, "")
        self.assertEqual(self.parse(event("error", message="plain")).text, "plain")
        self.assertLessEqual(len(self.parse(event("error", error={"message": "x" * 5000})).text),
                             300)
        failed = self.parse(event("response.done", response={
            "status": "failed", "status_details": {"type": "failed", "error": {
                "type": "insufficient_quota", "message": "You exceeded your quota."}}}))
        self.assertTrue(failed.error)
        self.assertEqual(failed.text, "You exceeded your quota.")
        completed = self.parse(event("response.done", response={"status": "completed",
                                                                 "usage": {"cost": 0.0005}}))
        self.assertEqual((completed.type, completed.phase, completed.error),
                         ("response.done", None, False))

    def test_harmless_provider_errors_are_not_call_errors(self):
        for code in ("response_cancel_not_active", "conversation_already_has_active_response",
                     "input_audio_buffer_commit_empty"):
            with self.subTest(code=code):
                self.assertIsNone(self.parse(event("error", error={
                    "type": "invalid_request_error", "code": code, "message": "Too late."})))
        real = self.parse(event("error", error={"type": "server_error", "code": "x",
                                                "message": "Model unavailable."}))
        self.assertEqual((real.error, real.text), (True, "Model unavailable."))

    def test_what_the_page_sends(self):
        self.assertEqual(self.parse(event("response.create"), outgoing=True).phase, "thinking")
        self.assertEqual(self.parse(event("response.cancel"), outgoing=True).phase, "listening")
        output = json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": "Sunny."}})
        self.assertIsNone(self.parse(output, outgoing=True))
        # Directions do not mix.
        self.assertIsNone(self.parse(event("response.create")))
        self.assertIsNone(self.parse(event("response.created"), outgoing=True))

    def test_everything_else_is_ignored(self):
        ignored = [
            event("session.created", session={"type": "realtime", "model": "m"}),
            json.dumps({"type": "conversation.item.created", "item": {"type": "error"}}),
            json.dumps({"event_id": "e", "item": {"type": "message"},
                        "type": "response.output_item.added"}),
            event("rate_limits.updated", rate_limits=[]),
            "not json at all, but it says \"type\":\"error\"",
            json.dumps(["type", "error"]),
            "",
            '{"type": "error"' + " " * (300 * 1024) + "}",
        ]
        for data in ignored:
            with self.subTest(data=data[:60]):
                self.assertIsNone(self.parse(data))
        self.assertIsNone(voice.parse_call_event(None))  # type: ignore[arg-type]

    def test_quoted_text_does_not_fool_the_parser(self):
        tricky = event("response.output_audio_transcript.delta",
                       delta='she said {"type":"error","message":"boom"}')
        parsed = self.parse(tricky)
        self.assertFalse(parsed.error)
        self.assertEqual(parsed.text, 'she said {"type":"error","message":"boom"}')

    def test_key_order_does_not_matter(self):
        data = '{"event_id":"evt_9","response":{"id":"r"},"type":"response.created"}'
        self.assertEqual(self.parse(data).phase, "thinking")


class CallTrackerTest(unittest.TestCase):
    """``CallTracker``: whole calls, as the window will see them."""

    def setUp(self):
        self.tracker = voice.CallTracker()

    def feed(self, *events: str, outgoing: bool = False) -> list[tuple]:
        updates = []
        for data in events:
            updates += self.tracker.feed(data, outgoing)
        return updates

    def test_a_turn_with_the_opendots_gateway(self):
        # The order the real call on the owner's server produced.
        self.assertEqual(self.tracker.start(), [("call-phase", "connecting")])
        self.assertEqual(self.tracker.connection("connecting"), [])
        self.assertEqual(self.tracker.connection("connected"), [("call-phase", "listening")])
        self.assertEqual(self.feed(event("session.created", session={"model": "m"})), [])
        self.assertEqual(self.feed(event("input_audio_buffer.speech_started")), [])
        self.assertEqual(self.feed(event("input_audio_buffer.speech_stopped")),
                         [("call-phase", "thinking")])
        self.assertEqual(self.feed(event("response.created")), [])
        self.assertEqual(self.feed(event("output_audio_buffer.started")),
                         [("call-phase", "speaking")])
        self.assertEqual(
            self.feed(event("conversation.item.input_audio_transcription.completed",
                            transcript="Olá! Qual é o seu nome?")),
            [("caption", "you", "Olá! Qual é o seu nome?", True)])
        self.assertEqual(
            self.feed(event("response.output_audio_transcript.done",
                            transcript="Oi! Meu nome é Dot.")),
            [("caption", "dot", "Oi! Meu nome é Dot.", True)])
        self.assertEqual(self.feed(event("response.done", response={"status": "completed"})),
                         [])
        self.assertEqual(self.feed(event("output_audio_buffer.stopped")),
                         [("call-phase", "listening")])
        self.assertEqual(self.tracker.end(), [("call-phase", "ended")])
        self.assertEqual(self.tracker.end(), [])
        self.assertEqual(self.feed(event("input_audio_buffer.speech_started")), [])
        self.assertEqual(self.tracker.connection("connected"), [])

    def test_captions_grow_piece_by_piece(self):
        self.tracker.start()
        self.tracker.connection("connected")
        updates = self.feed(event("response.created"),
                            event("response.output_audio_transcript.delta", delta="Oi!"),
                            event("response.output_audio_transcript.delta", delta=" Meu nome"),
                            event("response.output_audio_transcript.delta", delta=" é Dot."))
        self.assertEqual(updates, [
            ("call-phase", "thinking"),
            ("call-phase", "speaking"),
            ("caption", "dot", "Oi!", False),
            ("caption", "dot", "Oi! Meu nome", False),
            ("caption", "dot", "Oi! Meu nome é Dot.", False),
        ])
        # A done without its own text completes the pieces.
        self.assertEqual(self.feed(event("response.output_audio_transcript.done")),
                         [("caption", "dot", "Oi! Meu nome é Dot.", True)])
        # The next reply starts a new line, even if the previous one never finished.
        self.feed(event("response.created"),
                  event("response.output_audio_transcript.delta", delta="Lost"),
                  event("input_audio_buffer.speech_started"))
        updates = self.feed(event("response.created"),
                            event("response.output_audio_transcript.delta", delta="New"))
        self.assertIn(("caption", "dot", "New", False), updates)

    def test_the_callers_words_in_pieces(self):
        self.tracker.start()
        updates = self.feed(
            event("conversation.item.input_audio_transcription.delta", delta="Qual"),
            event("conversation.item.input_audio_transcription.delta", delta=" é"),
            event("conversation.item.input_audio_transcription.completed",
                  transcript="Qual é o seu nome?"))
        self.assertEqual([u for u in updates if u[0] == "caption"], [
            ("caption", "you", "Qual", False),
            ("caption", "you", "Qual é", False),
            ("caption", "you", "Qual é o seu nome?", True),
        ])
        self.assertEqual(self.feed(event("conversation.item.input_audio_transcription.completed",
                                         transcript="   ")), [])

    def test_compute_keeps_thinking_until_the_answer_speaks(self):
        self.tracker.start()
        self.tracker.connection("connected")
        self.feed(event("input_audio_buffer.speech_stopped"), event("response.created"))
        updates = self.feed(
            event("output_audio_buffer.started"),
            event("response.output_audio_transcript.done", transcript="Vou verificar."),
            event("response.function_call_arguments.done", name="ask_compute",
                  call_id="call_1", arguments="{}"),
            event("response.done", response={"status": "completed"}))
        # Still saying "let me check": the research shows once the Dot goes quiet.
        self.assertEqual([u for u in updates if u[0] == "call-phase"],
                         [("call-phase", "speaking")])
        self.assertEqual(self.tracker.phase, "speaking")
        self.assertEqual(self.feed(event("output_audio_buffer.stopped")),
                         [("call-phase", "thinking")])
        self.assertTrue(self.tracker.computing)
        self.assertEqual(self.tracker.stale(), [])  # long research is not "stuck"
        # The page sends the result back and asks for the spoken answer.
        self.assertEqual(self.feed(json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": "Done."}}),
            event("response.create"), outgoing=True), [])
        self.assertFalse(self.tracker.computing)
        self.assertEqual(self.feed(event("response.created"),
                                   event("output_audio_buffer.started")),
                         [("call-phase", "speaking")])

    def test_compute_without_words_thinks_at_once(self):
        self.tracker.start()
        self.tracker.connection("connected")
        self.feed(event("output_audio_buffer.started"), event("output_audio_buffer.stopped"))
        self.feed(event("input_audio_buffer.speech_stopped"), event("response.created"))
        self.assertEqual(self.tracker.phase, "thinking")
        self.assertEqual(self.feed(event("response.function_call_arguments.done",
                                         name="ask_compute", call_id="c", arguments="{}"),
                                   event("response.done", response={"status": "completed"})),
                         [])
        self.assertEqual((self.tracker.phase, self.tracker.computing), ("thinking", True))

    def test_a_turn_without_words_goes_back_to_listening(self):
        self.tracker.start()
        self.tracker.connection("connected")
        self.feed(event("input_audio_buffer.speech_stopped"), event("response.created"))
        self.assertEqual(self.feed(event("response.done", response={"status": "completed"})),
                         [("call-phase", "listening")])
        self.feed(event("input_audio_buffer.speech_stopped"))
        self.assertEqual(self.tracker.stale(), [("call-phase", "listening")])

    def test_providers_without_audio_buffer_events(self):
        self.tracker.start()
        self.tracker.connection("connected")
        updates = self.feed(event("response.created"),
                            event("response.output_audio_transcript.delta", delta="Hi"),
                            event("response.done", response={"status": "completed"}))
        self.assertEqual([u for u in updates if u[0] == "call-phase"][-1],
                         ("call-phase", "listening"))

    def test_errors(self):
        self.tracker.start()
        self.tracker.connection("connected")
        self.feed(event("input_audio_buffer.speech_stopped"))
        updates = self.feed(event("error", error={"message": "HTTP 402"}))
        self.assertEqual(updates, [("call-phase", "listening"), ("call-error", "HTTP 402")])
        updates = self.feed(event("error", error={}))
        self.assertEqual(updates, [("call-error", "The voice provider reported an error.")])
        updates = self.feed(event("response.done", response={
            "status": "failed", "status_details": {"error": {"message": "Quota."}}}))
        self.assertEqual(updates, [("call-error", "Quota.")])

    def test_connection_states(self):
        self.assertEqual(self.tracker.start(), [("call-phase", "connecting")])
        self.assertEqual(self.tracker.start(), [])
        self.assertEqual(self.tracker.connection("new"), [])
        self.assertEqual(self.tracker.connection("connected"), [("call-phase", "listening")])
        self.assertEqual(self.tracker.connection("disconnected"), [("call-phase", "connecting")])
        self.assertEqual(self.tracker.connection("connected"), [("call-phase", "listening")])
        # A failed connection does not come back: the call is over.
        self.assertEqual(self.tracker.connection("failed"),
                         [("call-error", "The voice connection dropped."),
                          ("call-phase", "ended")])
        self.assertTrue(self.tracker.ended)
        self.assertEqual(self.tracker.connection("closed"), [])
        self.assertEqual(self.tracker.error("x"), [])

    def test_a_connection_closed_by_the_page(self):
        self.tracker.start()
        self.tracker.connection("connected")
        self.assertEqual(self.tracker.connection("closed"), [("call-phase", "ended")])
        self.assertEqual(self.tracker.connection("failed"), [])

    def test_hanging_up_is_not_a_dropped_connection(self):
        # OpenDots hangs up the provider first and closes the page's side only after the
        # call is saved (~40 s): the connection fails meanwhile, which is no error.
        self.tracker.start()
        self.tracker.connection("connected")
        self.assertEqual(self.tracker.connection("disconnected", hanging_up=True), [])
        self.assertEqual(self.tracker.phase, "listening")
        self.assertEqual(self.tracker.connection("failed", hanging_up=True),
                         [("call-phase", "ended")])
        self.assertEqual(self.tracker.connection("closed"), [])


class FakeHost:
    """A window that knows the 0.3 voice options."""

    def __init__(self, **options):
        self.options = options
        self.reads = 0

    def voice_options(self):
        self.reads += 1
        return dict(self.options)

    def echo_cancellation(self):
        raise AssertionError("hosts with voice_options are not asked about echo")


class OldHost:
    """A window from Dotdeck 0.2: one echo cancellation switch."""

    def __init__(self, echo: bool):
        self.echo = echo

    def echo_cancellation(self):
        return self.echo


class CallOptionsTest(unittest.TestCase):
    def bridge(self, echo=lambda: True, **kwargs):
        return voice.VoiceBridge(lambda pc, e: None, lambda cb: cb(True), echo, **kwargs)

    def test_options_are_typed_and_complete(self):
        options = voice.call_options({"mic-mode": "walkie-talkie", "join-muted": 1,
                                      "microphone-device": None, "speaker-device": " out ",
                                      "surprise": True})
        self.assertEqual(set(options), set(voice.DEFAULT_OPTIONS))
        self.assertEqual(options["mic-mode"], "open")
        self.assertIs(options["join-muted"], True)
        self.assertEqual(options["microphone-device"], "")
        self.assertEqual(options["speaker-device"], "out")
        self.assertEqual(voice.call_options(None), voice.DEFAULT_OPTIONS)

    def test_a_host_with_voice_options(self):
        host = FakeHost(**{"mic-mode": "push-to-talk", "noise-suppression": False})
        bridge = self.bridge(host.echo_cancellation)  # how the web view passes it today
        options = bridge.current_options()
        self.assertEqual(options["mic-mode"], "push-to-talk")
        self.assertFalse(options["noise-suppression"])
        self.assertTrue(options["echo-cancellation"])
        self.assertEqual(host.reads, 1)
        explicit = self.bridge(lambda: False, voice_options=lambda: {"join-muted": True})
        self.assertTrue(explicit.current_options()["join-muted"])

    def test_old_hosts_keep_their_all_or_nothing_switch(self):
        for echo in (True, False):
            with self.subTest(echo=echo):
                options = self.bridge(OldHost(echo).echo_cancellation).current_options()
                self.assertEqual((options["echo-cancellation"], options["noise-suppression"],
                                  options["gain-control"]), (echo, echo, echo))
                self.assertEqual(options["mic-mode"], "open")
                self.assertFalse(options["join-muted"])

    def test_a_broken_host_falls_back_to_its_echo_switch(self):
        def broken():
            raise RuntimeError("settings unavailable")
        with self.assertLogs("src.core.voice", "ERROR"):
            options = self.bridge(lambda: False, voice_options=broken).current_options()
        self.assertFalse(options["echo-cancellation"])
        options = self.bridge(lambda: True, voice_options=lambda: "nonsense").current_options()
        self.assertTrue(options["echo-cancellation"])

    def test_devices_are_looked_at_while_the_microphone_is_asked_for(self):
        # Chosen devices are found, and "System default" is checked (see the device
        # tests), on a worker thread while the person answers.
        for device in ("usb-headset", ""):
            with self.subTest(device=device):
                bridge = self.bridge(FakeHost(**{"speaker-device": device}).echo_cancellation)
                scanned = threading.Event()
                box = {}
                with mock.patch.object(voice, "_TEST_AUDIO", False), \
                        mock.patch.object(voice, "_scan", side_effect=lambda *a: scanned.set()):
                    bridge.handle({"op": "requestMicrophone"}, lambda r, e: box.update(result=r))
                    self.assertTrue(scanned.wait(2))
                self.assertEqual(box["result"], {"allowed": True})
        # Tests and CI never look at the real devices.
        bridge = self.bridge()
        with mock.patch.object(voice, "_scan") as scan:
            bridge.handle({"op": "requestMicrophone"}, lambda r, e: None)
            run_until(lambda: False, 0.1)
        scan.assert_not_called()

    def test_who_starts_muted(self):
        self.assertFalse(voice.starts_muted(voice.call_options()))
        self.assertTrue(voice.starts_muted(voice.call_options({"mic-mode": "push-to-talk"})))
        self.assertTrue(voice.starts_muted(voice.call_options({"join-muted": True})))

    def test_voice_processing_stage(self):
        self.assertEqual(voice._dsp_description("1", False, False, False), "")
        stage = voice._dsp_description("7", True, False, True)
        self.assertIn("webrtcdsp name=dsp-7 probe=echo-7", stage)
        self.assertIn("echo-cancel=true noise-suppression=false gain-control=true", stage)
        self.assertNotIn("probe=", voice._dsp_description("7", False, True, False))


class StubPeer:
    """What the bridge's call bookkeeping uses of a ``PeerConnection``; no WebRTC needed."""

    hanging_up = voice.PeerConnection.hanging_up

    def __init__(self, pc_id: str):
        self.id = pc_id
        self._has_mic = True
        self.mic_enabled = True
        self.speaker_muted = False
        self.page_muted = self.page_paused = False
        self.options = voice.call_options()
        self.closed = False

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = enabled

    def set_speaker_muted(self, muted: bool) -> None:
        self.speaker_muted = muted

    def label(self, dc) -> str:
        return voice.EVENTS_CHANNEL if str(dc) == "1" else ""

    def send(self, dc: str, text: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class BridgeBookkeepingTest(unittest.TestCase):
    """Phases around hanging up, and the "thinking" watchdog, with a stand-in peer."""

    def setUp(self):
        self.signals: list[tuple] = []
        self.bridge = voice.VoiceBridge(lambda pc, e: None, lambda cb: cb(True), lambda: True)
        for name in ("call-phase", "call-error", "calls-changed"):
            self.bridge.connect(name, lambda _b, *a, name=name: self.signals.append((name, *a)))
        self.addCleanup(self.bridge.close_all)
        self.peer = StubPeer("1")
        self.bridge._peers["1"] = self.peer
        tracker = self.bridge._trackers["1"] = voice.CallTracker()
        self.bridge._apply("1", tracker.start())
        self.relay(type="connectionstatechange", state="connected")

    def op(self, **request):
        box = {}
        self.bridge.handle(dict(request, pc="1"), lambda r, e: box.update(result=r, error=e))
        self.assertIsNone(box["error"])

    def relay(self, **event):
        self.bridge._relay("1", event)

    def say(self, data: str):
        self.relay(type="dcmessage", dc="1", data=data)

    def phases(self) -> list[str]:
        return [s[1] for s in self.signals if s[0] == "call-phase"]

    def errors(self) -> list[str]:
        return [s[1] for s in self.signals if s[0] == "call-error"]

    def test_the_page_hanging_up_ends_the_call_at_once(self):
        # useVoice.ts end(): tracks off, audio paused, then ~40 s saving the call.
        self.op(op="setMicEnabled", enabled=False)
        self.assertEqual(self.phases(), ["connecting", "listening"])
        self.op(op="setSpeakerMuted", muted=True, paused=True)
        self.assertEqual(self.phases(), ["connecting", "listening", "ended"])
        self.assertEqual(self.bridge.phase, "")
        self.assertTrue(self.bridge.in_call)  # the page closes its side later
        self.relay(type="connectionstatechange", state="failed")
        self.op(op="close")
        self.assertEqual(self.phases().count("ended"), 1)
        self.assertEqual(self.errors(), [])
        self.assertTrue(self.peer.closed)

    def test_the_server_hanging_up_first_is_no_dropped_connection(self):
        # Without the "paused" hint (muting and pausing look the same), the call ends
        # when the connection gives up, quietly.
        self.op(op="setMicEnabled", enabled=False)
        self.op(op="setSpeakerMuted", muted=True)
        self.assertEqual(self.bridge.phase, "listening")
        self.relay(type="connectionstatechange", state="disconnected")
        self.assertEqual(self.bridge.phase, "listening")
        self.relay(type="connectionstatechange", state="failed")
        self.assertEqual(self.phases()[-1], "ended")
        self.assertEqual(self.errors(), [])

    def test_a_connection_dropped_mid_call_is_an_error(self):
        self.op(op="setSpeakerMuted", muted=True)  # muting the call audio is not hanging up
        self.relay(type="connectionstatechange", state="disconnected")
        self.assertEqual(self.bridge.phase, "connecting")
        self.relay(type="connectionstatechange", state="failed")
        self.assertEqual(self.errors(), ["The voice connection dropped."])
        self.assertEqual(self.phases()[-1], "ended")

    def test_pausing_with_the_microphone_on_is_not_hanging_up(self):
        self.op(op="setSpeakerMuted", muted=True, paused=True)
        self.assertEqual(self.bridge.phase, "listening")
        # The native mute (push to talk) is not the page's: still no hang-up.
        self.bridge.set_mic_enabled(False)
        self.op(op="setSpeakerMuted", muted=True, paused=True)
        self.assertEqual(self.bridge.phase, "listening")

    def test_thinking_without_news_goes_back_to_listening(self):
        with mock.patch.object(voice, "THINKING_TIMEOUT_S", 0.1):
            self.say(event("input_audio_buffer.speech_stopped"))
            self.assertEqual(self.bridge.phase, "thinking")
            self.assertTrue(run_until(lambda: self.bridge.phase == "listening", 3))
        self.assertEqual(self.phases(), ["connecting", "listening", "thinking", "listening"])
        self.assertEqual(self.bridge._watchdogs, {})

    def test_news_keeps_thinking_alive(self):
        with mock.patch.object(voice, "THINKING_TIMEOUT_S", 0.25):
            self.say(event("input_audio_buffer.speech_stopped"))
            for _tick in range(6):
                run_until(lambda: False, 0.1)
                self.say(event("rate_limits.updated", rate_limits=[]))
            self.assertEqual(self.bridge.phase, "thinking")
            self.assertEqual(len(self.bridge._watchdogs), 1, "one watchdog per call")
            self.assertTrue(run_until(lambda: self.bridge.phase == "listening", 3))

    def test_research_is_never_stale_but_a_lost_answer_is(self):
        with mock.patch.object(voice, "THINKING_TIMEOUT_S", 0.05):
            self.say(event("response.function_call_arguments.done", name="ask_compute",
                           call_id="c", arguments="{}"))
            run_until(lambda: False, 0.3)
            self.assertEqual(self.bridge.phase, "thinking")
            # The page sends the result and asks for the answer, which never comes.
            self.op(op="send", dc="1", data=event("response.create"))
            self.assertTrue(run_until(lambda: self.bridge.phase == "listening", 3))

    def test_the_watchdog_ends_with_the_call(self):
        with mock.patch.object(voice, "THINKING_TIMEOUT_S", 0.1):
            self.say(event("input_audio_buffer.speech_stopped"))
            self.assertEqual(len(self.bridge._watchdogs), 1)
            self.op(op="close")
            self.assertEqual(self.bridge._watchdogs, {})
            run_until(lambda: False, 0.3)
        self.assertEqual(self.phases()[-1], "ended")


@unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
class VoiceBridgeLoopbackTest(unittest.TestCase):
    def setUp(self):
        self.events: list[dict] = []
        self.mic_requests = 0
        self.signals: list[tuple] = []

        def request_microphone(callback):
            self.mic_requests += 1
            callback(True)

        self.bridge = voice.VoiceBridge(lambda pc, event: self.events.append(event),
                                        request_microphone, lambda: False)

    def tearDown(self):
        self.bridge.close_all()

    def watch(self, bridge):
        for name in ("calls-changed", "call-phase", "caption", "call-error", "audio-level"):
            bridge.connect(name, lambda _b, *args, name=name: self.signals.append((name, *args)))

    def phases(self) -> list[str]:
        return [s[1] for s in self.signals if s[0] == "call-phase"]

    def call(self, bridge=None, **request):
        box = {}
        (bridge or self.bridge).handle(request, lambda result, error: box.update(result=result,
                                                                                  error=error))
        self.assertTrue(run_until(lambda: "error" in box, 10), f"no reply to {request['op']}")
        self.assertIsNone(box["error"], f"{request['op']} failed: {box['error']}")
        return box["result"]

    def seen(self, event_type, **fields):
        return any(e.get("type") == event_type and all(e.get(k) == v for k, v in fields.items())
                   for e in self.events)

    def connect_remote(self, pc: str, bridge) -> tuple:
        """Answers ``pc``'s offer from a local peer; returns that peer and its events."""
        offer = self.call(bridge, op="createOffer", pc=pc)
        self.call(bridge, op="setLocalDescription", pc=pc, type="offer", sdp=offer["sdp"])
        remote_events: list[dict] = []
        remote = voice.PeerConnection("remote", remote_events.append, echo_cancellation=False)
        self.addCleanup(remote.close)
        remote.add_microphone()
        done = {}
        remote.set_description(False, "offer", offer["sdp"], lambda e: done.update(remote=e))
        self.assertTrue(run_until(lambda: "remote" in done) and done["remote"] is None)
        remote.create_answer(lambda sdp, e: done.update(answer=sdp))
        self.assertTrue(run_until(lambda: done.get("answer")))
        remote.set_description(True, "answer", done["answer"], lambda e: done.update(local=e))
        self.assertTrue(run_until(lambda: "local" in done))
        complete = GstWebRTC.WebRTCICEGatheringState.COMPLETE
        self.assertTrue(run_until(lambda: remote.webrtc.props.ice_gathering_state == complete, 15))
        answer = remote.webrtc.props.local_description.sdp.as_text()
        self.call(bridge, op="setRemoteDescription", pc=pc, type="answer", sdp=answer)
        return remote, remote_events

    def test_call_with_data_channel_and_audio(self):
        self.assertEqual(self.call(op="requestMicrophone"), {"allowed": True})
        pc = self.call(op="create")["pc"]
        dc = self.call(op="createDataChannel", pc=pc, label="oai-events")["dc"]
        self.call(op="addTrack", pc=pc, kind="audio")
        self.assertTrue(self.bridge.in_call)
        offer = self.call(op="createOffer", pc=pc)
        self.assertEqual(offer["type"], "offer")
        self.assertIn("m=audio", offer["sdp"])
        self.assertIn("m=application", offer["sdp"])
        self.assertIn("OPUS/48000", offer["sdp"].upper())
        self.call(op="setLocalDescription", pc=pc, type="offer", sdp=offer["sdp"])

        # The remote side: answer, gather every candidate, then reply in one SDP.
        remote_events: list[dict] = []
        remote = voice.PeerConnection("remote", remote_events.append, echo_cancellation=False)
        self.addCleanup(remote.close)
        remote.add_microphone()
        done = {}
        remote.set_description(False, "offer", offer["sdp"], lambda e: done.update(remote=e))
        self.assertTrue(run_until(lambda: "remote" in done))
        self.assertIsNone(done["remote"])
        remote.create_answer(lambda sdp, e: done.update(answer=sdp, answer_error=e))
        self.assertTrue(run_until(lambda: "answer" in done))
        self.assertIsNone(done["answer_error"])
        remote.set_description(True, "answer", done["answer"], lambda e: done.update(local=e))
        self.assertTrue(run_until(lambda: "local" in done))
        complete = GstWebRTC.WebRTCICEGatheringState.COMPLETE
        self.assertTrue(run_until(lambda: remote.webrtc.props.ice_gathering_state == complete, 15))
        answer = remote.webrtc.props.local_description.sdp.as_text()
        self.assertIn("a=candidate", answer)

        self.call(op="setRemoteDescription", pc=pc, type="answer", sdp=answer)

        self.assertTrue(run_until(lambda: self.seen("connectionstatechange", state="connected"),
                                  20))
        self.assertTrue(run_until(lambda: self.seen("dcopen", dc=dc), 10))

        # Echo server on the remote data channel.
        def echo_when_ready():
            channel = next((e for e in remote_events if e.get("type") == "datachannel"), None)
            if channel is None:
                return False
            for event in remote_events:
                if event.get("type") == "dcmessage" and event.get("data") == "ping" \
                        and not event.get("answered"):
                    event["answered"] = True
                    remote.send(channel["dc"], '{"type":"session.created"}')
            return True

        self.assertTrue(run_until(echo_when_ready, 10))
        self.call(op="send", pc=pc, dc=dc, data="ping")
        self.assertTrue(run_until(lambda: echo_when_ready() and self.seen(
            "dcmessage", dc=dc, data='{"type":"session.created"}'), 10))

        # Remote audio reached our mixer.
        self.assertTrue(run_until(lambda: self.seen("track", kind="audio"), 10))

        self.call(op="setMicEnabled", pc=pc, enabled=False)
        self.assertFalse(self.bridge.mic_enabled)
        self.call(op="setSpeakerMuted", pc=pc, muted=True)
        self.call(op="close", pc=pc)
        self.assertFalse(self.bridge.in_call)

    def test_unknown_operation_and_closed_call_reply_with_errors(self):
        box = {}
        self.bridge.handle({"op": "dance"}, lambda r, e: box.update(error=e))
        self.assertIn("Unsupported", box["error"])
        self.bridge.handle({"op": "createOffer", "pc": "42"}, lambda r, e: box.update(error=e))
        self.assertIn("no longer active", box["error"])
        self.bridge.handle({"op": "addTrack", "pc": "42", "kind": "video"},
                           lambda r, e: box.update(error=e))
        self.assertIsNotNone(box["error"])

    def test_phases_captions_and_levels_of_a_call(self):
        host = FakeHost(**{"noise-suppression": True, "gain-control": False})
        bridge = voice.VoiceBridge(lambda pc, event: self.events.append(event),
                                   lambda cb: cb(True), host.echo_cancellation)
        self.addCleanup(bridge.close_all)
        self.watch(bridge)
        self.assertEqual(bridge.phase, "")
        pc = self.call(bridge, op="create")["pc"]
        dc = self.call(bridge, op="createDataChannel", pc=pc, label="oai-events")["dc"]
        self.call(bridge, op="createDataChannel", pc=pc, label="something-else")
        self.assertEqual(self.phases(), [])  # no microphone, no call yet
        self.call(bridge, op="addTrack", pc=pc, kind="audio")
        self.assertEqual(self.phases(), ["connecting"])
        self.assertEqual(bridge.phase, "connecting")
        self.assertTrue(bridge.mic_enabled)
        self.assertFalse(bridge.push_to_talk)

        # Noise suppression runs without echo cancellation (which tests leave out).
        if Gst.ElementFactory.find("webrtcdsp") is not None:
            dsp = bridge._peers[pc].pipeline.get_by_name(f"dsp-{pc}")
            self.assertIsNotNone(dsp)
            self.assertFalse(dsp.get_property("echo-cancel"))
            self.assertTrue(dsp.get_property("noise-suppression"))
            self.assertFalse(dsp.get_property("gain-control"))

        remote, remote_events = self.connect_remote(pc, bridge)
        self.assertTrue(run_until(lambda: "listening" in self.phases(), 20), self.phases())
        self.assertTrue(run_until(lambda: self.seen("dcopen", dc=dc), 10))
        self.assertTrue(run_until(
            lambda: len([e for e in remote_events if e.get("type") == "datachannel"]) == 2, 10))
        channels = {e["label"]: e["dc"] for e in remote_events if e.get("type") == "datachannel"}

        # The Dot answers over the events channel, the way the gateway does.
        for data in (event("input_audio_buffer.speech_started"),
                     event("input_audio_buffer.speech_stopped"),
                     event("response.created"),
                     event("output_audio_buffer.started"),
                     event("response.output_audio_transcript.delta", delta="Oi!"),
                     event("conversation.item.input_audio_transcription.completed",
                           transcript="Olá?"),
                     event("response.output_audio_transcript.done", transcript="Oi! Sou o Dot."),
                     event("output_audio_buffer.stopped"),
                     event("error", error={"message": "HTTP 402"})):
            remote.send(channels["oai-events"], data)
        # The same words on another channel are not the provider's.
        remote.send(channels["something-else"], event("error", error={"message": "nope"}))
        self.assertTrue(run_until(lambda: ("call-error", "HTTP 402") in self.signals, 10))
        run_until(lambda: False, 0.3)  # let the other channel's message arrive too
        self.assertEqual(self.phases(), ["connecting", "listening", "thinking", "speaking",
                                         "listening"])
        captions = [s[1:] for s in self.signals if s[0] == "caption"]
        self.assertEqual(captions, [("dot", "Oi!", False), ("you", "Olá?", True),
                                    ("dot", "Oi! Sou o Dot.", True)])
        self.assertNotIn(("call-error", "nope"), self.signals)
        self.assertTrue(self.seen("dcmessage", data=event("error", error={"message": "nope"})))

        # What the page sends counts too.
        self.call(bridge, op="send", pc=pc, dc=dc, data=event("response.create"))
        self.assertEqual(self.phases()[-1], "thinking")

        # Both sides are audible: our test tone and the remote's.
        def heard(role):
            return any(s[0] == "audio-level" and s[1] == role and s[2] > 0.3
                       for s in self.signals)
        self.assertTrue(run_until(lambda: heard("you"), 10))
        self.assertTrue(run_until(lambda: heard("dot"), 10))

        self.call(bridge, op="close", pc=pc)
        self.assertEqual(self.phases()[-1], "ended")
        self.assertEqual(bridge.phase, "")
        bridge.close_all()
        self.assertEqual(self.phases().count("ended"), 1)

    def test_push_to_talk_and_join_muted_start_muted(self):
        for options in ({"mic-mode": "push-to-talk"}, {"join-muted": True}):
            with self.subTest(options=options):
                self.signals.clear()
                host = FakeHost(**options)
                bridge = voice.VoiceBridge(lambda pc, event: None, lambda cb: cb(True),
                                           host.echo_cancellation)
                self.addCleanup(bridge.close_all)
                self.watch(bridge)
                pc = self.call(bridge, op="create")["pc"]
                self.call(bridge, op="addTrack", pc=pc, kind="audio")
                self.assertTrue(bridge.in_call)
                self.assertFalse(bridge.mic_enabled)
                self.assertIn(("calls-changed",), self.signals)
                volume = bridge._peers[pc]._mic_volume
                self.assertTrue(volume.get_property("mute"))
                self.assertEqual(bridge.push_to_talk, options.get("mic-mode") == "push-to-talk")
                # Holding the talk key (or unmuting) opens the microphone.
                bridge.set_mic_enabled(True)
                self.assertTrue(bridge.mic_enabled)
                self.assertFalse(volume.get_property("mute"))
                bridge.set_mic_enabled(False)
                self.assertFalse(bridge.mic_enabled)
                self.call(bridge, op="close", pc=pc)
                self.assertFalse(bridge.in_call)

    def test_open_microphone_by_default(self):
        bridge = voice.VoiceBridge(lambda pc, event: None, lambda cb: cb(True),
                                   FakeHost().echo_cancellation)
        self.addCleanup(bridge.close_all)
        pc = self.call(bridge, op="create")["pc"]
        self.call(bridge, op="addTrack", pc=pc, kind="audio")
        self.assertTrue(bridge.mic_enabled)
        self.assertFalse(bridge._peers[pc]._mic_volume.get_property("mute"))

    def test_unknown_devices_fall_back_to_the_system_default(self):
        host = FakeHost(**{"microphone-device": "dotdeck-test-no-such-mic",
                           "speaker-device": "dotdeck-test-no-such-speaker"})
        bridge = voice.VoiceBridge(lambda pc, event: None, lambda cb: cb(True),
                                   host.echo_cancellation)
        self.addCleanup(bridge.close_all)
        pc = self.call(bridge, op="create")["pc"]
        self.call(bridge, op="addTrack", pc=pc, kind="audio")
        peer = bridge._peers[pc]
        self.assertEqual((peer.microphone_name, peer.speaker_name), ("", ""))
        self.assertNotEqual(peer.pipeline.get_state(5 * Gst.SECOND)[0],
                            Gst.StateChangeReturn.FAILURE)


if __name__ == "__main__":
    unittest.main()
