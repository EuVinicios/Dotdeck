"""The voice gateway: VAD, the Realtime HTTP surface and a full call against a fake OpenRouter.

The caller is Dotdeck's own native WebRTC peer, talking to the gateway the way the OpenDots
page talks to OpenAI: offer without candidates, ``oai-events`` data channel, function calls.
"""

from __future__ import annotations

import array
import base64
import json
import math
import sys
import threading
import unittest
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers import run_until

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from voice_gateway import vad  # noqa: E402
from voice_gateway.http_api import parse_form  # noqa: E402
from voice_gateway.call import chat_tools, opus_payload_type  # noqa: E402

from src.core import voice  # noqa: E402

TOKEN = "gateway-token-for-tests-0123456789"


def tone(seconds: float, rate: int = 16000, level: float = 0.2) -> bytes:
    return array.array("h", (int(32767 * level * math.sin(2 * math.pi * 440 * i / rate))
                             for i in range(int(seconds * rate)))).tobytes()


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\0\0" * int(seconds * rate)


class VadTest(unittest.TestCase):
    def test_segments_an_utterance(self):
        detector = vad.Vad(stop_ms=600)
        events = detector.feed(silence(0.5) + tone(1.0) + silence(1.0))
        self.assertEqual([e[0] for e in events], [vad.START, vad.END])
        seconds = len(events[1][1]) / 2 / 16000
        self.assertTrue(1.0 <= seconds <= 1.8, seconds)

    def test_ignores_clicks_and_respects_bias(self):
        detector = vad.Vad()
        self.assertEqual(detector.feed(silence(0.3) + tone(0.04) + silence(1.0)), [])
        quiet = vad.Vad()
        quiet.bias_db = 40
        self.assertEqual(quiet.feed(silence(0.3) + tone(1.0, level=0.05)), [])


class FormAndToolsTest(unittest.TestCase):
    def test_multipart_like_undici(self):
        boundary = "----formdata-undici-0123"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sdp\"\r\n\r\nv=0\r\n"
                f"m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n\r\n--{boundary}\r\nContent-Disposition: "
                f"form-data; name=\"session\"\r\n\r\n{{\"model\":\"x\"}}\r\n--{boundary}--\r\n")
        fields = parse_form(f"multipart/form-data; boundary={boundary}", body.encode())
        self.assertTrue(fields["sdp"].startswith("v=0"))
        self.assertEqual(json.loads(fields["session"]), {"model": "x"})

    def test_tools_and_payload_type(self):
        tools = chat_tools({"tools": [{"type": "function", "name": "ask_compute",
                                       "description": "d", "parameters": {"type": "object"}}]})
        self.assertEqual(tools[0]["function"]["name"], "ask_compute")
        self.assertEqual(opus_payload_type("a=rtpmap:109 opus/48000/2\r\n"), 109)
        self.assertEqual(opus_payload_type("no opus here"), 111)


