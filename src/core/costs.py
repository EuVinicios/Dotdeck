"""What things cost, in words: a conversation, a call and the OpenRouter spend.

The numbers come from the server (``/api/costs/threads/:id`` and ``/api/spend``, only on
servers set up with the Dotdeck guide) and from the voice gateway during a call. Every
helper here is pure, so the texts can be tested without a window.
"""

from __future__ import annotations

import locale

from ..i18n import _, ngettext
from .models import money


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def usd(value: float) -> str:
    """"US$ 0,29"; fractions of a cent keep two significant digits ("US$ 0,0046")."""
    return _("US$ {amount}").format(amount=money(value))


def tokens(value: int) -> str:
    """12.345 tokens with the user's thousands separator."""
    try:
        return locale.format_string("%d", int(value), grouping=True)
    except (TypeError, ValueError):
        return str(int(value))


# -- a conversation --------------------------------------------------------------------

def conversation_cost(cost: dict | None) -> float | None:
    if not isinstance(cost, dict):
        return None
    return _amount(cost.get("costUsd"))


def conversation_text(cost: dict | None) -> str:
    """"≈ US$ 0,012 in this conversation", or "" before anything was spent."""
    value = conversation_cost(cost)
    if value is None or (value == 0 and not cost.get("turns") and not cost.get("calls")):
        return ""
    return _("≈ {cost} in this conversation").format(cost=usd(value))


def conversation_tooltip(cost: dict | None) -> str:
    value = conversation_cost(cost)
    if value is None:
        return ""
    turns = int(cost.get("turns") or 0)
    calls = int(cost.get("calls") or 0)
    lines = [_("This conversation: about {cost}").format(cost=usd(value)),
             ngettext("{turns} reply · {input} tokens in · {output} tokens out",
                      "{turns} replies · {input} tokens in · {output} tokens out",
                      turns).format(turns=turns, input=tokens(cost.get("inputTokens") or 0),
                                    output=tokens(cost.get("outputTokens") or 0))]
    if calls:
        lines.append(ngettext("Includes the voice of {calls} call",
                              "Includes the voice of {calls} calls", calls).format(calls=calls))
    lines.append(_("Approximate: tokens × the model's price."))
    if cost.get("complete") is False:
        lines.append(_("Some replies used a model without a known price."))
    return "\n".join(lines)


# -- a call ----------------------------------------------------------------------------

def call_cost(voice: float | None, compute: float | None) -> float | None:
    """The voice (speech and transcription) plus what the Dot worked out meanwhile."""
    if voice is None and compute is None:
        return None
    return (voice or 0.0) + max(0.0, compute or 0.0)


def call_tooltip(voice: float | None, compute: float | None) -> str:
    total = call_cost(voice, compute)
    if total is None:
        return ""
    lines = [_("This call so far: about {cost}").format(cost=usd(total))]
    if voice is not None:
        lines.append(_("Voice and transcription: {cost}").format(cost=usd(voice)))
    if compute:
        lines.append(_("Research during the call: {cost}").format(cost=usd(compute)))
    return "\n".join(lines)


# -- the OpenRouter spend --------------------------------------------------------------

def _spend_parts(spend: dict | None) -> tuple[dict, dict | None] | None:
    if not isinstance(spend, dict) or not spend.get("available") or spend.get("error"):
        return None
    key = spend.get("key") if isinstance(spend.get("key"), dict) else None
    if key is None:
        return None
    credits = spend.get("credits") if isinstance(spend.get("credits"), dict) else None
    return key, credits


def spend_text(spend: dict | None) -> str:
    """"US$ 0,29 this month · US$ 6,11 left", for the sidebar; "" when unknown."""
    parts = _spend_parts(spend)
    if parts is None:
        return ""
    key, credits = parts
    month = _amount(key.get("monthly"))
    left = _amount(key.get("limitRemaining"))
    if left is None and credits is not None:
        left = _amount(credits.get("remaining"))
    if month is None:
        return _("{left} left").format(left=usd(left)) if left is not None else ""
    if left is None:
        return _("{month} this month").format(month=usd(month))
    return _("{month} this month · {left} left").format(month=usd(month), left=usd(left))


def spend_tooltip(spend: dict | None) -> str:
    parts = _spend_parts(spend)
    if parts is None:
        return ""
    key, credits = parts
    lines = [_("OpenRouter spend (this server's key)")]
    for field, label in (("daily", _("Today: {cost}")), ("weekly", _("This week: {cost}")),
                         ("monthly", _("This month: {cost}")),
                         ("usage", _("All time: {cost}"))):
        value = _amount(key.get(field))
        if value is not None:
            lines.append(label.format(cost=usd(value)))
    limit, left = _amount(key.get("limit")), _amount(key.get("limitRemaining"))
    if limit is not None:
        lines.append(_("Key limit: {limit} ({left} left)").format(
            limit=usd(limit), left=usd(left or 0)))
    if credits is not None:
        remaining, total = _amount(credits.get("remaining")), _amount(credits.get("total"))
        if remaining is not None and total is not None:
            lines.append(_("Account credits: {left} of {total} left").format(
                left=usd(remaining), total=usd(total)))
    lines.append(_("Includes chats, routines and voice calls. Click for AI Models."))
    return "\n".join(lines)

