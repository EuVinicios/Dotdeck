"""One voice call: the WebRTC peer that stands in for the OpenAI Realtime endpoint.

The caller (the OpenDots page, in a browser or in Dotdeck) negotiates WebRTC and talks to
us exactly as it would to OpenAI: Opus audio both ways and Realtime events on the
``oai-events`` data channel. Underneath, every turn is one streaming chat completion with
an audio-capable model on OpenRouter: we detect when the caller stops talking, send the
utterance, play the spoken reply as it streams and relay ``ask_compute`` tool calls.

Two details keep the call receipt (built by the caller from our events) faithful:

* the caller's words (``conversation.item.input_audio_transcription.completed``) are
  transcribed in parallel with the reply, and a reply waits a moment for them before its
  ``response.output_audio_transcript.done``, so the receipt reads "You" before "Dot";
* what the caller says while an ``ask_compute`` request is pending is queued (up to
  ``queue_limit`` utterances) and answered together with the compute result.
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
from collections import deque
from typing import Any, Callable

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


def _dig(data: Any, *keys: str) -> Any:
    """``data[k1][k2]…`` for nested dicts, or None as soon as something is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def session_voice(session: Any) -> Any:
    """The output voice a Realtime session asks for (GA ``audio.output.voice`` or beta
    ``voice``), or None when it does not mention one."""
    voice = _dig(session, "audio", "output", "voice")
    return voice if voice is not None else _dig(session, "voice")


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


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


