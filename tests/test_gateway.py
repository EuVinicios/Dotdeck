"""The voice gateway: VAD, the Realtime HTTP surface and a full call against a fake OpenRouter.

The caller is Dotdeck's own native WebRTC peer, talking to the gateway the way the OpenDots
page talks to OpenAI: offer without candidates, ``oai-events`` data channel, function calls.
The call logic (receipt order, speech during a compute request, voice changes) is also driven
directly, without WebRTC, and ``server/install.sh`` runs against a throwaway HOME with every
outside command faked.
"""

from __future__ import annotations

import array
import base64
import gc
import http.client
import io
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import wave
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers import run_until

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from voice_gateway import vad  # noqa: E402
from voice_gateway.http_api import parse_form  # noqa: E402
from voice_gateway.call import Call, chat_tools, opus_payload_type  # noqa: E402
from voice_gateway.openrouter import OpenRouter, ProviderError  # noqa: E402

from src.core import voice  # noqa: E402

TOKEN = "gateway-token-for-tests-0123456789"
SECRET_KEY = "sk-or-v1-not-a-real-key-0123456789abcdef"
ASK_COMPUTE = {"type": "function", "name": "ask_compute", "description": "d",
               "parameters": {"type": "object", "properties": {"request": {"type": "string"}}}}
TRANSCRIBED = "conversation.item.input_audio_transcription.completed"
SPOKEN = "response.output_audio_transcript.done"


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


def seconds_of(wav_b64: str) -> float:
    with wave.open(io.BytesIO(base64.b64decode(wav_b64))) as audio:
        return audio.getnframes() / audio.getframerate()


