"""One Dotdeck at a time, without owning a name on D-Bus (used inside the Snap).

GApplication keeps a single instance by owning its application id on the session bus.
Under strict Snap confinement that needs a ``dbus`` slot, which the Snap Store only
grants after a manual review. Inside the snap Dotdeck therefore runs non-unique and does
the same job itself: the first process holds a lock in the snap's private runtime folder
and listens on a Unix socket there; a second launch (the app grid, ``--toggle``,
``--toggle-mute``) sends its command to the first one and exits.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

COMMANDS = ("activate", "toggle", "toggle-mute", "background")


def _runtime_dir() -> Path:
    path = Path(GLib.get_user_runtime_dir() or GLib.get_tmp_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


class SingleInstance:
    def __init__(self, folder: Path | None = None, name: str = "dotdeck"):
        folder = folder or _runtime_dir()
        self.lock_path = folder / f"{name}.lock"
        self.socket_path = folder / f"{name}.socket"
        self._lock_fd: int | None = None
        self._service: Gio.SocketService | None = None

    def acquire(self) -> bool:
        """True when this process is the first one (it keeps the lock until it exits)."""
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def send(self, command: str, timeout: float = 3.0) -> bool:
        """Hand ``command`` to the running Dotdeck (from a second launch)."""
        if command not in COMMANDS:
            raise ValueError(command)
        client = Gio.SocketClient(timeout=max(1, int(timeout)))
        try:
            connection = client.connect(Gio.UnixSocketAddress.new(str(self.socket_path)), None)
            connection.get_output_stream().write_all(f"{command}\n".encode(), None)
            connection.close(None)
            return True
        except GLib.Error as error:
            log.warning("Could not reach the running Dotdeck: %s", error.message)
            return False

    def listen(self, on_command: Callable[[str], None]) -> None:
        """Run ``on_command(command)`` on the main loop for every later launch."""
        if self._lock_fd is None:
            raise RuntimeError("listen() needs the lock")
        try:
            self.socket_path.unlink()  # left over by a process that crashed
        except FileNotFoundError:
            pass
        service = Gio.SocketService()
        service.add_address(Gio.UnixSocketAddress.new(str(self.socket_path)),
                            Gio.SocketType.STREAM, Gio.SocketProtocol.DEFAULT, None)
        os.chmod(self.socket_path, 0o600)

        def incoming(_service, connection, _source) -> bool:
            stream = Gio.DataInputStream.new(connection.get_input_stream())
            line, _length = stream.read_line_utf8(None)
            command = (line or "").strip()
            if command in COMMANDS:
                GLib.idle_add(lambda: (on_command(command), False)[1])
            else:
                log.debug("Ignoring %r on the instance socket", command)
            return True

        service.connect("incoming", incoming)
        service.start()
        self._service = service

    def close(self) -> None:
        if self._service is not None:
            self._service.stop()
            self._service.close()
            self._service = None
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