class Call:
    # How long a finished reply waits for the caller's transcript before its
    # ``response.output_audio_transcript.done`` (keeps "You" before "Dot" in the receipt).
    transcript_wait = 1.5
    # Utterances kept while an ask_compute request is pending (the oldest goes first).
    queue_limit = 3
    # After a compute result, answer the queued utterances ourselves if the caller does not
    # send ``response.create`` within this many seconds.
    follow_up_delay = 0.8
    # A compute result that has not come back after this long (OpenDots gives up after
    # 90 s) no longer holds the call: the next utterance is answered without it.
    tool_timeout = 150.0
    # A caller that vanished must not hold a call slot (and its pipeline) forever: a call
    # that has not connected after ``connect_timeout`` (OpenDots gives up after 30 s) or
    # that outlives ``max_duration`` (OpenDots ends calls at 15 min) is closed here.
    connect_timeout = 60.0
    max_duration = 20 * 60.0

    def __init__(self, call_id: str, session: dict, config: Config, provider: OpenRouter,
                 on_closed: Callable[[str], None]):
        self.id = call_id
        self.config = config
        self.provider = provider
        self._on_closed = on_closed
        self.closed = False
        self.close_reason = ""
        self.connected = False
        self.started = time.monotonic()
        self.cost = 0.0
        self.replies = 0
        self.compute_requests = 0
        self._lock = threading.RLock()

        model = str(session.get("model") or "")
        self.model = model if "/" in model else config.model
        voice = session_voice(session)
        self.voice = config.voice_override or (voice if voice in VOICES else config.voice)
        self.tools = chat_tools(session)
        instructions = session.get("instructions") or \
            "You are a helpful voice assistant. Keep answers short."
        self.messages: list[dict] = [{"role": "system", "content": str(instructions)}]

        self.vad = vadmod.Vad(stop_ms=config.vad_stop_ms)
        self._channel = None
        self._announced_session = False
        self._turn_cancel: threading.Event | None = None
        self._responding = False
        self._turns_started = 0
        self._turn_basis = 0                    # len(messages) when the current turn started
        self._pending_tools: list[str] = []     # tool call ids still waiting for their output
        self._tools_since = 0.0
        self._queued: deque[dict] = deque()     # caller messages held back meanwhile
        self._respond_after_tools = False
        self._unheard: list[threading.Event] = []  # caller transcriptions still running
        self._timers: list[threading.Timer] = []   # cancelled when the call closes
        self._handlers: list[tuple[Any, int]] = []  # signal handlers, disconnected then
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
        self._connect(self.webrtc, "pad-added", self._on_pad_added)
        self._connect(self.webrtc, "on-data-channel", self._on_data_channel)
        self._connect(self.webrtc, "notify::connection-state", self._on_state)
        self._connect(self.webrtc, "notify::ice-gathering-state", self._on_gathering)
        self._feeder = threading.Thread(target=self._feed_speaker, daemon=True)

    def _connect(self, obj: Any, signal: str, callback: Callable[..., Any]) -> bool:
        """``obj.connect`` that ``close()`` undoes. A handler holds the call and the call
        holds the pipeline: without the disconnect, no call would ever be freed."""
        with self._lock:
            if self.closed:
                return False
            self._handlers.append((obj, obj.connect(signal, callback)))
        return True

    @property
    def waiting_tool(self) -> bool:
        """True while an ask_compute (or other tool) result is pending."""
        return bool(self._pending_tools)

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
        self._start_watchdogs()
        return description.sdp.as_text()

    def _start_watchdogs(self) -> None:
        self._later(self.connect_timeout, self._check_connected)
        self._later(self.max_duration, self.close, "time limit")

    def _later(self, seconds: float, action: Callable[..., None], *args: Any) -> None:
        """Run ``action(*args)`` after ``seconds`` unless the call has closed by then."""
        timer = threading.Timer(seconds, action, args=args)
        timer.daemon = True
        with self._lock:
            if self.closed:
                return
            self._timers = [t for t in self._timers if t.is_alive()]
            self._timers.append(timer)
        timer.start()

    def _check_connected(self) -> None:
        if not self.connected:
            self.close("never connected")

    def _on_gathering(self, webrtc, _pspec) -> None:
        if webrtc.props.ice_gathering_state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._answer_ready.set()

    def _on_state(self, webrtc, _pspec) -> None:
        state = webrtc.props.connection_state
        if state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            self.connected = True
        if state in (GstWebRTC.WebRTCPeerConnectionState.FAILED,
                     GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            reason = f"connection {state.value_nick}"
            threading.Thread(target=self.close, args=(reason,), daemon=True).start()

    # -- caller audio ----------------------------------------------------------------------

    def _on_pad_added(self, _webrtc, pad: Gst.Pad) -> None:
        if pad.get_direction() != Gst.PadDirection.SRC or self.closed:
            return
        listen = Gst.parse_bin_from_description(
            "queue ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=S16LE,rate={vadmod.RATE},channels=1,layout=interleaved ! "
            "appsink name=ear emit-signals=true sync=false max-buffers=50 drop=false", True)
        self.pipeline.add(listen)
        if not self._connect(listen.get_by_name("ear"), "new-sample", self._on_sample):
            return
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
        audio = base64.b64encode(wav_bytes(pcm, vadmod.RATE)).decode()
        message = {"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}]}
        heard = threading.Event() if self.config.transcribe_model else None
        with self._lock:
            if self.closed:
                return
            if heard is not None:
                self._unheard.append(heard)
            queued = self._add_message(message)
        if heard is not None:
            threading.Thread(target=self._transcribe, args=(message, item, pcm, heard),
                             daemon=True).start()
        if not queued:
            self._respond()

    def _add_message(self, message: dict) -> bool:
        """Add a caller message to the conversation, or hold it back while a tool result is
        pending (the chat history must go tool call → tool result → next message).
        Returns True when it was queued. Call with the lock held."""
        if self._pending_tools and time.monotonic() - self._tools_since > self.tool_timeout:
            log.info("Call %s: no compute result after %.0f s; carrying on without it",
                     self.id, time.monotonic() - self._tools_since)
            for call_id in self._pending_tools:
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "content": "No result: the request timed out."})
            self._pending_tools.clear()
            self.messages.extend(self._queued)
            self._queued.clear()
            self._respond_after_tools = False
        if not self._pending_tools:
            self.messages.append(message)
            return False
        if len(self._queued) >= self.queue_limit:
            self._queued.popleft()
            log.info("Call %s: more than %d utterances during a compute request; "
                     "dropped the oldest", self.id, self.queue_limit)
        self._queued.append(message)
        log.debug("Call %s: caller spoke during a compute request; queued (%d)",
                  self.id, len(self._queued))
        return True

    def _transcribe(self, message: dict, item: str, pcm: bytes,
                    heard: threading.Event) -> None:
        try:
            try:
                result = self.provider.transcribe(self.config.transcribe_model, pcm,
                                                  vadmod.RATE)
            except ProviderError as error:
                log.warning("Call %s: transcription failed: %s", self.id, error)
                return
            except Exception:  # a bug must not cost the call: log it, carry on
                log.exception("Call %s: transcription failed unexpectedly", self.id)
                return
            with self._lock:
                self.cost += result.cost
                if result.text:
                    # Later turns carry the words, not the audio again.
                    message["content"] = result.text
            if result.text:
                self.send({"type": "conversation.item.input_audio_transcription.completed",
                           "item_id": item, "content_index": 0, "transcript": result.text})
        finally:
            with self._lock:
                if heard in self._unheard:
                    self._unheard.remove(heard)
            heard.set()

    def _await_caller_words(self, heard: list[threading.Event]) -> None:
        """Give the caller's transcripts (running in parallel) up to ``transcript_wait``
        seconds to be sent first; a late one is still sent the moment it arrives."""
        deadline = time.monotonic() + self.transcript_wait
        for event in heard:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not event.wait(remaining):
                log.debug("Call %s: reply sent before the caller's transcript", self.id)
                return

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
            self._turns_started += 1
            self._turn_basis = len(self.messages)
            messages = [dict(m) for m in self.messages]
            heard = list(self._unheard)
            voice = self.voice
        threading.Thread(target=self._run_turn, args=(messages, voice, heard, cancel),
                         daemon=True).start()

    def _run_turn(self, messages: list[dict], voice: str, heard: list[threading.Event],
                  cancel: threading.Event) -> None:
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

        began = time.monotonic()
        try:
            result = self.provider.stream_turn(self.model, messages, voice, self.tools,
                                               transcript, audio, cancel)
        except Exception as error:  # ProviderError, or a bug: the call must not get stuck
            with self._lock:
                current = self._turn_cancel is cancel and not cancel.is_set()
                if current:
                    self._responding = False
            if not current:
                return
            if isinstance(error, ProviderError):
                log.warning("Call %s: %s", self.id, error)
                message = str(error)
            else:
                log.exception("Call %s: the reply failed unexpectedly", self.id)
                message = "The voice gateway could not produce a reply."
            self.send({"type": "error", "error": {"type": "provider_error",
                                                  "message": message}})
            return
        with self._lock:
            self.cost += result.cost
            if result.cancelled or cancel.is_set():
                return
            self._responding = False
            self.replies += 1
            for call in result.tool_calls:
                call["id"] = call["id"] or _id("call")
            if result.tool_calls:
                self._pending_tools.extend(call["id"] for call in result.tool_calls)
                self._tools_since = time.monotonic()
                self.compute_requests += len(result.tool_calls)
                self.messages.append({"role": "assistant", "content": result.transcript or None,
                                      "tool_calls": [{"id": c["id"], "type": "function",
                                                      "function": {"name": c["name"],
                                                                   "arguments": c["arguments"]}}
                                                     for c in result.tool_calls]})
            elif result.transcript:
                self.messages.append({"role": "assistant", "content": result.transcript})
        log.debug("Call %s: reply in %.2f s (%d chars, %d tool calls, US$ %.5f)", self.id,
                  time.monotonic() - began, len(result.transcript), len(result.tool_calls),
                  result.cost)
        self._await_caller_words(heard)
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
        self._connect(channel, "on-message-string", lambda _c, text: self._on_message(text))
        self._connect(channel, "on-open", lambda _c: self._session_created())
        if channel.props.ready_state == GstWebRTC.WebRTCDataChannelState.OPEN:
            self._session_created()

    def _session_view(self) -> dict:
        return {"id": self.id, "object": "realtime.session", "model": self.model,
                "voice": self.voice, "output_modalities": ["audio"],
                "audio": {"output": {"voice": self.voice}}}

    def _session_created(self) -> None:
        with self._lock:                      # "on-open" and the ready check can race
            if self._announced_session:
                return
            self._announced_session = True
        self.send({"type": "session.created", "session": self._session_view()})

    def _on_message(self, text: str) -> None:
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "conversation.item.create":
            item = event.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "function_call_output":
                self._tool_output(item)
            elif item.get("type") == "message":
                parts = [str(c.get("text") or "") for c in item.get("content") or []
                         if isinstance(c, dict)]
                with self._lock:
                    self._add_message({"role": str(item.get("role") or "user"),
                                       "content": " ".join(p for p in parts if p)})
        elif kind == "response.create":
            with self._lock:
                if self._pending_tools:
                    # Answer once every tool result is in (the history needs them all).
                    self._respond_after_tools = True
                    return
                if self._responding and len(self.messages) == self._turn_basis:
                    return  # already answering exactly this conversation
            self._respond()
        elif kind == "response.cancel":
            self._interrupt()
        elif kind == "session.update":
            self._update_session(event.get("session"))

    def _tool_output(self, item: dict) -> None:
        call_id = item.get("call_id")
        with self._lock:
            if call_id not in self._pending_tools:
                log.debug("Call %s: ignored a result for an unknown tool call", self.id)
                return
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": str(item.get("output", ""))})
            self._pending_tools.remove(call_id)
            if self._pending_tools:
                return
            queued = list(self._queued)
            self._queued.clear()
            self.messages.extend(queued)
            respond = self._respond_after_tools
            self._respond_after_tools = False
            marker = self._turns_started
        if respond:
            self._respond()
        elif queued:
            # The caller normally sends response.create right after the result; if it does
            # not, still answer what was said meanwhile.
            self._later(self.follow_up_delay, self._follow_up, marker)

    def _follow_up(self, marker: int) -> None:
        with self._lock:
            if self.closed or self._pending_tools or self._turns_started != marker:
                return
        log.debug("Call %s: answering speech queued during a compute request", self.id)
        self._respond()

    def _update_session(self, session: Any) -> None:
        """``session.update`` from the caller: only the output voice can change; it applies
        from the next reply on. Always answered with ``session.updated``."""
        voice = session_voice(session)
        if voice is not None:
            wanted = voice.strip().lower() if isinstance(voice, str) else ""
            if self.config.voice_override:
                log.info("Call %s: voice stays %s (forced by VOICE_GATEWAY_FORCE_VOICE)",
                         self.id, self.voice)
            elif wanted in VOICES:
                with self._lock:
                    changed, self.voice = wanted != self.voice, wanted
                if changed:
                    log.info("Call %s: voice changed to %s", self.id, wanted)
            else:
                log.info("Call %s: ignored unknown voice %r", self.id, str(voice)[:40])
        self.send({"type": "session.updated", "session": self._session_view()})

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

    def close(self, reason: str = "hangup") -> None:
        """End the call; logs one line with how long it lasted and what it cost."""
        with self._lock:
            if self.closed:
                return
            self.close_reason = reason        # first: readers check ``closed`` without the lock
            self.closed = True
            if self._turn_cancel is not None:
                self._turn_cancel.set()
            cost, replies, computes = self.cost, self.replies, self.compute_requests
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()
        self._speaker_stop.set()
        self.pipeline.set_state(Gst.State.NULL)
        with self._lock:
            handlers, self._handlers = self._handlers, []
        for obj, handler in handlers:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        self._channel = None
        log.info("Call %s ended (%s): %s, %s, %s, US$ %.5f", self.id, reason,
                 _duration(time.monotonic() - self.started),
                 _plural(replies, "reply", "replies"),
                 _plural(computes, "compute request", "compute requests"), cost)
        self._on_closed(self.id)
