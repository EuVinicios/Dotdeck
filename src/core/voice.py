"""Native WebRTC for the OpenDots voice call.

WebKitGTK builds shipped by distributions and by the GNOME Flatpak runtime have no
``RTCPeerConnection``, so the OpenDots call (OpenAI Realtime over WebRTC) cannot start
in a plain web view. ``web/rtc-polyfill.js`` gives the page an ``RTCPeerConnection``
that forwards every call here, and this module runs the real peer connection with
GStreamer's ``webrtcbin``: microphone from PipeWire/PulseAudio, Opus audio, a data
channel for the provider's events, and optional echo cancellation.

Only what the voice call needs is implemented: one outgoing audio track, incoming
audio, data channels, offer/answer and ICE candidates.
"""

from __future__ import annotations

import logging
import os
from itertools import count
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
try:
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, GObject, Gst, GstSdp, GstWebRTC  # noqa: E402
    _HAVE_WEBRTC = True
except (ValueError, ImportError):
    from gi.repository import GLib, GObject, Gst  # noqa: E402
    GstSdp = None
    GstWebRTC = None
    _HAVE_WEBRTC = False

log = logging.getLogger(__name__)

REQUIRED_ELEMENTS = ("webrtcbin", "nicesrc", "dtlssrtpenc", "sctpenc", "opusenc", "opusdec",
                     "rtpopuspay", "rtpopusdepay", "audiomixer", "volume")
OPUS_CAPS = "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000"

# DOTDECK_TEST_AUDIO=1 replaces the microphone with a tone and the speakers with a
# fake sink, so tests and CI run without audio hardware.
_TEST_AUDIO = os.environ.get("DOTDECK_TEST_AUDIO") == "1"

if _HAVE_WEBRTC:
    _STATES = {
        GstWebRTC.WebRTCPeerConnectionState.NEW: "new",
        GstWebRTC.WebRTCPeerConnectionState.CONNECTING: "connecting",
        GstWebRTC.WebRTCPeerConnectionState.CONNECTED: "connected",
        GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED: "disconnected",
        GstWebRTC.WebRTCPeerConnectionState.FAILED: "failed",
        GstWebRTC.WebRTCPeerConnectionState.CLOSED: "closed",
    }
else:
    _STATES = {}


def missing_elements() -> list[str]:
    if not _HAVE_WEBRTC:
        return ["gir1.2-gst-plugins-bad-1.0"]
    if not Gst.is_initialized():
        Gst.init(None)
    return [name for name in REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]


def available() -> bool:
    return not missing_elements()


def snap_microphone_blocked() -> bool:
    """In a snap the microphone needs the audio-record interface, which users connect."""
    if not os.environ.get("SNAP_NAME"):
        return False
    import subprocess
    try:
        return subprocess.run(["snapctl", "is-connected", "audio-record"],
                              capture_output=True, timeout=5).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


class VoiceError(Exception):
    pass


def _main(func, *args) -> None:
    """Run ``func(*args)`` on the main loop (GStreamer calls back from its own threads)."""
    def run():
        func(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)


