"""Shared test utilities."""

from __future__ import annotations

import os
import tempfile
import time

# Isolate every test run from the user's real config, keyring and audio devices.
_ROOT = tempfile.mkdtemp(prefix="dotdeck-tests-")
for _name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = os.path.join(_ROOT, _name.lower())
os.environ.setdefault("DOTDECK_SECRETS", "memory")
os.environ.setdefault("DOTDECK_TEST_AUDIO", "1")

from gi.repository import GLib  # noqa: E402


def run_until(predicate, timeout: float = 10.0) -> bool:
    """Iterate the default main context until ``predicate()`` is true."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.002)
    return predicate()


def tmp_path(name: str) -> str:
    return os.path.join(_ROOT, name)
