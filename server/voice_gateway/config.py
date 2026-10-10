"""Gateway settings, from environment variables (see server/README.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    token: str                                   # what OpenDots sends as VOICE_API_KEY
    api_key: str                                 # OpenRouter key
    base_url: str = "https://openrouter.ai/api/v1"
    host: str = "127.0.0.1"
    port: int = 4320
    model: str = "openai/gpt-audio-mini"
    voice: str = "alloy"
    voice_override: str = ""                     # force a voice, whatever OpenDots asks
    transcribe_model: str = "google/gemini-2.5-flash-lite"
    vad_stop_ms: int = 700
    max_calls: int = 2
    greeting: bool = True                        # the Dot answers the call with one sentence

    @classmethod
    def from_env(cls, env: dict | None = None) -> Config:
        env = os.environ if env is None else env
        token = env.get("VOICE_GATEWAY_TOKEN", "")
        api_key = env.get("OPENROUTER_API_KEY", "")
        if len(token) < 24:
            raise ValueError("Set VOICE_GATEWAY_TOKEN (at least 24 characters).")
        if not api_key:
            raise ValueError("Set OPENROUTER_API_KEY.")
        return cls(
            token=token, api_key=api_key,
            base_url=env.get("OPENROUTER_BASE_URL", cls.base_url),
            host=env.get("VOICE_GATEWAY_HOST", cls.host),
            port=int(env.get("VOICE_GATEWAY_PORT", cls.port)),
            model=env.get("VOICE_GATEWAY_MODEL", cls.model),
            voice=env.get("VOICE_GATEWAY_VOICE", cls.voice),
            voice_override=env.get("VOICE_GATEWAY_FORCE_VOICE", ""),
            transcribe_model=env.get("VOICE_GATEWAY_TRANSCRIBE_MODEL", cls.transcribe_model),
            vad_stop_ms=int(env.get("VOICE_GATEWAY_SILENCE_MS", cls.vad_stop_ms)),
            max_calls=int(env.get("VOICE_GATEWAY_MAX_CALLS", cls.max_calls)),
            greeting=env.get("VOICE_GATEWAY_GREETING", "1").strip().lower()
            not in ("0", "false", "no", "off"),
        )