class PeerConnection:
    """One webrtcbin pipeline. All public methods run on the main thread."""

    def __init__(self, pc_id: str, emit: Callable[[dict], None], echo_cancellation: bool = True):
        self.id = pc_id
        self._emit = emit
        self._closed = False
        self._channels: dict[str, GstWebRTC.WebRTCDataChannel] = {}
        self._channel_ids = count(1)
        self._mic_volume: Gst.Element | None = None
        self._has_mic = False
        self.mic_enabled = True
        self.speaker_muted = False
        self._echo = echo_cancellation and Gst.ElementFactory.find("webrtcdsp") is not None \
            and Gst.ElementFactory.find("webrtcechoprobe") is not None and not _TEST_AUDIO

        self.pipeline = Gst.Pipeline.new(f"dotdeck-call-{pc_id}")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.pipeline.add(self.webrtc)

        # Playback: a silent live source keeps the mixer (and the echo probe) running
        # before the remote audio arrives; remote tracks are mixed in as they come.
        sink = "fakesink sync=false" if _TEST_AUDIO else "autoaudiosink"
        probe = f"webrtcechoprobe name=echo-{pc_id} ! " if self._echo else ""
        self._playback = Gst.parse_bin_from_description(
            "audiotestsrc wave=silence is-live=true ! audio/x-raw,rate=48000,channels=2 ! "
            "audiomixer name=mix ! audioconvert ! audioresample ! "
            f"{probe}volume name=speaker ! {sink}", False)
        self.pipeline.add(self._playback)
        self._mixer = self._playback.get_by_name("mix")
        self._speaker = self._playback.get_by_name("speaker")

        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("on-data-channel", self._on_remote_data_channel)
        self.webrtc.connect("notify::connection-state", self._on_connection_state)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise VoiceError("Could not start the audio pipeline.")

    # -- media -----------------------------------------------------------------------------

    def add_microphone(self) -> None:
        if self._has_mic:
            return
        source = "audiotestsrc is-live=true wave=sine freq=440 volume=0.2" if _TEST_AUDIO \
            else "autoaudiosrc"
        dsp = f"webrtcdsp probe=echo-{self.id} ! audioconvert ! " if self._echo else ""
        mic = Gst.parse_bin_from_description(
            f"{source} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
            f"{dsp}volume name=mic ! audioconvert ! queue ! opusenc ! rtpopuspay pt=111 ! "
            f"queue ! capsfilter caps=\"{OPUS_CAPS}\"", True)
        self.pipeline.add(mic)
        if not mic.link(self.webrtc):
            raise VoiceError("Could not connect the microphone.")
        self._mic_volume = mic.get_by_name("mic")
        self._mic_volume.set_property("mute", not self.mic_enabled)
        transceiver = self.webrtc.emit("get-transceiver", 0)
        if transceiver is not None:
            transceiver.set_property("direction",
                                     GstWebRTC.WebRTCRTPTransceiverDirection.SENDRECV)
        mic.sync_state_with_parent()
        self._has_mic = True

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = enabled
        if self._mic_volume is not None:
            self._mic_volume.set_property("mute", not enabled)

    def set_speaker_muted(self, muted: bool) -> None:
        self.speaker_muted = muted
        self._speaker.set_property("mute", muted)

    def _on_pad_added(self, _webrtc, pad: Gst.Pad) -> None:
        # Runs on a streaming thread and must link before the first packet is pushed,
        # or the stream stops with "not-linked".
        if pad.get_direction() != Gst.PadDirection.SRC or self._closed:
            return
        decoder = Gst.parse_bin_from_description(
            "queue ! rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=48000,channels=2 ! queue", True)
        self.pipeline.add(decoder)
        decoder.link(self._mixer)
        if pad.link(decoder.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            log.warning("Could not link the remote audio of call %s", self.id)
            return
        decoder.sync_state_with_parent()
        _main(self._emit, {"type": "track", "kind": "audio"})

    # -- data channels ---------------------------------------------------------------------

    def create_data_channel(self, label: str, ordered: bool = True) -> str:
        options = Gst.Structure.new_empty("options")
        options.set_value("ordered", ordered)
        channel = self.webrtc.emit("create-data-channel", label, options)
        if channel is None:
            raise VoiceError("Could not create the data channel.")
        return self._track_channel(channel)

    def _on_remote_data_channel(self, _webrtc, channel) -> None:
        def adopt():
            dc_id = self._track_channel(channel)
            self._emit({"type": "datachannel", "dc": dc_id, "label": channel.props.label})
        _main(adopt)

    def _track_channel(self, channel) -> str:
        dc_id = str(next(self._channel_ids))
        self._channels[dc_id] = channel
        channel.connect("on-open", lambda _c: _main(self._emit, {"type": "dcopen", "dc": dc_id}))
        channel.connect("on-close", lambda _c: _main(self._emit, {"type": "dcclose", "dc": dc_id}))
        channel.connect("on-error", lambda _c, error: _main(
            self._emit, {"type": "dcerror", "dc": dc_id, "message": str(error)}))
        channel.connect("on-message-string", lambda _c, text: _main(
            self._emit, {"type": "dcmessage", "dc": dc_id, "data": text}))
        channel.connect("on-message-data", lambda _c, data: _main(
            self._emit, {"type": "dcmessage", "dc": dc_id,
                         "data": bytes(data.get_data() or b"").decode("utf-8", "replace")}))
        if channel.props.ready_state == GstWebRTC.WebRTCDataChannelState.OPEN:
            _main(self._emit, {"type": "dcopen", "dc": dc_id})
        return dc_id

    def send(self, dc_id: str, text: str) -> None:
        channel = self._channels.get(dc_id)
        if channel is None:
            raise VoiceError("Unknown data channel.")
        channel.send_string(text)

    def close_channel(self, dc_id: str) -> None:
        channel = self._channels.pop(dc_id, None)
        if channel is not None:
            channel.close()

    # -- negotiation -----------------------------------------------------------------------

    def create_offer(self, done: Callable[[str | None, str | None], None]) -> None:
        def on_reply(promise):
            reply = promise.get_reply()
            offer = reply.get_value("offer") if reply and reply.has_field("offer") else None
            if offer is None:
                _main(done, None, _error_text(reply, "Could not create the offer."))
            else:
                _main(done, offer.sdp.as_text(), None)
        self.webrtc.emit("create-offer", None, Gst.Promise.new_with_change_func(on_reply))

    def create_answer(self, done: Callable[[str | None, str | None], None]) -> None:
        def on_reply(promise):
            reply = promise.get_reply()
            answer = reply.get_value("answer") if reply and reply.has_field("answer") else None
            if answer is None:
                _main(done, None, _error_text(reply, "Could not create the answer."))
            else:
                _main(done, answer.sdp.as_text(), None)
        self.webrtc.emit("create-answer", None, Gst.Promise.new_with_change_func(on_reply))

    def set_description(self, local: bool, sdp_type: str, sdp: str,
                        done: Callable[[str | None], None]) -> None:
        kinds = {"offer": GstWebRTC.WebRTCSDPType.OFFER, "answer": GstWebRTC.WebRTCSDPType.ANSWER,
                 "pranswer": GstWebRTC.WebRTCSDPType.PRANSWER,
                 "rollback": GstWebRTC.WebRTCSDPType.ROLLBACK}
        if sdp_type not in kinds:
            done(f"Unsupported description type: {sdp_type}")
            return
        result, message = GstSdp.SDPMessage.new_from_text(sdp)
        if result != GstSdp.SDPResult.OK:
            done("The session description is not valid SDP.")
            return
        description = GstWebRTC.WebRTCSessionDescription.new(kinds[sdp_type], message)

        def on_reply(promise):
            reply = promise.get_reply()
            _main(done, _error_text(reply, None))
        signal = "set-local-description" if local else "set-remote-description"
        self.webrtc.emit(signal, description, Gst.Promise.new_with_change_func(on_reply))

    def add_ice_candidate(self, mline: int, candidate: str) -> None:
        if candidate:
            self.webrtc.emit("add-ice-candidate", mline, candidate)

    def _on_ice_candidate(self, _webrtc, mline: int, candidate: str) -> None:
        _main(self._emit, {"type": "icecandidate", "mline": mline, "candidate": candidate})

    def _on_connection_state(self, webrtc, _pspec) -> None:
        state = _STATES.get(webrtc.props.connection_state, "new")
        _main(self._emit, {"type": "connectionstatechange", "state": state})

    def _on_bus_message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.warning("Call %s: %s (%s)", self.id, error.message, debug)
            if message.src is not None and message.src.get_name().startswith(("autoaudio", "pulse",
                                                                                "pipewire")):
                self._emit({"type": "mediaerror", "message": error.message})

    # -- teardown --------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for channel in list(self._channels.values()):
            try:
                channel.close()
            except Exception:  # already closed by the peer
                pass
        self._channels.clear()
        bus = self.pipeline.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)


