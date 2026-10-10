"""The two HTTP endpoints of the OpenAI Realtime API that OpenDots uses.

``POST /v1/realtime/calls`` takes a multipart form with the caller's SDP offer and the
session configuration and answers with our SDP; ``POST /v1/realtime/calls/<id>/hangup``
ends the call. ``GET /health`` is for monitoring: ``{ok, calls, model, transcribeModel}``,
the models a call uses (never a key; OpenDots shows them in its model settings).
"""

from __future__ import annotations

import email.parser
import email.policy
import hmac
import json
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .call import Call
from .config import Config
from .openrouter import OpenRouter

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000


def parse_form(content_type: str, body: bytes) -> dict[str, str]:
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields = {}
    if not message.is_multipart():
        return fields
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            payload = part.get_payload(decode=True) or b""
            fields[str(name)] = payload.decode("utf-8", "replace")
    return fields


class Gateway:
    def __init__(self, config: Config, provider: OpenRouter | None = None):
        self.config = config
        self.provider = provider or OpenRouter(config.api_key, config.base_url)
        self.calls: dict[str, Call] = {}
        self._lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DotdeckVoiceGateway/1"

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def _reply(self, status: int, body: bytes = b"", content_type: str = "application/json",
                       headers: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status: int, message: str) -> None:
                self._reply(status, json.dumps({"error": {"message": message}}).encode())

            def _authorized(self) -> bool:
                supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                return hmac.compare_digest(supplied.encode(), gateway.config.token.encode())

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    self._reply(200, json.dumps(gateway.health()).encode())
                else:
                    self._error(404, "Not found.")

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    # The usual cause: VOICE_API_KEY in the OpenDots .env was replaced (for
                    # example by an OpenAI key pasted in its settings).
                    log.warning("Refused %s: wrong gateway token (VOICE_API_KEY in the OpenDots "
                                ".env must equal VOICE_GATEWAY_TOKEN)", self.path[:100])
                    self._error(401, "Invalid gateway token.")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    self._error(400, "Invalid Content-Length.")
                    return
                if length > MAX_BODY:
                    self._error(413, "Request is too large.")
                    return
                body = self.rfile.read(length)
                if self.path.rstrip("/") == "/v1/realtime/calls":
                    gateway.start_call(self, body)
                    return
                match = re.fullmatch(r"/v1/realtime/calls/([A-Za-z0-9_-]{1,200})/hangup", self.path)
                if match:
                    gateway.hangup(match.group(1))
                    self._reply(200, b"{}")
                    return
                self._error(404, "Not found.")

        self.server = ThreadingHTTPServer((config.host, config.port), Handler)
        self.server.daemon_threads = True

    def start_call(self, handler, body: bytes) -> None:
        fields = parse_form(handler.headers.get("Content-Type") or "", body)
        sdp = fields.get("sdp", "")
        if not sdp.startswith("v=0") or "m=audio" not in sdp:
            handler._error(400, "An SDP offer with audio is required.")
            return
        try:
            session = json.loads(fields.get("session") or "{}")
        except ValueError:
            session = None
        if not isinstance(session, dict):
            handler._error(400, "The session must be a JSON object.")
            return
        with self._lock:
            if len(self.calls) >= self.config.max_calls:
                log.warning("Refused a call: %d already in progress (VOICE_GATEWAY_MAX_CALLS)",
                            len(self.calls))
                handler._error(429, "Too many calls in progress.")
                return
            call_id = f"rtc_{secrets.token_hex(12)}"
            try:
                call = Call(call_id, session, self.config, self.provider, self._forget)
            except Exception:
                log.exception("Could not set up call %s", call_id)
                handler._error(500, "Could not set up the call.")
                return
            self.calls[call_id] = call
        began = time.monotonic()
        try:
            answer = call.answer(sdp)
        except Exception as error:
            log.warning("Could not answer call %s: %s", call_id, error)
            call.close("could not answer")
            handler._error(502, f"Could not start the call: {error}")
            return
        log.info("Call %s started: model %s, transcription %s, voice %s, answered in %.2f s "
                 "(%d in progress)", call_id, call.model, self.config.transcribe_model or "off",
                 call.voice, time.monotonic() - began, len(self.calls))
        handler._reply(201, answer.encode(), "application/sdp",
                       {"Location": f"/v1/realtime/calls/{call_id}"})

    def health(self) -> dict:
        """What ``GET /health`` answers: calls in progress and the models calls use."""
        return {"ok": True, "calls": len(self.calls), "model": self.config.model,
                "transcribeModel": self.config.transcribe_model or None}

    def hangup(self, call_id: str) -> None:
        call = self.calls.get(call_id)
        if call is not None:
            call.close("hangup")

    def _forget(self, call_id: str) -> None:
        with self._lock:
            self.calls.pop(call_id, None)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        for call in list(self.calls.values()):
            call.close("gateway stopped")
        self.server.shutdown()
        self.server.server_close()
