"""When a Dot's own news become a desktop notification (ADR-029).

As in the Grok Bot app: a Dot that finished replying tells the owner unless they are
looking at that very Dot (the window in front, the Dot open), the Dot or the event is
turned off, or the same Dot just said the same thing (at most one every 5 s).
"""

from __future__ import annotations

import time
from typing import Callable

GAP = 5.0   # seconds between two notices of one Dot about one event


class DotNotices:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: dict[tuple, float] = {}

    def allow(self, key: tuple, *, wanted: bool, looking: bool) -> bool:
        """Whether to notify now about ``key`` (server, Dot, event); remembers it if so."""
        if not wanted or looking:
            return False
        now = self._clock()
        if now - self._last.get(key, float("-inf")) < GAP:
            return False
        self._last[key] = now
        return True


def done_body(title: str) -> str:
    from ..i18n import _
    if title:
        return _("Finished replying in “{title}”.").format(title=title)
    return _("Finished replying.")
