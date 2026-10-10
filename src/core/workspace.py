"""The OpenDots workspace as Dotdeck sees it: Dots, Spaces, conversations and calls.

Built from ``GET /api/workspace`` and ``GET /api/state`` (upstream endpoints, so any
OpenDots server works). Pure data, no GTK: the sidebar, the tray and the settings
read it from the server monitor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTIVE_CALL = ("connecting", "active")
JOB_LIMIT = 60

# "# Manutenção — backups e o servidor de casa" (the first line Dotdeck reads the job from),
# with -, – or —.
_JOB_HEADER = re.compile(r"^#*\s*(?P<name>.+?)\s+[—–-]\s+(?P<job>.+)$")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def shorten(text: str, limit: int = JOB_LIMIT) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rstrip(" ,.;:—–-")
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut + "…"


def job_of(name: str, instructions: str) -> str:
    """A short "what this Dot does" line, from the first line of its instructions.

    ``# Atlas — chief of staff for the team`` gives ``chief of staff for the team``;
    plain prose gives its first sentence. There is no job field in OpenDots.
    """
    for line in instructions.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _JOB_HEADER.match(line)
        if match and match.group("name").strip("# ").casefold() == name.strip().casefold():
            return shorten(match.group("job").strip())
        line = line.lstrip("#").strip()
        sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
        return shorten(sentence)
    return ""


def with_job(name: str, instructions: str, job: str) -> str:
    """The instructions with their ``# Name — job`` header set to ``job`` (empty removes it)."""
    lines = instructions.splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    header = f"# {name} — {job.strip()}" if job.strip() else ""
    if first is not None and _JOB_HEADER.match(lines[first].strip()) and \
            lines[first].strip().startswith("#"):
        if header:
            lines[first] = header
        else:
            del lines[first]
            while lines and not lines[0].strip():
                del lines[0]
        return "\n".join(lines)
    if not header:
        return instructions
    return f"{header}\n\n{instructions}" if instructions.strip() else header


@dataclass(frozen=True)
class Dot:
    id: str
    name: str
    instructions: str = ""
    space_id: str = ""
    space_ids: tuple[str, ...] = ()
    research_allowed: bool = True
    memory_allowed: bool = True
    created_at: float = 0.0
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def job(self) -> str:
        return job_of(self.name, self.instructions)

    @classmethod
    def from_json(cls, data) -> Dot | None:
        if not isinstance(data, dict) or not _text(data.get("id")) or not _text(data.get("name")):
            return None
        spaces = data.get("spaceIds") if isinstance(data.get("spaceIds"), list) else []
        return cls(id=data["id"], name=data["name"], instructions=_text(data.get("instructions")),
                   space_id=_text(data.get("spaceId")),
                   space_ids=tuple(s for s in spaces if isinstance(s, str)),
                   research_allowed=data.get("researchAllowed") is not False,
                   memory_allowed=data.get("memoryAllowed") is not False,
                   created_at=_number(data.get("createdAt")), raw=dict(data))

    def update_body(self, **changes) -> dict:
        """The full body ``PUT /api/dots/:id`` wants (it replaces, and rejects extra keys)."""
        body = {"name": self.name, "instructions": self.instructions,
                "researchAllowed": self.research_allowed, "memoryAllowed": self.memory_allowed}
        for key in ("spaceId", "spaceIds", "learningContainerId", "skillDeliveryEnabled"):
            if key in self.raw:
                body[key] = self.raw[key]
        body.update(changes)
        return body


@dataclass(frozen=True)
class Space:
    id: str
    name: str
    description: str = ""

    @classmethod
    def from_json(cls, data) -> Space | None:
        if not isinstance(data, dict) or not _text(data.get("id")):
            return None
        return cls(id=data["id"], name=_text(data.get("name")) or data["id"],
                   description=_text(data.get("description")))


@dataclass(frozen=True)
class Conversation:
    id: str
    dot_id: str
    title: str
    created_at: float = 0.0

    @classmethod
    def from_json(cls, data) -> Conversation | None:
        if not isinstance(data, dict) or not _text(data.get("id")):
            return None
        return cls(id=data["id"], dot_id=_text(data.get("dotId")),
                   title=_text(data.get("title")), created_at=_number(data.get("createdAt")))


@dataclass(frozen=True)
class Call:
    id: str
    thread_id: str
    status: str
    started_at: float = 0.0
    ended_at: float = 0.0
    error: str = ""

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_CALL

    @classmethod
    def from_json(cls, data) -> Call | None:
        if not isinstance(data, dict) or not _text(data.get("id")):
            return None
        return cls(id=data["id"], thread_id=_text(data.get("threadId")),
                   status=_text(data.get("status")), started_at=_number(data.get("startedAt")),
                   ended_at=_number(data.get("endedAt")), error=_text(data.get("error")))