def _error_text(reply: Gst.Structure | None, default: str | None) -> str | None:
    if reply is not None and reply.has_field("error"):
        error = reply.get_value("error")
        return getattr(error, "message", None) or str(error)
    return default


class VoiceBridge(GObject.Object):
    """Dispatches the polyfill's operations to ``PeerConnection`` objects.

    ``send_event(pc_id, event)`` delivers events back to the page. ``request_microphone``
    asks the user for permission (``callback(allowed)``).
    """

    __gtype_name__ = "DotdeckVoiceBridge"
    __gsignals__ = {
        "calls-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, send_event: Callable[[str, dict], None],
                 request_microphone: Callable[[Callable[[bool], None]], None],
                 echo_cancellation: Callable[[], bool] = lambda: True):
        super().__init__()
        self._send_event = send_event
        self._request_microphone = request_microphone
        self._echo = echo_cancellation
        self._peers: dict[str, PeerConnection] = {}
        self._ids = count(1)

    @property
    def in_call(self) -> bool:
        return any(p._has_mic for p in self._peers.values())

    @property
    def mic_enabled(self) -> bool:
        return all(p.mic_enabled for p in self._peers.values() if p._has_mic)

    def set_mic_enabled(self, enabled: bool) -> None:
        for peer in self._peers.values():
            peer.set_mic_enabled(enabled)
        self.emit("calls-changed")

    def close_all(self) -> None:
        for peer in list(self._peers.values()):
            peer.close()
        self._peers.clear()
        self.emit("calls-changed")

    def handle(self, request: dict, reply: Callable[[dict | None, str | None], None]) -> None:
        """Run one operation from the page; ``reply(result, error)`` exactly once."""
        op = request.get("op")
        try:
            handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
            if handler is None:
                reply(None, f"Unsupported operation: {op}")
                return
            handler(request, reply)
        except VoiceError as error:
            reply(None, str(error))
        except Exception as error:  # never leave the page waiting
            log.exception("Voice operation %s failed", op)
            reply(None, str(error) or error.__class__.__name__)

    def _peer(self, request: dict) -> PeerConnection:
        peer = self._peers.get(str(request.get("pc")))
        if peer is None:
            raise VoiceError("This call is no longer active.")
        return peer

    def _op_requestMicrophone(self, _request, reply) -> None:
        self._request_microphone(lambda allowed: reply({"allowed": bool(allowed)}, None))

    def _op_create(self, _request, reply) -> None:
        if not available():
            raise VoiceError("Voice calls need the GStreamer WebRTC plugins: "
                             + ", ".join(missing_elements()))
        pc_id = str(next(self._ids))
        self._peers[pc_id] = PeerConnection(pc_id, lambda event: self._send_event(pc_id, event),
                                            self._echo())
        reply({"pc": pc_id}, None)

    def _op_createDataChannel(self, request, reply) -> None:
        dc_id = self._peer(request).create_data_channel(str(request.get("label", "")),
                                                        bool(request.get("ordered", True)))
        reply({"dc": dc_id}, None)

    def _op_addTrack(self, request, reply) -> None:
        if request.get("kind") != "audio":
            raise VoiceError("Only audio tracks are supported.")
        if snap_microphone_blocked():
            raise VoiceError("The microphone is not connected to the Dotdeck snap. Run: "
                             "snap connect dotdeck:audio-record")
        self._peer(request).add_microphone()
        self.emit("calls-changed")
        reply({}, None)

    def _op_createOffer(self, request, reply) -> None:
        self._peer(request).create_offer(
            lambda sdp, error: reply({"type": "offer", "sdp": sdp} if sdp else None, error))

    def _op_createAnswer(self, request, reply) -> None:
        self._peer(request).create_answer(
            lambda sdp, error: reply({"type": "answer", "sdp": sdp} if sdp else None, error))

    def _op_setLocalDescription(self, request, reply) -> None:
        self._peer(request).set_description(True, str(request.get("type")), str(request.get("sdp")),
                                            lambda error: reply({} if not error else None, error))

    def _op_setRemoteDescription(self, request, reply) -> None:
        self._peer(request).set_description(False, str(request.get("type")),
                                            str(request.get("sdp")),
                                            lambda error: reply({} if not error else None, error))

    def _op_addIceCandidate(self, request, reply) -> None:
        self._peer(request).add_ice_candidate(int(request.get("mline") or 0),
                                              str(request.get("candidate") or ""))
        reply({}, None)

    def _op_send(self, request, reply) -> None:
        self._peer(request).send(str(request.get("dc")), str(request.get("data", "")))
        reply({}, None)

    def _op_closeChannel(self, request, reply) -> None:
        self._peer(request).close_channel(str(request.get("dc")))
        reply({}, None)

    def _op_setMicEnabled(self, request, reply) -> None:
        self._peer(request).set_mic_enabled(bool(request.get("enabled")))
        self.emit("calls-changed")
        reply({}, None)

    def _op_setSpeakerMuted(self, request, reply) -> None:
        self._peer(request).set_speaker_muted(bool(request.get("muted")))
        reply({}, None)

    def _op_close(self, request, reply) -> None:
        peer = self._peers.pop(str(request.get("pc")), None)
        if peer is not None:
            peer.close()
            self.emit("calls-changed")
        reply({}, None)
