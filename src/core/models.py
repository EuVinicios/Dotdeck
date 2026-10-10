"""The server's AI models as Dotdeck shows them: names, providers, prices and keys.

Read from ``GET /api/model-config`` of the OpenDots fork. Newer servers report the
provider (``openrouter``, ``anthropic`` or ``openai-compatible``), one key per provider,
friendly names, prices per million tokens and the three models a voice call uses.
Servers set up with Dotdeck 0.3.2 report only the model id, one key, a provider guessed
from the API address (``claude`` for Anthropic) and preset names: every helper here
works with both, and nothing is invented when the server does not say it.

Pure data, no GTK, so the sidebar, the settings and the tests share it.
"""

from __future__ import annotations

import locale
import math
import re

from ..i18n import N_, _, translating

# What a server may report, newest ids first; "claude" and "gemini" come from 0.3.2-era
# servers, which guessed the provider from the API address.
PROVIDER_NAMES = {"openrouter": "OpenRouter", "anthropic": "Anthropic", "claude": "Anthropic",
                  "gemini": "Google Gemini", "openai": "OpenAI",
                  "openai-compatible": N_("OpenAI-compatible")}
# The providers Dotdeck offers, in the order of the Provider menu.
CHOOSABLE = ("openrouter", "anthropic", "openai-compatible")
CHOICE_LABELS = {"openrouter": N_("OpenRouter"), "anthropic": N_("Anthropic (API key)"),
                 "openai-compatible": N_("OpenAI-compatible")}
# "OpenRouter Key" is one message per provider, not "{provider} Key": Portuguese says
# "Chave do OpenRouter" but "Chave da Anthropic", and "Compatível com OpenAI" is no noun.
KEY_TITLES = {"openrouter": N_("OpenRouter Key"), "anthropic": N_("Anthropic Key"),
              "openai-compatible": N_("OpenAI-compatible Server Key"),
              "gemini": N_("Google Gemini Key")}
SPEEDS = {"fast": N_("fast"), "balanced": N_("balanced"), "deep": N_("deep thinking")}
# The audio models the Dotdeck voice gateway can use through OpenRouter. GPT Audio costs
# about 4× more for text and 25 to 50× more for audio (OpenRouter prices, October 2026).
VOICE_MODELS = (("openai/gpt-audio-mini", "GPT Audio Mini", N_("lower cost")),
                ("openai/gpt-audio", "GPT Audio", N_("better voice, many times the price")))
# The notes the OpenDots fork sends with its presets (src/server/fork/providers.ts) are in
# English; listed here they are translated like the rest of Dotdeck (see server_text).
SERVER_NOTES = (N_("Very low cost. Thinks before answering."),
                N_("Open weights, stronger reasoning."),
                N_("Anthropic through OpenRouter. Quick and capable with tools."),
                N_("Anthropic through OpenRouter. Strong writing and tool use."),
                N_("Anthropic through OpenRouter. Most capable, highest cost."),
                N_("Google through OpenRouter."),
                N_("OpenAI through OpenRouter."),
                N_("Meta, open weights."),
                N_("Direct with an Anthropic API key."))

# Words a model id spells in lower case: "deepseek" reads "DeepSeek".
_WORDS = {"gpt": "GPT", "deepseek": "DeepSeek", "glm": "GLM", "llama": "Llama",
          "openai": "OpenAI", "xai": "xAI", "ai": "AI", "mini": "Mini", "pro": "Pro",
          "oss": "OSS"}
_HINT = re.compile(r"\s*\([^()]*\)\s*$")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def modern(data: dict | None) -> bool:
    """The server reports one key per provider (OpenDots fork for Dotdeck 0.3.3+)."""
    return bool(data) and (isinstance(data.get("keys"), dict)
                           or isinstance(data.get("providers"), list))


def provider_of(data: dict | None) -> str:
    """The provider id the server uses now ("claude" from older servers is "anthropic")."""
    data = data or {}
    provider = _text(data.get("provider"))
    if provider == "claude":
        return "anthropic"
    if provider:
        return provider
    base_url = _text(data.get("baseUrl"))
    if "openrouter.ai" in base_url:
        return "openrouter"
    if "anthropic.com" in base_url:
        return "anthropic"
    return "openai-compatible" if base_url else ""


def provider_name(provider: str, base_url: str = "") -> str:
    if provider in PROVIDER_NAMES:
        return _(PROVIDER_NAMES[provider])
    if "openrouter.ai" in base_url:
        return PROVIDER_NAMES["openrouter"]
    return provider or _("Unknown")


