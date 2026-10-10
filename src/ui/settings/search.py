"""Finding a setting by typing: a forgiving matcher over the rows of every page.

Pure Python (no GTK), so it is unit tested on its own. The window turns its rows into
:class:`Entry` values right before searching, so titles that change (a server name, a
Dot count) are always current.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

LIMIT = 40

# How much a match in each field is worth; the title matters most.
_WEIGHTS = (("title", 10.0), ("keywords", 6.0), ("group", 3.0), ("subtitle", 2.0),
            ("page_title", 2.0))


def fold(text: str) -> str:
    """Lower case without accents, so "notificacao" finds "Notificação"."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(c for c in text if not unicodedata.combining(c))


def terms(query: str) -> list[str]:
    return [t for t in fold(query).split() if t]


@dataclass
class Entry:
    page: str                      # the page name ("voice")
    page_title: str                # what the sidebar calls it ("Voice")
    title: str
    subtitle: str = ""
    group: str = ""
    keywords: tuple[str, ...] = ()
    target: object = field(default=None, compare=False, repr=False)
    is_page: bool = False


def _field_text(entry: Entry, name: str) -> str:
    if name == "keywords":
        return fold(" ".join(entry.keywords))
    return fold(getattr(entry, name))


def _find(text: str, word: str) -> int:
    """Where ``word`` is in ``text``; words of three letters or less only match a word start
    (so "mic" finds "Microphone" but not "Acadêmico")."""
    index = text.find(word)
    if len(word) > 3:
        return index
    while index > 0 and text[index - 1].isalnum():
        index = text.find(word, index + 1)
    return index


def score(entry: Entry, words: list[str]) -> float:
    """0 when a word is missing; otherwise higher for title and word-start matches."""
    if not words:
        return 0.0
    texts = [(_field_text(entry, name), weight) for name, weight in _WEIGHTS]
    total = 0.0
    for word in words:
        best = 0.0
        for text, weight in texts:
            index = _find(text, word)
            if index < 0:
                continue
            value = weight
            if index == 0 or not text[index - 1].isalnum():
                value *= 1.5
            best = max(best, value)
        if not best:
            return 0.0
        total += best
    title = fold(entry.title)
    joined = " ".join(words)
    if title == joined:
        total += 20.0
    elif title.startswith(joined):
        total += 8.0
    if entry.is_page:
        total += 1.0
    return total


def search(entries: list[Entry], query: str, limit: int = LIMIT) -> list[Entry]:
    """The entries matching every word of ``query``, best first (stable for ties)."""
    words = terms(query)
    if not words:
        return []
    scored = [(score(entry, words), index, entry) for index, entry in enumerate(entries)]
    found = sorted((item for item in scored if item[0] > 0), key=lambda item: (-item[0], item[1]))
    seen: set[tuple[str, str, str]] = set()
    result: list[Entry] = []
    for _score, _index, entry in found:
        key = (entry.page, entry.group, entry.title)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
        if len(result) >= limit:
            break
    return result
