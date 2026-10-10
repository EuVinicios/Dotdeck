"""A tiny stand-in for an OpenDots server, for tests and screenshots.

It answers the read-only endpoints Dotdeck uses (``/api/state`` and ``/api/workspace``)
with the same status codes as the real server (401 without the owner token, 403 for
unknown hosts when it runs without one) and serves a configurable page at ``/``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PAGE = "<!doctype html><title>OpenDots</title><h1>OpenDots</h1>"


class FakeOpenDots:
    def __init__(self, token: str | None = None, local_only: bool = False,
                 name: str = "OpenDots Test", dots: list[str] | None = None,
                 page: str = DEFAULT_PAGE):
        self.token = token
        self.local_only = local_only
        self.name = name
        self.dots = dots if dots is not None else ["Chief", "Researcher"]
        self.tasks: list[dict] = []
        self.calls: list[dict] = []
        self.page = page
        self.requests: list[tuple[str, str | None]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, data) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (http.server naming)
                auth = self.headers.get("Authorization")
                fake.requests.append((self.path, auth))
                if not self.path.startswith("/api/"):
                    body = fake.page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if fake.local_only:
                    self._json(403, {"error": "Unrecognized host."})
                    return
                if fake.token and auth != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token to unlock OpenDots."})
                    return
                if self.path == "/api/state":
                    self._json(200, {"settings": {"name": fake.name, "paused": False},
                                     "tasks": fake.tasks, "memories": [], "mode": "live",
                                     "configured": True})
                elif self.path == "/api/workspace":
                    self._json(200, {"spaces": [], "conversations": [], "calls": fake.calls,
                                     "dots": [{"id": f"d{i}", "name": n}
                                              for i, n in enumerate(fake.dots)],
                                     "setup": {"missing": []}})
                else:
                    self._json(404, {"error": "Not found."})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeOpenDots:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def start(self) -> FakeOpenDots:
        return self.__enter__()

    def stop(self) -> None:
        self.__exit__()


class FakeUpdater:
    """Stand-in for server/updater: two official changes, then an update that finishes."""

    def __init__(self, token: str, behind: int = 2):
        self.token = token
        self.behind = behind
        self.polls_until_done = 2
        self.updates_started = 0
        self.job = {"state": "idle", "log": []}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, data) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _ok(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token."})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if self._ok():
                    if fake.job["state"] == "running":
                        fake.polls_until_done -= 1
                        if fake.polls_until_done <= 0:
                            fake.job = {"state": "done", "message": "Updated to abc1234.",
                                        "finished_at": "2026-10-10T01:00:00+00:00"}
                            fake.behind = 0
                    self._json(200, fake.status())

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._ok():
                    return
                if self.path == "/update":
                    fake.updates_started += 1
                    fake.job = {"state": "running", "step": "build", "message": "npm run build"}
                    self._json(202, fake.status())
                else:
                    self._json(200, fake.status())

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def status(self) -> dict:
        news = [{"short": "c0ffee1", "subject": "feat: dark mode", "date": "2026-10-09T12:00:00+00:00"},
                {"short": "c0ffee2", "subject": "fix: voice receipts", "date": "2026-10-08T12:00:00+00:00"}]
        return {"version": 1, "behind": self.behind, "ahead": 5, "dirty": False,
                "news": news[:self.behind],
                "current": {"short": "083a6b4", "subject": "fix(telegram): tokens",
                            "date": "2026-10-09T22:00:00+00:00"},
                "upstream": {"short": "c0ffee1", "subject": "feat: dark mode",
                             "date": "2026-10-09T12:00:00+00:00"},
                "job": self.job}

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