def key_title(provider: str) -> str:
    """"OpenRouter Key", the title of a provider's key ("API Key" when it is unknown)."""
    provider = "anthropic" if provider == "claude" else provider
    if provider in KEY_TITLES:
        return _(KEY_TITLES[provider])
    if not provider:
        return _("API Key")
    return _("{provider} Key").format(provider=provider_name(provider))


def server_text(text: str) -> str:
    """English text from the server in the user's language, or "" when Dotdeck has no
    translation for it: a Portuguese page does not show an English note."""
    text = (text or "").strip()
    if not text:
        return ""
    shown = _(text)
    return shown if shown != text or not translating() else ""


def providers(data: dict | None) -> list[dict]:
    """``[{id, name, hasKey, baseUrl}]`` for the Provider menu and the key rows.

    Newer servers list them; for older ones only the provider in use is known.
    """
    data = data or {}
    listed = data.get("providers") if isinstance(data.get("providers"), list) else None
    found: list[dict] = []
    if listed is not None:
        for item in listed:
            if isinstance(item, dict) and _text(item.get("id")):
                known = item["id"] in PROVIDER_NAMES           # Dotdeck has it translated
                found.append({"id": item["id"],
                              "name": provider_name(item["id"]) if known
                              else _text(item.get("name")) or item["id"],
                              "hasKey": item.get("hasKey") is True,
                              "baseUrl": _text(item.get("baseUrl"))})
    if not found and modern(data):
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        found = [{"id": p, "name": provider_name(p), "hasKey": keys.get(p) is True,
                  "baseUrl": ""} for p in CHOOSABLE]
    if not found:
        current = provider_of(data)
        if current:
            found = [{"id": current, "name": provider_name(current, _text(data.get("baseUrl"))),
                      "hasKey": bool(data.get("hasApiKey")), "baseUrl": _text(data.get("baseUrl"))}]
    return found


def has_key(data: dict | None, provider: str) -> bool:
    data = data or {}
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else None
    if keys is not None and provider in keys:
        return keys.get(provider) is True
    for item in providers(data):
        if item["id"] == provider:
            return item["hasKey"]
    return provider == provider_of(data) and bool(data.get("hasApiKey"))


def presets(data: dict | None, provider: str | None = None) -> list[dict]:
    """The server's model presets, optionally only those of ``provider``.

    Presets of 0.3.2-era servers are all OpenRouter ids (with a "/").
    """
    data = data or {}
    listed = data.get("presets") if isinstance(data.get("presets"), list) else []
    found = []
    for item in listed:
        if not isinstance(item, dict) or not _text(item.get("id")):
            continue
        kind = _text(item.get("provider")) or ("openrouter" if "/" in item["id"] else "")
        kind = "anthropic" if kind == "claude" else kind
        if provider is None or kind == provider:
            found.append(dict(item, provider=kind))
    return found


def preset(data: dict | None, model: str) -> dict | None:
    return next((p for p in presets(data) if p["id"] == model), None)


def strip_hint(name: str) -> str:
    """"DeepSeek V4 Flash (fast, low cost)" reads "DeepSeek V4 Flash"."""
    stripped = _HINT.sub("", name or "").strip()
    return stripped or (name or "").strip()


def derive_name(model: str) -> str:
    """A readable name from a bare id: ``anthropic/claude-haiku-5.5`` → Claude Haiku 5.5."""
    base = (model or "").rsplit("/", 1)[-1].split(":", 1)[0]
    parts = [p for p in re.split(r"[-_\s]+", base) if p]
    words: list[str] = []
    for part in parts:
        low = part.lower()
        if words and re.fullmatch(r"\d{1,2}", part) and re.fullmatch(r"[\d.]*\d", words[-1]):
            words[-1] = f"{words[-1]}.{part}"          # claude-haiku-5-5 → 5.5
        elif low in _WORDS:
            words.append(_WORDS[low])
        elif re.fullmatch(r"v\d+(\.\d+)?", low):
            words.append(low.upper())
        elif part[:1].isalpha():
            words.append(part[:1].upper() + part[1:])
        else:
            words.append(part)
    return " ".join(words) or (model or "")


def display_name(data: dict | None, model: str | None = None) -> str:
    """The friendly name of ``model`` (default: the chat model), never invented."""
    data = data or {}
    current = _text(data.get("model"))
    model = current if model is None else model
    if not model:
        return ""
    if model == current and _text(data.get("modelName")):
        return strip_hint(data["modelName"])
    found = preset(data, model)
    if found and _text(found.get("name")):
        return strip_hint(found["name"])
    return derive_name(model)


