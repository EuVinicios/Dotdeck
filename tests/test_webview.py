"""The server view in a real WebKit: token hand-off, links, notifications and the
voice-call polyfill driving the native bridge, the way the OpenDots page does."""

from __future__ import annotations

import json
import os
import unittest

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit  # noqa: E402

from src.core import voice  # noqa: E402
if voice.available():
    from gi.repository import GstWebRTC  # noqa: E402
from src.core.store import Profile  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402

# Mirrors the call setup of src/client/useVoice.ts in OpenDots.
VOICE_PAGE = """<!doctype html><meta charset="utf-8"><title>OpenDots</title><script>
window.__log = [];
const log = (x) => window.__log.push(String(x));
log('token:' + sessionStorage.getItem('opendots-token'));
log('rtc:' + typeof RTCPeerConnection);
window.__start = async () => {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const pc = new RTCPeerConnection();
  const audio = new Audio();
  audio.autoplay = true;
  const channel = pc.createDataChannel('oai-events');
  stream.getTracks().forEach((track) => pc.addTrack(track, stream));
  pc.ontrack = (event) => {
    audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
    log('track');
    audio.play().then(() => log('playing'), (e) => log('play-error:' + e));
  };
  pc.onconnectionstatechange = () => log('state:' + pc.connectionState);
  channel.onopen = () => { log('open'); channel.send('ping'); };
  channel.onmessage = (event) => log('message:' + event.data);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  Object.assign(window, { __pc: pc, __stream: stream, __audio: audio, __channel: channel });
  return offer.sdp;
};
window.__answer = async (sdp) => {
  await window.__pc.setRemoteDescription({ type: 'answer', sdp });
  log('answered');
};
</script><body>voice test</body>"""


class FakeHost:
    def __init__(self):
        self.external: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.tokens: list[str] = []
        self.microphone = True
        self.mic_requests = 0

    def toast(self, text, button=None, action=None):
        pass

    def open_external(self, uri):
        self.external.append(uri)

    def web_notification(self, profile, title, body):
        self.notifications.append((title, body))

    def ask_microphone(self, profile, callback):
        self.mic_requests += 1
        callback(self.microphone)

    def token_from_page(self, profile, token):
        self.tokens.append(token)

    def edit_profile(self, profile):
        pass

    def echo_cancellation(self):
        return False


@unittest.skipUnless(Gtk.init_check() if hasattr(Gtk, "init_check") else True,
                     "no display available")
class ServerViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.web.webview import ServerView
        cls.ServerView = ServerView
        cls.fake = FakeOpenDots(token="owner-token-for-tests-1234", page=VOICE_PAGE).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.host = FakeHost()
        self.profile = Profile("test", "Test", self.fake.url)
        self.view = self.ServerView(self.profile, "owner-token-for-tests-1234", self.host)
        self.window = Gtk.Window(child=self.view, default_width=800, default_height=600)
        self.window.present()
        self.assertTrue(run_until(lambda: self.view.loaded_once, 20), "page did not load")

    def tearDown(self):
        self.view.destroy()
        self.window.destroy()

    def js(self, body: str, **args):
        box = {}
        variant = GLib.Variant("a{sv}", {k: GLib.Variant("s", v) for k, v in args.items()})

        def done(view, result):
            try:
                value = view.call_async_javascript_function_finish(result)
                box["value"] = json.loads(value.to_json(0)) if value and not \
                    value.is_undefined() else None
            except GLib.Error as error:
                box["error"] = error.message

        self.view.webview.call_async_javascript_function(body, -1, variant, None, None, None, done)
        self.assertTrue(run_until(lambda: box, 15), f"no result for {body[:40]}")
        if "error" in box:
            raise AssertionError(box["error"])
        return box["value"]

    def log(self) -> list[str]:
        return self.js("return window.__log;")

    def test_token_is_handed_to_the_page_and_new_tokens_are_reported(self):
        self.assertIn("token:owner-token-for-tests-1234", self.log())
        self.js("sessionStorage.setItem('opendots-token', 'typed-in-the-page'); return true;")
        self.assertTrue(run_until(lambda: self.host.tokens, 5))
        self.assertEqual(self.host.tokens, ["typed-in-the-page"])

    def test_foreign_links_open_outside_and_notifications_are_native(self):
        # Leaving the server opens the browser instead (scripts run by the test count
        # as user gestures), and the view stays on the server.
        self.js("location.href = 'https://example.com/elsewhere'; return true;")
        self.assertTrue(run_until(lambda: self.host.external, 5))
        # Pop-ups are blocked by default; allow them here to exercise the new-window path.
        self.view.webview.get_settings().set_javascript_can_open_windows_automatically(True)
        self.js("window.open('https://example.com/docs', '_blank'); return true;")
        self.assertTrue(run_until(lambda: len(self.host.external) == 2, 5))
        self.assertEqual(self.host.external, ["https://example.com/elsewhere",
                                              "https://example.com/docs"])
        self.assertTrue(self.view.webview.get_uri().startswith(self.fake.url))
        self.js("""
            await Notification.requestPermission();
            new Notification('Task finished', { body: 'Weekly report' });
            return true;""")
        self.assertTrue(run_until(lambda: self.host.notifications, 5))
        self.assertEqual(self.host.notifications, [("Task finished", "Weekly report")])

    def reload_with_script(self, source: str) -> None:
        """Reload with ``source`` running before Dotdeck's own scripts."""
        finished = []
        handler = self.view.webview.connect("load-changed", lambda _v, e: finished.append(e))
        self.view.content.remove_all_scripts()
        self.view.content.add_script(WebKit.UserScript.new(
            source, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        self.view._install_scripts()
        self.view.reload()
        self.assertTrue(run_until(lambda: WebKit.LoadEvent.FINISHED in finished, 15))
        self.view.webview.disconnect(handler)

    @unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
    def test_microphone_without_media_devices(self):
        # WebKit leaves navigator.mediaDevices out on insecure origins (plain http on a
        # LAN or Tailscale address); calls must still get the microphone from Dotdeck.
        self.reload_with_script("delete Navigator.prototype.mediaDevices;")
        result = self.js("""
            const devices = navigator.mediaDevices;
            const stream = await devices.getUserMedia({ audio: true });
            let video;
            try { await devices.getUserMedia({ video: true }); video = 'allowed'; }
            catch (error) { video = error.name; }
            const listed = await devices.enumerateDevices();
            return { tracks: stream.getAudioTracks().length, video,
                     kinds: listed.map((device) => device.kind),
                     echo: devices.getSupportedConstraints().echoCancellation };""")
        self.assertEqual(result, {"tracks": 1, "video": "NotSupportedError",
                                  "kinds": ["audioinput"], "echo": True})
        self.assertEqual(self.host.mic_requests, 1)
        self.host.microphone = False
        self.assertEqual(self.js("""
            try { await navigator.mediaDevices.getUserMedia({ audio: true }); return 'allowed'; }
            catch (error) { return error.name; }"""), "NotAllowedError")

    @unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
    def test_calls_go_through_dotdeck_even_when_webkit_has_webrtc(self):
        # Dotdeck's call controls only see calls that run in its own WebRTC.
        self.reload_with_script("window.RTCPeerConnection = function NativePeer() {};")
        self.assertEqual(self.js("return [RTCPeerConnection.name, "
                                 "typeof window.__dotdeckVoiceEvent];"),
                         ["DotdeckPeerConnection", "function"])

    def test_microphone_denied(self):
        self.host.microphone = False
        result = self.js("""
            try { await navigator.mediaDevices.getUserMedia({ audio: true }); return 'allowed'; }
            catch (error) { return error.name; }""")
        self.assertEqual(result, "NotAllowedError")

    @unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
    def test_microphone_switch_survives_garbage_collection(self):
        # After a garbage collection WebKit hands the page a fresh navigator.mediaDevices
        # wrapper. A switch kept on the old wrapper was gone, the native getUserMedia()
        # came back with a track the call cannot carry, and the real OpenDots page (big
        # enough to collect) failed with "Dotdeck only carries the microphone in calls."
        self.js("""let kept = 0;
            for (let i = 0; i < 60; i++) {
              const junk = [];
              for (let j = 0; j < 200000; j++) junk.push({ j, s: 'x' + j });
              kept += junk.length;
            }
            return kept;""")
        run_until(lambda: False, 1)
        result = self.js("""
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const pc = new RTCPeerConnection();
            try {
              stream.getTracks().forEach((track) => pc.addTrack(track, stream));
              return 'ok';
            } catch (error) {
              return error.message;
            } finally {
              pc.close();
              stream.getTracks().forEach((track) => track.stop());
            }""")
        self.assertEqual(result, "ok")

    @unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
    def test_voice_call_through_the_polyfill(self):
        self.assertIn("rtc:function", self.log())
        offer = self.js("return await window.__start();")
        self.assertIn("m=application", offer)
        self.assertEqual(self.host.mic_requests, 1)

        remote_events: list[dict] = []
        remote = voice.PeerConnection("remote", remote_events.append, echo_cancellation=False)
        self.addCleanup(remote.close)
        remote.add_microphone()
        done = {}
        remote.set_description(False, "offer", offer, lambda e: done.update(remote=e))
        self.assertTrue(run_until(lambda: "remote" in done) and done["remote"] is None)
        remote.create_answer(lambda sdp, e: done.update(answer=sdp))
        self.assertTrue(run_until(lambda: done.get("answer")))
        remote.set_description(True, "answer", done["answer"], lambda e: done.update(local=e))
        complete = GstWebRTC.WebRTCICEGatheringState.COMPLETE
        self.assertTrue(run_until(lambda: remote.webrtc.props.ice_gathering_state == complete, 15))
        answer = remote.webrtc.props.local_description.sdp.as_text()

        def pong():
            channel = next((e for e in remote_events if e.get("type") == "datachannel"), None)
            for event in remote_events:
                if channel and event.get("data") == "ping" and not event.get("answered"):
                    event["answered"] = True
                    remote.send(channel["dc"], "pong")
            return channel is not None

        self.js("await window.__answer(answer); return true;", answer=answer)
        for expected in ("state:connected", "open", "message:pong", "track", "playing"):
            self.assertTrue(run_until(lambda: pong() and expected in self.log(), 20),
                            f"missing {expected!r} in {self.log()}")

        self.assertTrue(self.view.voice.in_call)
        self.js("window.__stream.getAudioTracks()[0].enabled = false; return true;")
        self.assertTrue(run_until(lambda: not self.view.voice.mic_enabled, 5))
        self.js("window.__audio.muted = true; return true;")
        peer = next(iter(self.view.voice._peers.values()))
        self.assertTrue(run_until(lambda: peer.speaker_muted, 5))
        self.js("window.__audio.muted = false; return true;")
        self.assertTrue(run_until(lambda: not peer.speaker_muted, 5))
        # Pausing the call's audio silences the speakers; playing brings them back.
        self.js("window.__audio.pause(); return true;")
        self.assertTrue(run_until(lambda: peer.speaker_muted, 5))
        self.js("window.__audio.muted = false; return true;")
        run_until(lambda: False, 0.3)
        self.assertTrue(peer.speaker_muted)
        self.js("await window.__audio.play(); return true;")
        self.assertTrue(run_until(lambda: not peer.speaker_muted, 5))
        self.js("window.__channel.close(); window.__pc.close(); return true;")
        self.assertTrue(run_until(lambda: not self.view.voice.in_call, 5))


if __name__ == "__main__":
    os.environ.setdefault("GDK_BACKEND", "wayland,x11")
    unittest.main()
