"""Background watch on one OpenDots server: connection state, Dots and tasks.

The web page only runs while its window is open. The monitor keeps polling the
read-only endpoints so Dotdeck can show status in the tray and the header bar and
send desktop notifications when a Dot finishes, fails or starts a scheduled task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib, GObject

from . import server
from .store import Profile

log = logging.getLogger(__name__)

TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_TRIGGERED = "triggered"

ACTIVE_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class TaskSnapshot:
    id: str
    prompt: str
    status: str
    interval: int | None
    next_run_at: float | None
    error: str

    @classmethod
    def from_json(cls, data: dict) -> TaskSnapshot | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        interval = data.get("intervalSeconds")
        next_run = data.get("nextRunAt")
        return cls(
            id=data["id"],
            prompt=str(data.get("prompt") or ""),
            status=str(data.get("status") or ""),
            interval=interval if isinstance(interval, int) else None,
            next_run_at=next_run if isinstance(next_run, (int, float)) else None,
            error=str(data.get("error") or ""),
        )


def task_events(previous: dict[str, TaskSnapshot],
                current: list[TaskSnapshot]) -> list[tuple[str, TaskSnapshot]]:
    """What changed between two polls, as ``(kind, task)`` pairs (pure, unit tested).

    New tasks are ignored: the user just created them. A recurring task goes back to
    ``queued`` after each run, so "completed" also covers running → queued, and a run
    that happened entirely between two polls shows up as an advanced ``nextRunAt``.
    """
    events: list[tuple[str, TaskSnapshot]] = []
    for task in current:
        before = previous.get(task.id)
        if before is None:
            continue
        if task.status == "failed" and before.status != "failed":
            events.append((TASK_FAILED, task))
            continue
        if task.interval and task.status == "running" and before.status != "running":
            events.append((TASK_TRIGGERED, task))
            continue
        finished = (before.status == "running" and task.status in ("completed", "queued")) or \
            (task.status == "completed" and before.status != "completed")
        rescheduled = bool(task.interval and before.next_run_at and task.next_run_at
                           and task.next_run_at > before.next_run_at)
        if finished or (rescheduled and task.status == before.status):
            events.append((TASK_COMPLETED, task))
    return events


class ServerMonitor(GObject.Object):
    """Polls ``/api/state`` and ``/api/workspace`` of one profile."""

    __gtype_name__ = "DotdeckServerMonitor"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "task-event": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
        "updates-available": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
    }

    INTERVAL = 20          # seconds between polls while healthy
    MAX_INTERVAL = 300     # backoff ceiling while the server is down
    UPDATE_INTERVAL = 1800  # seconds between updater status checks

    def __init__(self, profile: Profile, token_lookup: Callable[[Callable[[str | None], None]], None]):
        super().__init__()
        self.profile = profile
        self._token_lookup = token_lookup
        self._timer = 0
        self._cancellable: Gio.Cancellable | None = None
        self._tasks: dict[str, TaskSnapshot] | None = None
        self._failures = 0
        # Public state for the UI.
        self.status = "unknown"          # unknown | online | offline | token | local-only | other
        self.name = ""
        self.dots: list[str] = []
        self.active_tasks = 0
        self.active_calls = 0
        self.missing: list[str] = []
        self.voice_ready = False
        # The server's updater (server/updater/): None until asked, {} when not installed.
        self.updates: dict | None = None
        self._update_timer = 0
        self._announced_upstream = ""

    @property
    def online(self) -> bool:
        return self.status == "online"

    def start(self) -> None:
        if not self._timer:
            self.poll_now()

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._update_timer:
            GLib.source_remove(self._update_timer)
            self._update_timer = 0
        if self._cancellable:
            self._cancellable.cancel()
            self._cancellable = None

    def poll_now(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._cancellable:
            self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()
        cancellable = self._cancellable
        self._token_lookup(lambda token: self._poll(token, cancellable))

    def _schedule(self) -> None:
        delay = self.INTERVAL if self._failures == 0 else \
            min(self.MAX_INTERVAL, self.INTERVAL * (2 ** min(self._failures, 4)))
        self._timer = GLib.timeout_add_seconds(delay, self._on_timer)

    def _on_timer(self) -> bool:
        self._timer = 0
        self.poll_now()
        return GLib.SOURCE_REMOVE

    def _poll(self, token: str | None, cancellable: Gio.Cancellable) -> None:
        if cancellable.is_cancelled():
            return
        client = server.ServerClient(self.profile.url, token)
        client.get_json("/api/state", lambda s, d, e: self._got_state(client, s, d, e, cancellable),
                        cancellable)

    def _got_state(self, client, status, data, error, cancellable) -> None:
        if cancellable.is_cancelled():
            return
        if error is not None or status != 200 or not isinstance(data, dict):
            kind = server.UNREACHABLE if error is not None else \
                server.classify(status, data, bool(client.token)).kind
            self._failures += 1
            self.status = {server.TOKEN_REQUIRED: "token", server.TOKEN_INVALID: "token",
                           server.LOCAL_ONLY: "local-only", server.UNREACHABLE: "offline"}.get(
                               kind, "other")
            self.active_tasks = 0
            self.emit("changed")
            self._schedule()
            return
        self._failures = 0
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        self.name = str(settings.get("name") or "")
        tasks = [t for t in (TaskSnapshot.from_json(item) for item in data.get("tasks", []) or [])
                 if t]
        if self._tasks is not None:
            for kind, task in task_events(self._tasks, tasks):
                self.emit("task-event", kind, task.id, task.prompt, task.error)
        self._tasks = {task.id: task for task in tasks}
        self.active_tasks = sum(1 for t in tasks if t.status == "running")
        client.get_json("/api/workspace",
                        lambda s, d, e: self._got_workspace(s, d, cancellable), cancellable)

    def _got_workspace(self, status, data, cancellable) -> None:
        if cancellable.is_cancelled():
            return
        if status == 200 and isinstance(data, dict):
            self.dots = [str(d.get("name")) for d in data.get("dots", []) or []
                         if isinstance(d, dict) and d.get("name")]
            self.active_calls = sum(1 for c in data.get("calls", []) or []
                                    if isinstance(c, dict) and c.get("status") in
                                    ("connecting", "active"))
            setup = data.get("setup") if isinstance(data.get("setup"), dict) else {}
            self.missing = [m for m in setup.get("missing", []) or [] if isinstance(m, str)]
            self.voice_ready = bool(setup.get("voice"))
        self.status = "online"
        self.emit("changed")
        self._schedule()
        if not self._update_timer:
            self.poll_updates()

    def poll_updates(self) -> None:
        """Ask the server's updater what changed in the official OpenDots."""
        from .updates import UpdaterClient
        if self._update_timer:
            GLib.source_remove(self._update_timer)
        self._update_timer = GLib.timeout_add_seconds(self.UPDATE_INTERVAL, self._on_update_timer)
        self._token_lookup(lambda token: UpdaterClient(self.profile, token).status(
            self._got_updates))

    def _on_update_timer(self) -> bool:
        self._update_timer = 0
        self.poll_updates()
        return GLib.SOURCE_REMOVE

    def _got_updates(self, data: dict | None, error: str | None) -> None:
        self.set_updates(data if data is not None else {})

    def set_updates(self, data: dict) -> None:
        self.updates = data
        behind = int(data.get("behind") or 0) if data else 0
        upstream = str((data.get("upstream") or {}).get("short") or "") if data else ""
        if behind and upstream and upstream != self._announced_upstream:
            self._announced_upstream = upstream
            self.emit("updates-available", behind, upstream)
        self.emit("changed")


def chief_dot(profile: Profile, dots: list[str]) -> str | None:
    """The Dot behind "Talk to the chief": the one chosen, else a well-known name, else the first."""
    if profile.chief and profile.chief in dots:
        return profile.chief
    for name in ("Chefe", "Chief", "Hermes", "Boss", "Coordinator", "Coordenador"):
        for dot in dots:
            if dot.casefold() == name.casefold():
                return dot
    return dots[0] if dots else None