def at_a_glance(model: str, provider: str, config: dict | None = None) -> str:
    """"Claude Haiku 5.5 · OpenRouter", for the sidebar; empty when there is no model."""
    if not model:
        return ""
    data = dict(config or {})
    if _text(data.get("model")) != model:
        data.pop("modelName", None)
    name = display_name(data, model)
    provider = "anthropic" if provider == "claude" else provider
    if not provider:
        return name
    return f"{name} · {provider_name(provider)}"


# -- numbers -------------------------------------------------------------------------------

def number(value: float, decimals: int) -> str:
    """``value`` with the decimal separator of the user's language (0,10 in Portuguese)."""
    try:
        return locale.format_string(f"%.{decimals}f", value)
    except (TypeError, ValueError):
        return f"{value:.{decimals}f}"


def money(value: float) -> str:
    """Dollars per million tokens: two decimals, more for fractions of a cent."""
    if value <= 0:
        return number(0, 2)
    if value < 0.01:          # two significant digits: 0.0046, not 0.00
        return number(value, min(6, 1 - math.floor(math.log10(value))))
    return number(value, 2)


def _price(price) -> tuple[float, float] | None:
    if not isinstance(price, dict):
        return None
    try:
        values = float(price["input"]), float(price["output"])
    except (KeyError, TypeError, ValueError):
        return None
    return values if all(v >= 0 for v in values) else None


def price_text(price) -> str:
    """"US$ 0.10 input · US$ 0.50 output per million tokens", or "" without a price."""
    values = _price(price)
    if values is None:
        return ""
    return _("US$ {input} input · US$ {output} output per million tokens").format(
        input=money(values[0]), output=money(values[1]))


def short_price(price) -> str:
    """"US$ 0.10 / 0.50 per million tokens" for the model list, or ""."""
    values = _price(price)
    if values is None:
        return ""
    return _("US$ {input} / {output} per million tokens").format(
        input=money(values[0]), output=money(values[1]))


def preset_detail(item: dict) -> str:
    """The second line of a model in the list: price, speed and the server's note."""
    parts = [short_price(item.get("price"))]
    speed = _text(item.get("speed"))
    if speed in SPEEDS:
        parts.append(_(SPEEDS[speed]))
    parts.append(server_text(_text(item.get("note"))))
    return " · ".join(p for p in parts if p)


def seconds(milliseconds) -> str:
    """1234 → "1.2 s" (with the user's decimal separator)."""
    try:
        value = float(milliseconds) / 1000
    except (TypeError, ValueError):
        return ""
    return _("{seconds} s").format(seconds=number(value, 1))


# -- voice calls ---------------------------------------------------------------------------

def call_models(data: dict | None) -> dict:
    """The models of a voice call: {voice, transcribe, thinking, gateway, reported}.

    ``reported`` is False on older servers, which do not tell the transcription model.
    """
    data = data or {}
    call = data.get("call") if isinstance(data.get("call"), dict) else None
    voice = _text(data.get("voiceModel"))
    gateway = "/" in voice or bool(data.get("voiceGateway"))
    if call is None:
        return {"voice": voice, "transcribe": "", "thinking": _text(data.get("model")),
                "gateway": gateway, "reported": False}
    voice = _text(call.get("voiceModel")) or voice
    return {"voice": voice, "transcribe": _text(call.get("transcribeModel")),
            "thinking": _text(call.get("thinkingModel")) or _text(data.get("model")),
            "gateway": call.get("gateway") is True if "gateway" in call else gateway,
            "reported": True}


# -- checks before sending -----------------------------------------------------------------

def model_problem(provider: str, model: str) -> str | None:
    """Why ``model`` cannot be an id for ``provider`` (the server checks it again)."""
    if not model:
        return _("Type the model ID.")
    if any(c.isspace() for c in model) or len(model) > 100:
        return _("A model ID has no spaces and up to 100 characters.")
    if provider == "openrouter" and "/" not in model.strip("/"):
        return _("OpenRouter model IDs look like vendor/model-name.")
    if provider == "anthropic" and "/" in model:
        return _("Anthropic model IDs have no vendor prefix, for example claude-haiku-5-5.")
    return None


def base_url_problem(url: str) -> str | None:
    if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", url or ""):
        return _("Type an address that starts with https:// or http://.")
    return None
