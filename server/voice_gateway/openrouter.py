"""Streaming chat completions with audio in and audio out, through OpenRouter."""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import threading
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

OUTPUT_RATE = 24000          # pcm16 from the audio models is 24 kHz mono


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


@dataclass
class TurnResult:
    transcript: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = ""
    cost: float = 0.0
    cancelled: bool = False


@dataclass
class Transcription:
    text: str = ""
    cost: float = 0.0


class ProviderError(Exception):
    pass


class OpenRouter:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1",
                 timeout: float = 60.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, body: dict):
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/EuVinicios/Dotdeck",
                     "X-Title": "Dotdeck voice gateway"})
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read().decode()).get("error", {}).get("message", "")
            except (ValueError, AttributeError, OSError, http.client.HTTPException):
                pass
            raise ProviderError(f"OpenRouter returned HTTP {error.code}. {detail}".strip()) \
                from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(f"Could not reach OpenRouter: {error}") from None

    def stream_turn(self, model: str, messages: list[dict], voice: str, tools: list[dict],
                    on_transcript: Callable[[str], None], on_audio: Callable[[bytes], None],
                    cancel: threading.Event) -> TurnResult:
        """Run one spoken reply. Audio arrives as pcm16 at 24 kHz through ``on_audio``.

        Every failure (refusal, a connection that drops mid-reply, a stream that is not what
        we expect) is a ``ProviderError``, so the call can tell the caller and carry on."""
        body = {"model": model, "messages": messages, "stream": True,
                "modalities": ["text", "audio"], "audio": {"voice": voice, "format": "pcm16"},
                "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        result = TurnResult()
        calls: dict[int, dict] = {}
        with self._post(body) as response:
            try:
                for raw in response:
                    if cancel.is_set():
                        result.cancelled = True
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(chunk, dict):
                        _apply_chunk(chunk, result, calls, on_transcript, on_audio)
            except (OSError, http.client.HTTPException) as error:
                raise ProviderError(f"The connection to OpenRouter dropped during the reply: "
                                    f"{error or type(error).__name__}") from None
        result.tool_calls = [calls[i] for i in sorted(calls)]
        return result

    def transcribe(self, model: str, pcm: bytes, rate: int) -> Transcription:
        """Plain transcript of what the caller said (for captions and the call receipt)."""
        audio = base64.b64encode(wav_bytes(pcm, rate)).decode()
        body = {"model": model, "stream": False, "usage": {"include": True}, "messages": [
            {"role": "system", "content": "Transcribe the audio exactly, in its original "
                                          "language. Reply with the transcript only. If "
                                          "there are no clear spoken words, reply with "
                                          "nothing at all."},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}]}]}
        with self._post(body) as response:
            try:
                data = json.loads(response.read().decode())
            except (ValueError, OSError, http.client.HTTPException) as error:
                raise ProviderError(f"Unreadable transcription reply: {error}") from None
        result = Transcription()
        if not isinstance(data, dict):
            return result
        try:
            result.text = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            pass
        result.cost = _cost(data.get("usage"))
        return result


def _cost(usage) -> float:
    """``usage.cost`` from an OpenRouter reply, or 0 when it is missing or not a number."""
    try:
        return float(usage.get("cost") or 0.0) if isinstance(usage, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _apply_chunk(chunk: dict, result: TurnResult, calls: dict[int, dict],
                 on_transcript: Callable[[str], None], on_audio: Callable[[bytes], None]) -> None:
    """Add one streamed chunk to ``result``; pieces of an unexpected shape are skipped."""
    error = chunk.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else None
        raise ProviderError(str(message or error)[:500])
    cost = _cost(chunk.get("usage"))
    if cost:
        result.cost = cost
    choices = chunk.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        audio = delta.get("audio")
        if isinstance(audio, dict):
            transcript = audio.get("transcript")
            if isinstance(transcript, str) and transcript:
                result.transcript += transcript
                on_transcript(transcript)
            data = audio.get("data")
            if isinstance(data, str) and data:
                try:
                    pcm = base64.b64decode(data)
                except ValueError:           # binascii.Error: not base64
                    pcm = b""
                if pcm:
                    on_audio(pcm)
        tool_calls = delta.get("tool_calls")
        for call in tool_calls if isinstance(tool_calls, list) else []:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            slot = calls.setdefault(index if isinstance(index, int) else 0,
                                    {"id": "", "name": "", "arguments": ""})
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            slot["id"] = str(call.get("id") or slot["id"])
            slot["name"] = str(function.get("name") or slot["name"])
            arguments = function.get("arguments")
            slot["arguments"] += arguments if isinstance(arguments, str) else ""
        if choice.get("finish_reason"):
            result.finish_reason = str(choice["finish_reason"])
