#!/usr/bin/env python3
"""A stand-in OpenDots server with demo Dots, for screenshots and manual checks.

It serves a real OpenDots web client build (the ``dist/client`` folder of an OpenDots
checkout, after ``npm run build``) and answers the read-only API with fictional data,
so screenshots never show anyone's real Dots:

    python3 tools/demo_server.py --client ~/github/OpenDots/dist/client --port 4399

Chats need CopilotKit Intelligence and are not simulated: every Dot shows its empty
conversation, which is what a new user sees.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "demo-owner-token-0123456789abcdef"
NOW = int(time.time() * 1000)

DOTS = [
    ("Chief", "chief of staff, keeps everyone in sync",
     "Coordinate the other Dots, keep a short daily brief and make sure nothing falls through."),
    ("Researcher", "market and product research",
     "Find reliable sources, compare options and summarize what matters with links."),
    ("Writer", "posts, emails and documents",
     "Draft clear, friendly text in the owner's voice and keep it short."),
    ("Finance", "budgets, invoices and investments",
     "Track spending against the budget, flag unusual charges and explain the numbers simply."),
    ("Homelab", "keeps the home server healthy",
     "Watch backups, updates and disk space, and fix small problems before they grow."),
    ("Workshop", "code, scripts and automations",
     "Write and test small scripts and automations in the sandbox before anything runs."),
    ("Advisor", "strategy and second opinions",
     "Challenge plans with good questions and lay out trade-offs before decisions."),
    ("Scholar", "papers, citations and study plans",
     "Read papers, keep citations straight and turn big topics into study plans."),
]
SPACES = [("Everyday", "Personal errands and notes"), ("Product launch", "Launch plan and copy"),
          ("Home & infrastructure", "The homelab"), ("Research notes", "Sources and summaries")]


def workspace() -> dict:
    spaces = [{"id": f"space-{i}", "name": name, "description": text, "createdAt": NOW - i}
              for i, (name, text) in enumerate(SPACES)]
    dots = [{"id": f"dot-{i}", "spaceId": "space-0", "spaceIds": ["space-0"], "name": name,
             "instructions": f"# {name} — {job}\n\n{text}", "researchAllowed": True,
             "memoryAllowed": True, "createdAt": NOW - 10_000 + i, "learningContainerId": None,
             "skillDeliveryEnabled": False}
            for i, (name, job, text) in enumerate(DOTS)]
    return {"spaces": spaces, "dots": dots, "conversations": [], "calls": [],
            "setup": {"intelligence": True, "model": True, "browser": True, "voice": True,
                      "slack": "not_configured", "missing": []}}


def state() -> dict:
    return {"settings": {"name": "OpenDots Demo", "paused": False, "researchAllowed": True,
                         "memoryAllowed": True},
            "tasks": [{"id": "task-1", "prompt": "Weekly market summary", "status": "running",
                       "intervalSeconds": 604800, "nextRunAt": NOW + 3600_000, "createdAt": NOW}],
            "memories": [{"id": f"m{i}", "text": text, "createdAt": NOW - i} for i, text in
                         enumerate(["Prefers short answers", "Works from Lisbon",
                                    "Budget review on Fridays"])],
            "mode": "live", "configured": True}


class Handler(BaseHTTPRequestHandler):
    client: Path

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(401, {"error": "Enter your owner access token to continue."})
            elif path == "/api/state":
                self._json(200, state())
            elif path == "/api/workspace":
                self._json(200, workspace())
            elif path.startswith("/api/copilotkit/threads"):
                self._json(200, {"threads": [], "nextCursor": None})
            else:
                self._json(404, {"error": "Not found."})
            return
        target = (self.client / path.lstrip("/")).resolve()
        if not str(target).startswith(str(self.client)) or not target.is_file():
            target = self.client / "index.html"
        kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)

    def do_POST(self) -> None:  # noqa: N802
        self._json(403, {"error": "This is a read-only demo."})

    do_PUT = do_PATCH = do_DELETE = do_POST


def serve(client: Path, port: int = 0) -> ThreadingHTTPServer:
    handler = type("DemoHandler", (Handler,), {"client": client.resolve()})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4399)
    args = parser.parse_args()
    httpd = serve(args.client, args.port)
    print(f"Demo OpenDots on http://127.0.0.1:{httpd.server_port} (token {TOKEN})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