@dataclass(frozen=True)
class Memory:
    id: str
    text: str
    created_at: float = 0.0

    @classmethod
    def from_json(cls, data) -> Memory | None:
        if not isinstance(data, dict) or not _text(data.get("id")):
            return None
        return cls(id=data["id"], text=_text(data.get("text")),
                   created_at=_number(data.get("createdAt")))


@dataclass(frozen=True)
class Workspace:
    dots: tuple[Dot, ...] = ()
    spaces: tuple[Space, ...] = ()
    conversations: tuple[Conversation, ...] = ()
    calls: tuple[Call, ...] = ()
    setup: dict = field(default_factory=dict, compare=False)
    active_model: str = ""
    active_provider: str = ""

    @classmethod
    def from_json(cls, data) -> Workspace:
        if not isinstance(data, dict):
            return cls()

        def items(key, kind):
            values = data.get(key) if isinstance(data.get(key), list) else []
            return tuple(item for item in (kind.from_json(v) for v in values) if item)

        setup = data.get("setup") if isinstance(data.get("setup"), dict) else {}
        return cls(dots=items("dots", Dot), spaces=items("spaces", Space),
                   conversations=items("conversations", Conversation),
                   calls=items("calls", Call), setup=dict(setup),
                   active_model=_text(data.get("activeModel")),
                   active_provider=_text(data.get("activeProvider")))

    # -- lookups ---------------------------------------------------------------------------

    def dot(self, dot_id: str | None) -> Dot | None:
        return next((d for d in self.dots if d.id == dot_id), None)

    def dot_named(self, name: str | None) -> Dot | None:
        if not name:
            return None
        return next((d for d in self.dots if d.name == name), None) or \
            next((d for d in self.dots if d.name.casefold() == name.casefold()), None)

    def dot_index(self, dot_id: str) -> int:
        """Position in the server's order, which is also the order of the web page's list."""
        return next((i for i, d in enumerate(self.dots) if d.id == dot_id), -1)

    def space(self, space_id: str | None) -> Space | None:
        return next((s for s in self.spaces if s.id == space_id), None)

    def conversations_of(self, dot_id: str) -> list[Conversation]:
        return sorted((c for c in self.conversations if c.dot_id == dot_id),
                      key=lambda c: c.created_at, reverse=True)

    def latest_conversation(self, dot_id: str) -> Conversation | None:
        found = self.conversations_of(dot_id)
        return found[0] if found else None

    def conversation(self, conversation_id: str | None) -> Conversation | None:
        return next((c for c in self.conversations if c.id == conversation_id), None)

    def active_call(self) -> Call | None:
        return next((c for c in self.calls if c.active), None)

    def dot_in_call(self) -> Dot | None:
        call = self.active_call()
        conversation = self.conversation(call.thread_id) if call else None
        return self.dot(conversation.dot_id) if conversation else None

    @property
    def voice_ready(self) -> bool:
        return bool(self.setup.get("voice"))

    @property
    def missing(self) -> list[str]:
        return [m for m in self.setup.get("missing", []) or [] if isinstance(m, str)]

    def signature(self) -> tuple:
        """What the sidebar shows; it only rebuilds when this changes."""
        return (tuple((d.id, d.name, d.job) for d in self.dots),
                tuple((s.id, s.name) for s in self.spaces),
                tuple((c.id, c.dot_id, c.title) for c in self.conversations),
                tuple((c.id, c.status) for c in self.calls if c.active),
                bool(self.setup.get("voice")), self.active_model, self.active_provider)


@dataclass(frozen=True)
class ServerState:
    """``GET /api/state``: workspace settings, tasks and memories."""
    name: str = ""
    paused: bool = False
    research_allowed: bool = True
    memory_allowed: bool = True
    memories: tuple[Memory, ...] = ()
    configured: bool = True

    @classmethod
    def from_json(cls, data) -> ServerState:
        if not isinstance(data, dict):
            return cls()
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        memories = data.get("memories") if isinstance(data.get("memories"), list) else []
        return cls(name=_text(settings.get("name")), paused=settings.get("paused") is True,
                   research_allowed=settings.get("researchAllowed") is not False,
                   memory_allowed=settings.get("memoryAllowed") is not False,
                   memories=tuple(m for m in (Memory.from_json(v) for v in memories) if m),
                   configured=data.get("configured") is not False)
