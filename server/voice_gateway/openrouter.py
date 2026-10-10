"""Streaming chat completions with audio in and audio out, through OpenRouter."""

from __future__ import annotations

import base64
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
            except (ValueError, AttributeError):
                pass
            raise ProviderError(f"OpenRouter returned HTTP {error.code}. {detail}".strip()) \
                from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(f"Could not reach OpenRouter: {error}") from None

    def stream_turn(self, model: str, messages: list[dict], voice: str, tools: list[dict],
                    on_transcript: Callable[[str], None], on_audio: Callable[[bytes], None],
                    cancel: threading.Event) -> TurnResult:
        """Run one spoken reply. Audio arrives as pcm16 at 24 kHz through ``on_audio``."""
        body = {"model": model, "messages": messages, "stream": True,
                "modalities": ["text", "audio"], "audio": {"voice": voice, "format": "pcm16"},
                "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        result = TurnResult()
        calls: dict[int, dict] = {}
        with self._post(body) as response:
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
                if chunk.get("error"):
                    raise ProviderError(str(chunk["error"].get("message", chunk["error"])))
                usage = chunk.get("usage") or {}
                if usage.get("cost"):
                    result.cost = float(usage["cost"])
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    audio = delta.get("audio") or {}
                    if audio.get("transcript"):
                        result.transcript += audio["transcript"]
                        on_transcript(audio["transcript"])
                    if audio.get("data"):
                        on_audio(base64.b64decode(audio["data"]))
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0),
                                                {"id": "", "name": "", "arguments": ""})
                        slot["id"] = call.get("id") or slot["id"]
                        function = call.get("function") or {}
                        slot["name"] = function.get("name") or slot["name"]
                        slot["arguments"] += function.get("arguments") or ""
                    if choice.get("finish_reason"):
                        result.finish_reason = choice["finish_reason"]
        result.tool_calls = [calls[i] for i in sorted(calls)]
        return result

    def transcribe(self, model: str, pcm: bytes, rate: int) -> str:
        """Plain transcript of what the caller said (for captions and the call receipt)."""
        audio = base64.b64encode(wav_bytes(pcm, rate)).decode()
        body = {"model": model, "stream": False, "messages": [
            {"role": "system", "content": "Transcribe the audio exactly, in its original "
                                          "language. Reply with the transcript only."},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}]}]}
        with self._post(body) as response:
            data = json.loads(response.read().decode())
        try:
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return ""
