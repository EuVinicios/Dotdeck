"""Dotdeck's Settings window (one instance, a sidebar of sections and search).

Pages: general, appearance, notifications, shortcuts (APP); dots, voice, model
(YOUR DOTS); servers, privacy (SERVER); advanced, about (SYSTEM).
"""

from __future__ import annotations

PAGES = ("general", "appearance", "notifications", "shortcuts", "dots", "voice", "model",
         "servers", "privacy", "advanced", "about")


def open_settings(app, page: str | None = "general", *, add_server: bool = False,
                  edit: str | None = None, dot: str | None = None) -> None:
    """Show the Settings window at ``page`` (``None``: the page seen last).

    ``add_server`` opens the Add Server form, ``edit`` the page of that server profile,
    ``dot`` the editor of that Dot (an id on the active server).
    """
    from .window import present
    present(app, page, add_server=add_server, edit=edit, dot=dot)


def get_window():
    """The open Settings window, or None."""
    from .window import current
    return current()
