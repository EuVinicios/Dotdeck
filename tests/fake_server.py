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
