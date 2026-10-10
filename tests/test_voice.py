"""The native WebRTC bridge, end to end against a local GStreamer peer.

The remote peer behaves like the OpenAI Realtime endpoint: it answers an offer that
carries no ICE candidates with an answer that carries all of its own, and talks over
a data channel created by the caller.
"""

from __future__ import annotations

import unittest

from tests.helpers import run_until  # noqa: F401  (sets the test environment first)

from src.core import voice  # noqa: E402

if voice.available():
    from gi.repository import GstWebRTC  # noqa: E402


@unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
class VoiceBridgeLoopbackTest(unittest.TestCase):
    def setUp(self):
        self.events: list[dict] = []
        self.mic_requests = 0

        def request_microphone(callback):
            self.mic_requests += 1
            callback(True)

        self.bridge = voice.VoiceBridge(lambda pc, event: self.events.append(event),
                                        request_microphone, lambda: False)

    def tearDown(self):
        self.bridge.close_all()

    def call(self, **request):
        box = {}
        self.bridge.handle(request, lambda result, error: box.update(result=result, error=error))
        self.assertTrue(run_until(lambda: "error" in box, 10), f"no reply to {request['op']}")
        self.assertIsNone(box["error"], f"{request['op']} failed: {box['error']}")
        return box["result"]

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

        def seen(event_type, **fields):
            return any(e.get("type") == event_type and all(e.get(k) == v for k, v in fields.items())
                       for e in self.events)

        self.assertTrue(run_until(lambda: seen("connectionstatechange", state="connected"), 20))
        self.assertTrue(run_until(lambda: seen("dcopen", dc=dc), 10))

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
        self.assertTrue(run_until(lambda: echo_when_ready() and seen(
            "dcmessage", dc=dc, data='{"type":"session.created"}'), 10))

        # Remote audio reached our mixer.
        self.assertTrue(run_until(lambda: seen("track", kind="audio"), 10))

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


if __name__ == "__main__":
    unittest.main()