class FakeOpenRouter:
    """Streams like OpenRouter's gpt-audio-mini: audio deltas with transcripts, tool calls.

    Transcriptions answer "oi chefe" after ``transcribe_delay`` seconds, or, with
    ``by_length``, "fala de N s" (the length of the audio) so each utterance can be told apart.
    ``break_next`` spoils the next streamed reply: "drop" cuts the connection mid-reply,
    "garbage" sends chunks of unexpected shapes and then an error.
    """

    def __init__(self, tool_first: bool, transcribe_delay: float = 0.0, by_length: bool = False):
        self.requests: list[dict] = []
        self.tool_first = tool_first
        self.break_next = ""
        self.transcription_cost: object = 0.00001
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if not body.get("stream"):
                    time.sleep(transcribe_delay)
                    text = "oi chefe"
                    if by_length:
                        audio = body["messages"][-1]["content"][0]["input_audio"]["data"]
                        text = f"fala de {seconds_of(audio):.1f} s"
                    data = json.dumps({"choices": [{"message": {"content": text}}],
                                       "usage": {"cost": fake.transcription_cost}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                spoil, fake.break_next = fake.break_next, ""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if spoil == "drop":
                    # Chunked, and the connection closes in the middle of a chunk.
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    piece = b'data: {"choices": [{"delta": {"audio": {"transcript": "Ol"}}}]}\n\n'
                    self.wfile.write(b"%x\r\n%s\r\n400\r\npartial" % (len(piece), piece))
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.end_headers()
                last = body["messages"][-1]
                streamed = [m for m in fake.requests if m.get("stream")]
                if spoil == "garbage":
                    chunks = [[1, 2], "text", {
                        "choices": [None, {"delta": "x"},
                                    {"delta": {"audio": "x", "tool_calls": "x"}},
                                    {"delta": {"audio": {"transcript": 5,
                                                         "data": "!!notbase64"}}}],
                        "usage": {"cost": "n/a"}},
                        {"choices": [{"delta": {"audio": {"transcript": "Oi"}}}]},
                        {"error": "Upstream exploded"}]
                elif fake.tool_first and len(streamed) == 1:
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
                try:
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass                  # the gateway cancelled the turn (hang-up, barge-in)

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
        self.logs = collect_logs(self)
        config = Config(token=TOKEN, api_key=SECRET_KEY, base_url=self.fake.url, port=0,
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
                                         method="POST",
                                         headers={"Authorization": "Bearer sk-proj-pasted-key"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        # The owner can tell why calls fail, without the key that was sent.
        self.assertTrue(any("wrong gateway token" in line for line in self.logs))
        self.assertFalse(any("sk-proj-pasted-key" in line for line in self.logs))

    def test_rejects_malformed_requests(self):
        port = self.gateway.server.server_address[1]
        for length in ("-5", "lots"):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            self.addCleanup(connection.close)
            connection.putrequest("POST", "/v1/realtime/calls")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Length", length)
            connection.endheaders()
            reply = connection.getresponse()
            self.assertEqual((reply.status, json.loads(reply.read())["error"]["message"]),
                             (400, "Invalid Content-Length."))
        offer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        for session in ("[1]", "not json", '"text"'):
            boundary = uuid.uuid4().hex
            body = "".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\""
                           f"\r\n\r\n{v}\r\n" for k, v in (("sdp", offer), ("session", session)))
            request = urllib.request.Request(
                f"{self.gateway.url}/v1/realtime/calls", method="POST",
                data=f"{body}--{boundary}--\r\n".encode(),
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 400, session)
        self.assertEqual(self.gateway.calls, {})

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
        freed = weakref.ref(self.gateway.calls[call_id])
        hangup = self.post(f"/v1/realtime/calls/{call_id}/hangup")
        self.assertEqual(hangup["status"], 200)
        self.assertTrue(run_until(lambda: not self.gateway.calls, 5))
        # The call and its pipeline are freed, not kept alive by their signal handlers.
        self.assertTrue(run_until(lambda: gc.collect() >= 0 and freed() is None, 10))

        # One line when the call starts and one when it ends, with duration and cost.
        started = [line for line in self.logs if f"Call {call_id} started:" in line]
        ended = [line for line in self.logs if f"Call {call_id} ended" in line]
        self.assertEqual(len(started), 1, self.logs)
        self.assertIn("model openai/gpt-audio-mini, voice marin, answered in", started[0])
        self.assertEqual(len(ended), 1, self.logs)
        self.assertRegex(ended[0], r"ended \(hangup\): \d+ s, \d+ repl(y|ies), "
                                   r"1 compute request, US\$ 0\.000\d\d$")
        for line in self.logs:
            self.assertNotIn(TOKEN, line)
            self.assertNotIn(SECRET_KEY, line)



class ProviderStreamTest(unittest.TestCase):
    """Whatever OpenRouter sends, a turn ends with a result or a ProviderError."""

    def setUp(self):
        self.fake = FakeOpenRouter(tool_first=False)
        self.addCleanup(self.fake.stop)
        self.provider = OpenRouter(SECRET_KEY, self.fake.url)
        self.words: list[str] = []
        self.audio: list[bytes] = []

    def turn(self):
        return self.provider.stream_turn("openai/gpt-audio-mini",
                                         [{"role": "user", "content": "oi"}], "marin", [],
                                         self.words.append, self.audio.append,
                                         threading.Event())

    def test_a_reply_cut_mid_stream_is_a_provider_error(self):
        self.fake.break_next = "drop"
        with self.assertRaises(ProviderError) as caught:
            self.turn()
        self.assertIn("dropped during the reply", str(caught.exception))
        self.assertEqual(self.words, ["Ol"])

    def test_odd_chunks_are_skipped_and_a_stream_error_is_reported(self):
        self.fake.break_next = "garbage"
        with self.assertRaises(ProviderError) as caught:
            self.turn()
        self.assertEqual(str(caught.exception), "Upstream exploded")
        self.assertEqual((self.words, self.audio), (["Oi"], []))
        result = self.turn()                         # the next turn is unaffected
        self.assertEqual((result.transcript, result.cost), ("Olá, tudo bem?", 0.0002))

    def test_transcription_keeps_the_words_when_the_cost_is_odd(self):
        self.fake.transcription_cost = "n/a"
        result = self.provider.transcribe("google/gemini-2.5-flash-lite", tone(0.5), 16000)
        self.assertEqual((result.text, result.cost), ("oi chefe", 0.0))


@unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
class MainTest(unittest.TestCase):
    """``python3 -m voice_gateway`` as the systemd unit runs it."""

    def test_a_busy_port_is_one_clear_error_line(self):
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        env = dict(os.environ, VOICE_GATEWAY_TOKEN=TOKEN, OPENROUTER_API_KEY=SECRET_KEY,
                   VOICE_GATEWAY_PORT=str(port), VOICE_GATEWAY_VOICE="robot")
        for name in ("JOURNAL_STREAM", "VOICE_GATEWAY_DEBUG", "VOICE_GATEWAY_FORCE_VOICE"):
            env.pop(name, None)
        result = subprocess.run([sys.executable, "-m", "voice_gateway"], cwd=SERVER, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 4, result.stderr)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(lines[-1], f"ERROR voice_gateway: Cannot listen on 127.0.0.1:{port}: "
                                    "Address already in use. Is another voice gateway still "
                                    "running?")
        self.assertIn("VOICE_GATEWAY_VOICE=robot is not one of the voices", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(SECRET_KEY, result.stderr + result.stdout)
        self.assertNotIn(TOKEN, result.stderr + result.stdout)

    def test_every_line_of_a_journal_entry_carries_its_priority(self):
        from voice_gateway.__main__ import FORMAT, JournalFormatter
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("voice_gateway.call", logging.ERROR, __file__, 1,
                                       "Call %s: the reply failed", ("rtc_x",), sys.exc_info())
        lines = JournalFormatter(FORMAT).format(record).split("\n")
        self.assertEqual(lines[0], "<3>ERROR voice_gateway.call: Call rtc_x: the reply failed")
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(line.startswith("<3>") for line in lines), lines)
        info = logging.LogRecord("voice_gateway", logging.INFO, __file__, 1, "Listening", (),
                                 None)
        self.assertEqual(JournalFormatter(FORMAT).format(info), "<6>INFO voice_gateway: Listening")


def collect_logs(test: unittest.TestCase) -> list[str]:
    """Formatted INFO+ lines from the gateway's loggers for the rest of ``test``."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Collect(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("voice_gateway")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    test.addCleanup(logger.setLevel, level)
    test.addCleanup(logger.removeHandler, handler)
    return lines


class RecordingCall(Call):
    """A call whose Realtime events are recorded (with a timestamp) instead of sent."""

    def __init__(self, *args, **kwargs):
        self.events: list[dict] = []
        super().__init__(*args, **kwargs)

    def send(self, event: dict) -> None:
        if not self.closed:
            self.events.append(dict(event, at=time.monotonic()))

    def count(self, kind: str) -> int:
        return sum(1 for e in list(self.events) if e["type"] == kind)

    def first(self, kind: str) -> dict:
        return next(e for e in list(self.events) if e["type"] == kind)

    def last(self, kind: str) -> dict:
        return [e for e in list(self.events) if e["type"] == kind][-1]

    def receipt(self) -> list[str]:
        """The transcript useVoice.ts builds from our events for the call receipt."""
        lines = []
        for event in list(self.events):
            if event["type"] == TRANSCRIBED:
                lines.append(f"You: {event['transcript']}")
            elif event["type"] == SPOKEN:
                lines.append(f"Dot: {event['transcript']}")
        return lines


@unittest.skipUnless(voice.available(), "GStreamer WebRTC plugins are not installed")
class CallLogicTest(unittest.TestCase):
    """Receipt order, speech during ask_compute and voice changes, driven without WebRTC."""

    def make_call(self, tool_first: bool = False, transcribe_delay: float = 0.0,
                  **config) -> RecordingCall:
        from voice_gateway.config import Config
        from voice_gateway.openrouter import OpenRouter
        self.fake = FakeOpenRouter(tool_first, transcribe_delay=transcribe_delay,
                                   by_length=True)
        self.addCleanup(self.fake.stop)
        settings = Config(token=TOKEN, api_key=SECRET_KEY, base_url=self.fake.url, port=0,
                          **config)
        session = {"type": "realtime", "model": "gpt-4o-realtime-preview",
                   "instructions": "You are Chefe.", "audio": {"output": {"voice": "marin"}},
                   "tools": [ASK_COMPUTE]}
        call = RecordingCall("rtc_test", session, settings,
                             OpenRouter(SECRET_KEY, self.fake.url), lambda _id: None)
        call.follow_up_delay = 0.2
        self.addCleanup(call.close)
        return call

    def streamed(self) -> list[dict]:
        return [r for r in list(self.fake.requests) if r.get("stream")]

    def wait(self, call: RecordingCall, kind: str, count: int = 1, timeout: float = 10) -> None:
        self.assertTrue(run_until(lambda: call.count(kind) >= count, timeout),
                        f"no {count}× {kind} in {[e['type'] for e in call.events]}")

    def tool_output(self, call: RecordingCall, output: str = "Sol, 31 °C") -> None:
        call._on_message(json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": output}}))

    # -- receipt order ---------------------------------------------------------------------

    def test_receipt_puts_the_caller_before_the_dot(self):
        # The reply streams at once; the caller's transcript takes half a second.
        call = self.make_call(transcribe_delay=0.5)
        call._caller_finished(tone(0.6))
        self.wait(call, "response.done")
        self.assertEqual(call.receipt(), ["You: fala de 0.6 s", "Dot: Olá, tudo bem?"])
        self.assertLess(call.first(TRANSCRIBED)["at"], call.first(SPOKEN)["at"])

    def test_a_late_transcript_does_not_hold_the_reply(self):
        call = self.make_call(transcribe_delay=1.5)
        call.transcript_wait = 0.3
        call._caller_finished(tone(0.6))
        self.wait(call, "response.done")
        waited = call.first(SPOKEN)["at"] - call.first("response.created")["at"]
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 1.2)                 # gave up after ~0.3 s, not 1.5 s
        self.assertEqual(call.count(TRANSCRIBED), 0)
        # …and the caller's words are still sent the moment they arrive.
        self.wait(call, TRANSCRIBED, timeout=5)
        self.assertEqual(call.receipt(), ["Dot: Olá, tudo bem?", "You: fala de 0.6 s"])

    # -- speech during ask_compute ---------------------------------------------------------

    def test_speech_during_compute_is_queued_and_answered_with_the_result(self):
        call = self.make_call(tool_first=True)
        call._caller_finished(tone(0.5))
        self.wait(call, "response.function_call_arguments.done")
        self.assertTrue(call.waiting_tool)
        for seconds in (0.6, 0.7, 0.8, 0.9):     # one more than the queue keeps
            call._caller_finished(tone(seconds))
        self.wait(call, TRANSCRIBED, count=5)
        self.assertEqual(call.count("input_audio_buffer.speech_stopped"), 5)
        self.assertEqual(len(self.streamed()), 1)    # nothing answered meanwhile
        self.assertEqual(call.count("error"), 0)

        self.tool_output(call)
        call._on_message(json.dumps({"type": "response.create"}))
        self.wait(call, "response.done", count=2)
        second = self.streamed()[1]["messages"]
        self.assertEqual(second[1], {"role": "user", "content": "fala de 0.5 s"})
        self.assertEqual(second[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(second[3], {"role": "tool", "tool_call_id": "call_1",
                                     "content": "Sol, 31 °C"})
        # The oldest of the four utterances made room for the newest three.
        self.assertEqual(second[4:], [{"role": "user", "content": f"fala de {s} s"}
                                      for s in ("0.7", "0.8", "0.9")])
        self.assertFalse(call.waiting_tool)
        run_until(lambda: False, call.follow_up_delay + 0.3)
        self.assertEqual(len(self.streamed()), 2)    # answered once, not twice

    def test_queued_speech_is_answered_even_without_response_create(self):
        call = self.make_call(tool_first=True)
        call._caller_finished(tone(0.5))
        self.wait(call, "response.function_call_arguments.done")
        call._caller_finished(tone(0.7))
        self.tool_output(call)
        self.assertTrue(run_until(lambda: len(self.streamed()) == 2, 5))
        second = self.streamed()[1]["messages"]
        self.assertEqual(second[-2]["role"], "tool")
        self.assertEqual(second[-1]["role"], "user")

    def test_response_create_waits_for_the_tool_result(self):
        call = self.make_call(tool_first=True)
        call._caller_finished(tone(0.5))
        self.wait(call, "response.function_call_arguments.done")
        call._on_message(json.dumps({"type": "response.create"}))
        run_until(lambda: False, 0.3)
        self.assertEqual(len(self.streamed()), 1)
        self.tool_output(call)
        self.assertTrue(run_until(lambda: len(self.streamed()) == 2, 5))
        self.assertEqual(self.streamed()[1]["messages"][-1]["role"], "tool")
        # A result for a call that is not pending is ignored (it would break the history).
        self.tool_output(call, "again")
        self.assertEqual(sum(1 for m in call.messages if m["role"] == "tool"), 1)

    def test_compute_that_never_returns_does_not_mute_the_call(self):
        call = self.make_call(tool_first=True)
        call.tool_timeout = 0.2
        call._caller_finished(tone(0.5))
        self.wait(call, "response.function_call_arguments.done")
        run_until(lambda: False, 0.3)
        call._caller_finished(tone(0.7))
        self.assertTrue(run_until(lambda: len(self.streamed()) == 2, 5))
        second = self.streamed()[1]["messages"]
        self.assertEqual(second[-2], {"role": "tool", "tool_call_id": "call_1",
                                      "content": "No result: the request timed out."})
        self.assertEqual(second[-1]["role"], "user")

    # -- voice changes ---------------------------------------------------------------------

    def test_session_update_changes_the_voice_of_later_replies(self):
        call = self.make_call()
        call._session_created()
        self.assertEqual(call.first("session.created")["session"]["voice"], "marin")
        call._on_message(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "audio": {"output": {"voice": "coral"}}}}))
        updated = call.last("session.updated")["session"]
        self.assertEqual((updated["voice"], updated["audio"]["output"]["voice"]),
                         ("coral", "coral"))
        call._caller_finished(tone(0.5))
        self.wait(call, "response.done")
        self.assertEqual(self.streamed()[0]["audio"], {"voice": "coral", "format": "pcm16"})

        # Unknown voices are ignored (the confirmation shows what stays in use), and nothing
        # but the voice can be changed from the data channel.
        call._on_message(json.dumps({"type": "session.update", "session": {
            "audio": {"output": {"voice": "robot"}}, "instructions": "Be someone else."}}))
        self.assertEqual(call.last("session.updated")["session"]["voice"], "coral")
        self.assertEqual(call.messages[0], {"role": "system", "content": "You are Chefe."})
        call._on_message(json.dumps({"type": "session.update", "session": {"voice": 7}}))
        self.assertEqual(call.voice, "coral")

        # The beta shape (``session.voice``) works too.
        call._on_message(json.dumps({"type": "session.update", "session": {"voice": "Nova"}}))
        self.assertEqual(call.last("session.updated")["session"]["voice"], "nova")
        call._caller_finished(tone(0.6))
        self.wait(call, "response.done", count=2)
        self.assertEqual(self.streamed()[1]["audio"]["voice"], "nova")

    def test_forced_voice_wins_over_session_update(self):
        call = self.make_call(voice_override="sage")
        call._on_message(json.dumps({"type": "session.update", "session": {
            "audio": {"output": {"voice": "coral"}}}}))
        self.assertEqual(call.last("session.updated")["session"]["voice"], "sage")
        call._caller_finished(tone(0.5))
        self.wait(call, "response.done")
        self.assertEqual(self.streamed()[0]["audio"]["voice"], "sage")

    # -- failures and watchdogs ------------------------------------------------------------

    def test_a_reply_that_breaks_is_reported_and_the_call_carries_on(self):
        call = self.make_call()
        logs = collect_logs(self)
        self.fake.break_next = "drop"
        call._caller_finished(tone(0.5))
        self.wait(call, "error")
        self.assertIn("dropped during the reply", call.first("error")["error"]["message"])
        self.assertTrue(run_until(lambda: not call._responding, 2))
        self.assertTrue(any(line.startswith("WARNING") and "dropped" in line for line in logs))
        call._caller_finished(tone(0.6))             # the next utterance is answered
        self.wait(call, "response.done")
        self.assertEqual(call.count("error"), 1)

    def test_a_reply_that_fails_unexpectedly_does_not_leave_the_call_stuck(self):
        call = self.make_call()
        logs = collect_logs(self)
        original = call.provider.stream_turn
        calls = []

        def broken_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("a bug")
            return original(*args)

        call.provider.stream_turn = broken_once
        call._caller_finished(tone(0.5))
        self.wait(call, "error")
        self.assertEqual(call.first("error")["error"]["message"],
                         "The voice gateway could not produce a reply.")
        self.assertTrue(any(line.startswith("ERROR") for line in logs), logs)
        self.assertTrue(run_until(lambda: not call._responding, 2))
        call._on_message(json.dumps({"type": "response.create"}))
        self.wait(call, "response.done")

    def test_a_call_that_never_connects_is_closed(self):
        call = self.make_call()
        closed: list[str] = []
        call._on_closed = closed.append
        call.connect_timeout = 0.2
        call._start_watchdogs()
        self.assertTrue(run_until(lambda: closed, 3))
        self.assertEqual((call.close_reason, closed), ("never connected", ["rtc_test"]))

    def test_a_connected_call_is_closed_at_the_time_limit(self):
        call = self.make_call()
        call.connect_timeout, call.max_duration = 0.1, 0.5
        call.connected = True
        call._start_watchdogs()
        run_until(lambda: False, 0.3)
        self.assertFalse(call.closed)                # connected: the first check passes
        self.assertTrue(run_until(lambda: call.closed, 3))
        self.assertEqual(call.close_reason, "time limit")

    def test_a_closed_call_is_freed(self):
        from voice_gateway.config import Config
        call = Call("rtc_gc", {}, Config(token=TOKEN, api_key=SECRET_KEY, port=0),
                    OpenRouter(SECRET_KEY, "http://127.0.0.1:9"), lambda _id: None)
        call._start_watchdogs()
        freed = weakref.ref(call)
        call.close("hangup")
        del call
        self.assertTrue(run_until(lambda: gc.collect() >= 0 and freed() is None, 5))

    def test_closing_cancels_every_timer(self):
        call = self.make_call()
        call._start_watchdogs()
        timers = list(call._timers)
        self.assertEqual(len(timers), 2)
        call.close("hangup")
        self.assertTrue(run_until(lambda: not any(t.is_alive() for t in timers), 2))
        self.assertEqual((call.close_reason, call._timers), ("hangup", []))
        call._later(0.01, call.close, "late")        # nothing is scheduled after the end
        self.assertEqual(call._timers, [])

    # -- logging ---------------------------------------------------------------------------

    def test_the_end_of_a_call_is_one_log_line_with_duration_and_cost(self):
        call = self.make_call()
        logs = collect_logs(self)
        call._caller_finished(tone(0.5))
        self.wait(call, "response.done")
        self.wait(call, TRANSCRIBED)
        call.close("hangup")
        call.close("connection closed")              # a second close changes nothing
        ended = [line for line in logs if "ended" in line]
        self.assertEqual(len(ended), 1, logs)
        self.assertRegex(ended[0], r"^INFO voice_gateway\.call: Call rtc_test ended \(hangup\): "
                                   r"\d+ s, 1 reply, 0 compute requests, US\$ 0\.00021$")
        self.assertFalse(any(SECRET_KEY in line or TOKEN in line for line in logs))
        self.assertEqual(call.receipt(), ["You: fala de 0.5 s", "Dot: Olá, tudo bem?"])


class InstallScriptTest(unittest.TestCase):
    """server/install.sh with a throwaway HOME; systemctl, curl, npm… are fakes that only log."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dotdeck-install-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.opendots = self.home / "github" / "OpenDots"
        (self.opendots / ".git").mkdir(parents=True)
        (self.opendots / "src" / "server").mkdir(parents=True)
        (self.opendots / "src" / "server" / "index.ts").write_text(
            "const url = process.env.VOICE_REALTIME_URL;\n")
        self.env_file = self.opendots / ".env"
        self.env_file.write_text("OWNER_TOKEN=owner-token-for-tests-0123456789\nPORT=4310\n"
                                 "OPENAI_BASE_URL=https://openrouter.ai/api/v1\n"
                                 f"OPENAI_API_KEY={SECRET_KEY}\n")
        self.health = self.root / "health.json"
        self.health.write_text('{"ok": true, "calls": 0, "setup": {"voice": true}}')
        self.commands = self.root / "commands.log"
        self.commands.touch()
        self.proc = self.root / "proc"           # what stray gateways' /proc/PID/cgroup say
        self.proc.mkdir()
        self.strays = self.root / "strays"       # what pgrep finds
        self.strays.write_text("")
        fakes = self.root / "bin"
        fakes.mkdir()
        for name in ("systemctl", "npm", "node", "git", "flatpak", "sleep"):
            self.fake(fakes / name, f'echo "{name} $*" >> "{self.commands}"')
        self.fake(fakes / "pgrep", f'echo "pgrep $*" >> "{self.commands}"\n'
                  f'cat "{self.strays}"; test -s "{self.strays}"')
        self.fake(fakes / "curl", f'echo "curl $*" >> "{self.commands}"\n'
                  'case " $* " in *" -w "*) printf 200 ;; '
                  f'*) cat "{self.health}" ;; esac')
        self.path = f"{fakes}:/usr/bin:/bin"
        self.share = self.home / ".local" / "share" / "dotdeck-server"
        self.unit = self.home / ".config" / "systemd" / "user" / "opendots-voice-gateway.service"
        self.gateway_env = self.home / ".config" / "dotdeck-server" / "voice-gateway.env"

    @staticmethod
    def fake(path: Path, body: str) -> None:
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": self.path, "LANG": "C.UTF-8",
               "LC_ALL": "C.UTF-8", "PROC_ROOT": str(self.proc)}
        return subprocess.run(["bash", str(SERVER / "install.sh"), *args,
                               "--opendots", str(self.opendots)],
                              env=env, capture_output=True, text=True, timeout=60)

    def ran(self, tool: str) -> list[str]:
        return [line.split(" ", 1)[1] for line in self.commands.read_text().splitlines()
                if line.split(" ", 1)[0] == tool]

    def test_help_and_bad_options(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("Install the Dotdeck server companions"))
        self.assertIn("--update [--apply]", result.stdout)
        self.assertNotIn("set -euo", result.stdout)
        self.assertNotIn("#", result.stdout)
        for args in (("--voice",), ("--model", ""), ("--bogus",)):
            result = subprocess.run(["bash", str(SERVER / "install.sh"), *args],
                                    env={"HOME": str(self.home), "PATH": self.path},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, args)
        self.assertEqual(self.commands.read_text(), "")

    def test_dry_run_changes_nothing(self):
        before = self.env_file.read_text()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Nada foi alterado", result.stdout)
        self.assertEqual(self.env_file.read_text(), before)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.share.exists())
        self.assertEqual(self.ran("systemctl") + self.ran("npm"), [])

    def test_apply_writes_a_unit_that_logs_under_its_own_name(self):
        result = self.run_script("--apply", "--no-updater")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit = self.unit.read_text()
        self.assertIn("\nSyslogIdentifier=opendots-voice-gateway\n", unit)
        # Stopping the service must stop the sandbox, which flatpak puts in its own scope.
        self.assertIn("\nExecStart=/usr/bin/flatpak run --die-with-parent ", unit)
        self.assertIn(f"--filesystem={self.share}:ro", unit)
        self.assertIn(f"EnvironmentFile={self.gateway_env}\n", unit)
        self.assertTrue((self.share / "voice_gateway" / "call.py").is_file())
        self.assertFalse(list(self.share.rglob("__pycache__")))
        self.assertEqual(self.gateway_env.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"OPENROUTER_API_KEY={SECRET_KEY}\n", self.gateway_env.read_text())
        self.assertIn("VOICE_REALTIME_URL=http://127.0.0.1:4320/v1/realtime/calls",
                      self.env_file.read_text())
        systemctl = self.ran("systemctl")
        self.assertIn("--user enable opendots-voice-gateway.service", systemctl)
        start = systemctl.index("--user start opendots-voice-gateway.service")
        self.assertEqual(systemctl[start - 1], "--user stop opendots-voice-gateway.service")
        self.assertIn("journalctl --user -t opendots-voice-gateway -f", result.stdout)
        self.assertNotIn(SECRET_KEY, result.stdout + result.stderr)
        self.assertNotIn("owner-token-for-tests", result.stdout + result.stderr)

    def installed(self) -> str:
        """An installation from before 0.3: no SyslogIdentifier, stale code."""
        (self.share / "voice_gateway").mkdir(parents=True)
        (self.share / "voice_gateway" / "stale.py").write_text("old = True\n")
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\nExecStart=/usr/bin/flatpak run old\n")
        self.gateway_env.parent.mkdir(parents=True)
        self.gateway_env.write_text(f"VOICE_GATEWAY_TOKEN={TOKEN}\n")
        return self.env_file.read_text()

    def test_update_refreshes_only_the_gateway(self):
        env_before = self.installed()
        dry = self.run_script("--update")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertNotIn("SyslogIdentifier", self.unit.read_text())
        self.assertEqual(self.ran("systemctl"), [])

        result = self.run_script("--update", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("\nSyslogIdentifier=opendots-voice-gateway\n", self.unit.read_text())
        self.assertTrue((self.share / "voice_gateway" / "call.py").is_file())
        self.assertFalse((self.share / "voice_gateway" / "stale.py").exists())
        self.assertFalse(list(self.share.rglob("__pycache__")))
        self.assertFalse((self.share / "updater").exists())
        self.assertEqual(self.env_file.read_text(), env_before)       # OpenDots untouched
        self.assertEqual(self.gateway_env.read_text(), f"VOICE_GATEWAY_TOKEN={TOKEN}\n")
        self.assertEqual(self.ran("systemctl"), [
            "--user daemon-reload", "--user stop opendots-voice-gateway.service",
            "--user start opendots-voice-gateway.service"])
        self.assertEqual(self.ran("npm") + self.ran("git"), [])
        backups = list((self.home / "work" / "backups").glob("*/opendots-voice-gateway.service*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("flatpak run old", backups[0].read_text())
        # The code that was running is kept too, for a rollback.
        old_code = backups[0].parent / "voice_gateway.before"
        self.assertEqual([p.name for p in old_code.iterdir()], ["stale.py"])
        self.assertEqual(backups[0].parent.stat().st_mode & 0o777, 0o700)

    def test_update_stops_gateways_left_outside_the_service(self):
        # A 0.2 unit (no --die-with-parent) leaves the sandbox running after a stop; it
        # would keep port 4320 and answer /health with the old code.
        self.installed()
        self.strays.write_text("4242\n4343\n")
        for pid, cgroup in (("4242", "app-flatpak-org.gnome.Platform-3087338353.scope"),
                            ("4343", "session-2.scope")):
            (self.proc / pid).mkdir()
            (self.proc / pid / "cgroup").write_text(
                f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{cgroup}\n")
        result = self.run_script("--update", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran("systemctl"), [
            "--user daemon-reload", "--user stop opendots-voice-gateway.service",
            "--user stop app-flatpak-org.gnome.Platform-3087338353.scope",
            "--user start opendots-voice-gateway.service"])
        self.assertIn("pare com: kill 4343", result.stdout)    # not a flatpak scope: told
        self.assertIn("-f ^python3 -m voice_gateway$", self.ran("pgrep")[0])

    def test_update_waits_for_calls_to_end(self):
        self.installed()
        self.health.write_text('{"ok": true, "calls": 1}')
        result = self.run_script("--update", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("chamada(s) em andamento", result.stdout)
        self.assertTrue((self.share / "voice_gateway" / "stale.py").exists())
        self.assertEqual(self.ran("systemctl"), [])

    def test_update_needs_an_installed_gateway(self):
        result = self.run_script("--update", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("não está instalada", result.stdout)
        self.assertFalse(self.share.exists())


if __name__ == "__main__":
    unittest.main()