class FakeOpenRouter:
    """Streams like OpenRouter's gpt-audio-mini: audio deltas with transcripts, tool calls."""

    def __init__(self, tool_first: bool):
        self.requests: list[dict] = []
        self.tool_first = tool_first
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if not body.get("stream"):
                    data = json.dumps({"choices": [{"message": {"content": "oi chefe"}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                last = body["messages"][-1]
                streamed = [m for m in fake.requests if m.get("stream")]
                if fake.tool_first and len(streamed) == 1:
                    chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                               "type": "function", "function": {"name": "ask_compute",
                                                                "arguments": ""}}]}}]},
                              {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                                  "arguments": "{\"request\":\"clima\"}"}}]},
                                  "finish_reason": "tool_calls"}]}]
                else:
                    words = ["Resultado", " pronto"] if last.get("role") == "tool" else \
                        ["Olá", ", tudo bem?"]
                    pcm = tone(0.6, rate=24000)
                    half = len(pcm) // 2
                    chunks = [{"choices": [{"delta": {"audio": {"transcript": words[0],
                               "data": base64.b64encode(pcm[:half]).decode()}}}]},
                              {"choices": [{"delta": {"audio": {"transcript": words[1],
                               "data": base64.b64encode(pcm[half:]).decode()}},
                                            "finish_reason": "stop"}],
                               "usage": {"cost": 0.0002}}]
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
class GatewayCallTest(unittest.TestCase):
    def setUp(self):
        from voice_gateway.config import Config
        from voice_gateway.http_api import Gateway
        self.fake = FakeOpenRouter(tool_first=True)
        self.addCleanup(self.fake.stop)
        config = Config(token=TOKEN, api_key="test", base_url=self.fake.url, port=0,
                        vad_stop_ms=500)
        self.gateway = Gateway(config)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.shutdown)

    def post(self, path: str, form: dict | None = None):
        boundary = uuid.uuid4().hex
        body = b""
        for name, value in (form or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n").encode()
        body += f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{self.gateway.url}{path}", data=body, method="POST",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        box = {}

        def worker():
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    box["status"], box["headers"] = response.status, dict(response.headers)
                    box["body"] = response.read().decode()
            except Exception as error:  # reported by the assertion below
                box["error"] = error

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(run_until(lambda: not thread.is_alive(), 25))
        self.assertNotIn("error", box)
        return box

    def test_rejects_bad_token(self):
        request = urllib.request.Request(f"{self.gateway.url}/v1/realtime/calls", data=b"x",
                                         method="POST", headers={"Authorization": "Bearer no"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_full_call_with_tool(self):
        events: list[dict] = []
        caller = voice.PeerConnection("caller", events.append, echo_cancellation=False)
        self.addCleanup(caller.close)
        dc = caller.create_data_channel("oai-events")
        caller.add_microphone()
        box = {}
        caller.create_offer(lambda sdp, e: box.update(offer=sdp))
        self.assertTrue(run_until(lambda: box.get("offer"), 10))
        caller.set_description(True, "offer", box["offer"], lambda e: box.update(local=e))
        self.assertTrue(run_until(lambda: "local" in box, 10))

        session = {"type": "realtime", "model": "gpt-4o-realtime-preview",
                   "instructions": "You are Chefe.", "audio": {"output": {"voice": "marin"}},
                   "tools": [{"type": "function", "name": "ask_compute", "description": "d",
                              "parameters": {"type": "object", "properties": {
                                  "request": {"type": "string"}}}}]}
        reply = self.post("/v1/realtime/calls", {"sdp": box["offer"],
                                                 "session": json.dumps(session)})
        self.assertEqual(reply["status"], 201)
        self.assertRegex(reply["headers"]["Location"], r"^/v1/realtime/calls/rtc_[0-9a-f]+$")
        self.assertIn("a=candidate", reply["body"])
        caller.set_description(False, "answer", reply["body"], lambda e: box.update(remote=e))

        def messages():
            return [json.loads(e["data"]) for e in events if e.get("type") == "dcmessage"]

        def got(kind, **fields):
            return any(m.get("type") == kind and all(m.get(k) == v for k, v in fields.items())
                       for m in messages())

        self.assertTrue(run_until(lambda: any(e.get("state") == "connected" for e in events), 20))
        self.assertTrue(run_until(lambda: got("session.created"), 10))
        self.assertEqual(next(m for m in messages() if m["type"] == "session.created")
                         ["session"]["voice"], "marin")

        # Speak for a second, then go quiet: the gateway hears a whole utterance.
        self.assertTrue(run_until(lambda: got("input_audio_buffer.speech_started"), 10))
        run_until(lambda: False, 1.0)
        caller.set_mic_enabled(False)
        self.assertTrue(run_until(lambda: got("input_audio_buffer.speech_stopped"), 10))
        self.assertTrue(run_until(lambda: got("response.function_call_arguments.done",
                                              name="ask_compute", call_id="call_1"), 15))
        first = next(r for r in self.fake.requests if r.get("stream"))
        self.assertEqual(first["model"], "openai/gpt-audio-mini")
        self.assertEqual(first["audio"], {"voice": "marin", "format": "pcm16"})
        self.assertEqual(first["messages"][0], {"role": "system", "content": "You are Chefe."})
        self.assertEqual(first["messages"][-1]["content"][0]["type"], "input_audio")
        self.assertEqual(first["tools"][0]["function"]["name"], "ask_compute")
        self.assertTrue(run_until(lambda: got(
            "conversation.item.input_audio_transcription.completed", transcript="oi chefe"), 10))

        # Like useVoice.ts: hand back the compute result and ask for the spoken answer.
        caller.send(dc, json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": "Sol, 31 °C"}}))
        caller.send(dc, json.dumps({"type": "response.create"}))
        self.assertTrue(run_until(lambda: got("response.output_audio_transcript.done",
                                              transcript="Resultado pronto"), 15))
        second = [r for r in self.fake.requests if r.get("stream")][1]
        self.assertEqual(second["messages"][-1], {"role": "tool", "tool_call_id": "call_1",
                                                  "content": "Sol, 31 °C"})
        self.assertEqual(second["messages"][-2]["tool_calls"][0]["function"]["name"],
                         "ask_compute")
        self.assertIsInstance(second["messages"][1]["content"], str)  # audio became text
        self.assertTrue(run_until(lambda: any(e.get("type") == "track" for e in events), 10))
        self.assertTrue(run_until(lambda: got("output_audio_buffer.stopped"), 10))

        call_id = reply["headers"]["Location"].rsplit("/", 1)[1]
        hangup = self.post(f"/v1/realtime/calls/{call_id}/hangup")
        self.assertEqual(hangup["status"], 200)
        self.assertTrue(run_until(lambda: not self.gateway.calls, 5))


if __name__ == "__main__":
    unittest.main()
