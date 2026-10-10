"""One voice call: the WebRTC peer that stands in for the OpenAI Realtime endpoint.

The caller (the OpenDots page, in a browser or in Dotdeck) negotiates WebRTC and talks to
us exactly as it would to OpenAI: Opus audio both ways and Realtime events on the
``oai-events`` data channel. Underneath, every turn is one streaming chat completion with
an audio-capable model on OpenRouter: we detect when the caller stops talking, send the
utterance, play the spoken reply as it streams and relay ``ask_compute`` tool calls.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import secrets
import threading
import time
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402

from . import vad as vadmod  # noqa: E402
from .config import Config  # noqa: E402
from .openrouter import OUTPUT_RATE, OpenRouter, ProviderError, wav_bytes  # noqa: E402

log = logging.getLogger(__name__)

VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage",
          "shimmer", "verse", "marin", "cedar"}
CHUNK_MS = 40
AHEAD_NS = 200 * Gst.MSECOND


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def opus_payload_type(sdp: str) -> int:
    match = re.search(r"^a=rtpmap:(\d+) opus/48000", sdp, re.M | re.I)
    return int(match.group(1)) if match else 111


def chat_tools(session: dict) -> list[dict]:
    """Realtime tool definitions → chat-completions tool definitions."""
    tools = []
    for tool in session.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name"):
            tools.append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}})
    return tools


class Call:
    def __init__(self, call_id: str, session: dict, config: Config, provider: OpenRouter,
                 on_closed: Callable[[str], None]):
        self.id = call_id
        self.config = config
        self.provider = provider
        self._on_closed = on_closed
        self.closed = False
        self.started = time.monotonic()
        self.cost = 0.0
        self._lock = threading.RLock()

        model = str(session.get("model") or "")
        self.model = model if "/" in model else config.model
        voice = str(((session.get("audio") or {}).get("output") or {}).get("voice") or "")
        self.voice = config.voice_override or (voice if voice in VOICES else config.voice)
        self.tools = chat_tools(session)
        self.messages: list[dict] = [{"role": "system", "content": str(
            session.get("instructions") or "You are a helpful voice assistant. Keep answers short.")}]

        self.vad = vadmod.Vad(stop_ms=config.vad_stop_ms)
        self._channel = None
        self._turn_cancel: threading.Event | None = None
        self._responding = False
        self._waiting_tool = False
        self._speaker: queue.Queue = queue.Queue()
        self._generation = 0
        self._speaker_stop = threading.Event()
        self._speaking_until = 0
        self._next_pts = 0
        self._answer: str | None = None
        self._answer_ready = threading.Event()

        self.pipeline = Gst.Pipeline.new(f"gateway-{call_id}")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.pipeline.add(self.webrtc)
        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("on-data-channel", self._on_data_channel)
        self.webrtc.connect("notify::connection-state", self._on_state)
        self.webrtc.connect("notify::ice-gathering-state", self._on_gathering)
        self._feeder = threading.Thread(target=self._feed_speaker, daemon=True)

    # -- negotiation -----------------------------------------------------------------------

    def answer(self, offer: str, timeout: float = 10.0) -> str:
        """Answer the caller's offer; the answer carries all our ICE candidates."""
        pt = opus_payload_type(offer)
        # The reply audio is one continuous stream: the feeder thread pushes silence while
        # the Dot is quiet, so the caller always receives audio in real time.
        self._tts = Gst.ElementFactory.make("appsrc", "tts")
        self._tts.set_property("caps", Gst.Caps.from_string(
            f"audio/x-raw,format=S16LE,rate={OUTPUT_RATE},channels=1,layout=interleaved"))
        self._tts.set_property("format", Gst.Format.TIME)
        self._tts.set_property("is-live", True)
        send = Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! "
            "capsfilter caps=\"audio/x-raw,format=S16LE,rate=48000,channels=1\" ! "
            f"opusenc ! rtpopuspay pt={pt} ! capsfilter caps=\"application/x-rtp,media=audio,"
            f"encoding-name=OPUS,payload={pt},clock-rate=48000\"", True)
        self.pipeline.add(self._tts)
        self.pipeline.add(send)
        self._tts.link(send)
        send.link(self.webrtc)
        self.pipeline.set_state(Gst.State.PLAYING)

        result, message = GstSdp.SDPMessage.new_from_text(offer)
        if result != GstSdp.SDPResult.OK:
            raise ValueError("The offer is not valid SDP.")
        description = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER,
                                                              message)
        done = threading.Event()
        self.webrtc.emit("set-remote-description", description,
                         Gst.Promise.new_with_change_func(lambda _p: done.set()))
        if not done.wait(timeout):
            raise TimeoutError("Timed out applying the offer.")
        answered = threading.Event()
        box = {}

        def on_answer(promise):
            # The description belongs to the promise's reply: copy it out as text now.
            reply = promise.get_reply()
            if reply is not None and reply.has_field("answer"):
                box["sdp"] = reply.get_value("answer").sdp.as_text()
            answered.set()

        self.webrtc.emit("create-answer", None, Gst.Promise.new_with_change_func(on_answer))
        if not answered.wait(timeout) or not box.get("sdp"):
            raise RuntimeError("Could not create the answer.")
        _result, answer = GstSdp.SDPMessage.new_from_text(box["sdp"])
        local = threading.Event()
        self.webrtc.emit("set-local-description",
                         GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER,
                                                                answer),
                         Gst.Promise.new_with_change_func(lambda _p: local.set()))
        local.wait(timeout)
        self._answer_ready.wait(timeout)          # ICE gathering complete
        description = self.webrtc.props.local_description
        self._feeder.start()
        return description.sdp.as_text()

    def _on_gathering(self, webrtc, _pspec) -> None:
        if webrtc.props.ice_gathering_state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._answer_ready.set()

    def _on_state(self, webrtc, _pspec) -> None:
        state = webrtc.props.connection_state
        if state in (GstWebRTC.WebRTCPeerConnectionState.FAILED,
                     GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            log.info("Call %s: connection %s", self.id, state.value_nick)
            threading.Thread(target=self.close, daemon=True).start()

    # -- caller audio ----------------------------------------------------------------------

    def _on_pad_added(self, _webrtc, pad: Gst.Pad) -> None:
        if pad.get_direction() != Gst.PadDirection.SRC or self.closed:
            return
        listen = Gst.parse_bin_from_description(
            "queue ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=S16LE,rate={vadmod.RATE},channels=1,layout=interleaved ! "
            "appsink name=ear emit-signals=true sync=false max-buffers=50 drop=false", True)
        self.pipeline.add(listen)
        listen.get_by_name("ear").connect("new-sample", self._on_sample)
        if pad.link(listen.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            log.warning("Call %s: could not link the caller's audio", self.id)
            return
        listen.sync_state_with_parent()

    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        pcm = bytes(info.data)
        buffer.unmap(info)
        if log.isEnabledFor(logging.DEBUG):
            self._frames = getattr(self, "_frames", 0) + 1
            if self._frames % 25 == 0:
                log.debug("call %s: %d bytes, level %.1f dB, floor %.1f, threshold %.1f, "
                          "speaking=%s", self.id, len(pcm), vadmod.frame_db(pcm[-640:]),
                          self.vad.floor_db, self.vad.threshold_db, self.vad.speaking)
        self.vad.bias_db = 6.0 if self._playing() else 0.0
        for event in self.vad.feed(pcm):
            if event[0] == vadmod.START:
                self._caller_started()
            elif event[0] == vadmod.END and event[1]:
                self._caller_finished(event[1])
        return Gst.FlowReturn.OK

    def _caller_started(self) -> None:
        self.send({"type": "input_audio_buffer.speech_started", "item_id": _id("item")})
        if self._responding or self._playing():
            self._interrupt()

    def _caller_finished(self, pcm: bytes) -> None:
        item = _id("item")
        self.send({"type": "input_audio_buffer.speech_stopped", "item_id": item})
        if self._waiting_tool:
            return  # the Dot is busy with a compute request; the caller can repeat after
        audio = base64.b64encode(wav_bytes(pcm, vadmod.RATE)).decode()
        message = {"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}]}
        with self._lock:
            self.messages.append(message)
        if self.config.transcribe_model:
            threading.Thread(target=self._transcribe, args=(message, item, pcm),
                             daemon=True).start()
        self._respond()

    def _transcribe(self, message: dict, item: str, pcm: bytes) -> None:
        try:
            text = self.provider.transcribe(self.config.transcribe_model, pcm, vadmod.RATE)
        except ProviderError as error:
            log.info("Call %s: transcription failed: %s", self.id, error)
            return
        if not text:
            return
        with self._lock:
            # Later turns carry the words, not the audio again.
            message["content"] = text
        self.send({"type": "conversation.item.input_audio_transcription.completed",
                   "item_id": item, "content_index": 0, "transcript": text})

    # -- replies ---------------------------------------------------------------------------

    def _respond(self) -> None:
        with self._lock:
            if self.closed:
                return
            if self._turn_cancel is not None:
                self._turn_cancel.set()
            cancel = threading.Event()
            self._turn_cancel = cancel
            self._responding = True
            messages = [dict(m) for m in self.messages]
        threading.Thread(target=self._run_turn, args=(messages, cancel), daemon=True).start()

    def _run_turn(self, messages: list[dict], cancel: threading.Event) -> None:
        response_id, item = _id("resp"), _id("item")
        self.send({"type": "response.created", "response": {"id": response_id,
                                                             "status": "in_progress"}})

        def transcript(delta: str) -> None:
            if not cancel.is_set():
                self.send({"type": "response.output_audio_transcript.delta",
                           "response_id": response_id, "item_id": item, "delta": delta})

        generation = self._generation

        def audio(pcm: bytes) -> None:
            if not cancel.is_set():
                self._speaker.put((generation, pcm))

        try:
            result = self.provider.stream_turn(self.model, messages, self.voice, self.tools,
                                               transcript, audio, cancel)
        except ProviderError as error:
            log.warning("Call %s: %s", self.id, error)
            self.send({"type": "error", "error": {"type": "provider_error",
                                                  "message": str(error)}})
            with self._lock:
                self._responding = False
            return
        self.cost += result.cost
        if result.cancelled or cancel.is_set():
            return
        with self._lock:
            self._responding = False
            if result.tool_calls:
                self._waiting_tool = True
                self.messages.append({"role": "assistant", "content": result.transcript or None,
                                      "tool_calls": [{"id": c["id"], "type": "function",
                                                      "function": {"name": c["name"],
                                                                   "arguments": c["arguments"]}}
                                                     for c in result.tool_calls]})
            elif result.transcript:
                self.messages.append({"role": "assistant", "content": result.transcript})
        if result.transcript:
            self.send({"type": "response.output_audio_transcript.done",
                       "response_id": response_id, "item_id": item,
                       "transcript": result.transcript})
        for call in result.tool_calls:
            self.send({"type": "response.function_call_arguments.done",
                       "response_id": response_id, "item_id": _id("item"),
                       "call_id": call["id"], "name": call["name"],
                       "arguments": call["arguments"] or "{}"})
        self.send({"type": "response.done", "response": {
            "id": response_id, "status": "completed",
            "usage": {"cost": round(result.cost, 6)}}})

    def _interrupt(self) -> None:
        with self._lock:
            if self._turn_cancel is not None:
                self._turn_cancel.set()
            self._responding = False
        self._clear_speaker()

    # -- data channel ----------------------------------------------------------------------

    def _on_data_channel(self, _webrtc, channel) -> None:
        self._channel = channel
        channel.connect("on-message-string", lambda _c, text: self._on_message(text))
        channel.connect("on-open", lambda _c: self._session_created())
        if channel.props.ready_state == GstWebRTC.WebRTCDataChannelState.OPEN:
            self._session_created()

    def _session_created(self) -> None:
        if getattr(self, "_announced_session", False):
            return
        self._announced_session = True
        self.send({"type": "session.created", "session": {
            "id": self.id, "object": "realtime.session", "model": self.model,
            "voice": self.voice, "output_modalities": ["audio"]}})

    def _on_message(self, text: str) -> None:
        try:
            event = json.loads(text)
        except ValueError:
            return
        kind = event.get("type")
        if kind == "conversation.item.create":
            item = event.get("item") or {}
            if item.get("type") == "function_call_output":
                with self._lock:
                    self.messages.append({"role": "tool", "tool_call_id": item.get("call_id"),
                                          "content": str(item.get("output", ""))})
                    self._waiting_tool = False
            elif item.get("type") == "message":
                parts = [c.get("text", "") for c in item.get("content") or []
                         if isinstance(c, dict)]
                with self._lock:
                    self.messages.append({"role": item.get("role", "user"),
                                          "content": " ".join(p for p in parts if p)})
        elif kind == "response.create":
            self._respond()
        elif kind == "response.cancel":
            self._interrupt()

    def send(self, event: dict) -> None:
        channel = self._channel
        if channel is None or self.closed:
            return
        if channel.props.ready_state != GstWebRTC.WebRTCDataChannelState.OPEN:
            return
        event.setdefault("event_id", _id("evt"))
        try:
            channel.send_string(json.dumps(event))
        except Exception as error:  # channel closing under us
            log.debug("Call %s: could not send %s: %s", self.id, event.get("type"), error)

    # -- speaker ---------------------------------------------------------------------------

    def _running_time(self) -> int:
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return clock.get_time() - self.pipeline.get_base_time()

    def _playing(self) -> bool:
        return not self._speaker.empty() or self._running_time() < self._speaking_until

    def _clear_speaker(self) -> None:
        with self._lock:
            self._generation += 1
            try:
                while True:
                    self._speaker.get_nowait()
            except queue.Empty:
                pass
            was_playing = self._running_time() < self._speaking_until
            self._speaking_until = 0
            self._next_pts = 0
        if was_playing:
            self.send({"type": "output_audio_buffer.cleared"})

    def _feed_speaker(self) -> None:
        """Push reply audio (or silence) to the call in real time, a little ahead of the clock.

        Each queued item is ``(generation, pcm)``; an interruption bumps the generation, so
        audio from before it is dropped even if it was already taken from the queue.
        """
        chunk = OUTPUT_RATE * 2 * CHUNK_MS // 1000
        quiet = b"\0" * chunk
        pending, generation = b"", -1
        announced = False
        while not self._speaker_stop.is_set():
            now = self._running_time()
            if self._next_pts < now:
                self._next_pts = now + 20 * Gst.MSECOND
            ahead = self._next_pts - now
            if ahead > AHEAD_NS:
                time.sleep(min(0.05, (ahead - AHEAD_NS) / Gst.SECOND))
                continue
            while len(pending) < chunk:
                try:
                    item_generation, pcm = self._speaker.get_nowait()
                except queue.Empty:
                    break
                if item_generation != generation:
                    pending, generation = b"", item_generation
                pending += pcm
            if pending and generation != self._generation:
                pending = b""
            if pending:
                piece, pending = pending[:chunk].ljust(chunk, b"\0"), pending[chunk:]
                if not announced:
                    announced = True
                    self.send({"type": "output_audio_buffer.started"})
            else:
                piece = quiet
            buffer = Gst.Buffer.new_wrapped(piece)
            buffer.pts = self._next_pts
            buffer.duration = CHUNK_MS * Gst.MSECOND
            self._next_pts += buffer.duration
            if piece is not quiet:
                self._speaking_until = self._next_pts
            elif announced and now >= self._speaking_until:
                announced = False
                self.send({"type": "output_audio_buffer.stopped"})
            self._tts.emit("push-buffer", buffer)

    # -- teardown --------------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            if self._turn_cancel is not None:
                self._turn_cancel.set()
        self._speaker_stop.set()
        self.pipeline.set_state(Gst.State.NULL)
        log.info("Call %s closed after %.0f s, cost US$ %.5f", self.id,
                 time.monotonic() - self.started, self.cost)
        self._on_closed(self.id)
