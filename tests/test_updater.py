"""The OpenDots updater against throwaway git repositories.

npm and systemctl are fakes on PATH for every subprocess: these tests never build anything
or restart a real service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "updater"))

import opendots_updater as upd  # noqa: E402

TOKEN = "owner-token-for-updater-tests-0123"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Health:
    def __init__(self):
        self.ok = True
        health = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                good = health.ok and self.headers.get("Authorization") == f"Bearer {TOKEN}"
                self.send_response(200 if good else 503)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/state"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="updater-"))
        root = self.tmp
        self.calls = root / "calls.log"
        fakebin = root / "bin"
        fakebin.mkdir()
        for name in ("npm", "systemctl"):
            script = fakebin / name
            script.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {self.calls}\n"
                              f"[ -f {root}/fail-{name} ] && exit 1\nexit 0\n")
            script.chmod(0o755)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{fakebin}:{self._path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))
        for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ[key] = "Test"
        for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ[key] = "test@example.invalid"

        self.official = root / "official.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.official))
        self.backup = root / "backup.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.backup))
        self.upstream_work = root / "upstream"
        git(root, "clone", "-q", str(self.official), str(self.upstream_work))
        (self.upstream_work / "package.json").write_text('{"name": "opendots"}\n')
        (self.upstream_work / "app.txt").write_text("v1\n")
        git(self.upstream_work, "add", ".")
        git(self.upstream_work, "commit", "-q", "-m", "initial")
        git(self.upstream_work, "push", "-q", "origin", "HEAD:main")

        self.server = root / "server"
        git(root, "clone", "-q", str(self.official), str(self.server))
        (self.server / "local.txt").write_text("my change\n")
        git(self.server, "add", ".")
        git(self.server, "commit", "-q", "-m", "local: my change")
        git(self.server, "remote", "add", "backup", str(self.backup))

        self.health = Health()
        self.addCleanup(self.health.httpd.shutdown)
        self.updater = upd.Updater(self.server, "opendots.service", self.health.url, TOKEN,
                                   root / "state", health_timeout=3)

    def official_commit(self, path: str, content: str, message: str) -> None:
        git(self.upstream_work, "pull", "-q", "--rebase", "origin", "main")
        (self.upstream_work / path).write_text(content)
        git(self.upstream_work, "add", ".")
        git(self.upstream_work, "commit", "-q", "-m", message)
        git(self.upstream_work, "push", "-q", "origin", "HEAD:main")

    def wait_job(self) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.updater.status()
            if status["job"]["state"] != upd.RUNNING:
                return status
            time.sleep(0.05)
        self.fail("update did not finish")

    def commands(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_detects_news_and_updates_keeping_local_commits(self):
        status = self.updater.check()
        self.assertEqual((status["behind"], status["ahead"]), (0, 1))
        self.official_commit("app.txt", "v2\n", "feat: dark mode")
        self.official_commit("docs.txt", "hi\n", "docs: readme")
        status = self.updater.check()
        self.assertEqual(status["behind"], 2)
        self.assertEqual([n["subject"] for n in status["news"]], ["docs: readme", "feat: dark mode"])

        self.updater.start_update()
        status = self.wait_job()
        self.assertEqual(status["job"]["state"], upd.DONE, status["job"])
        self.assertEqual((status["behind"], status["ahead"]), (0, 2))  # local commit + merge
        self.assertEqual((self.server / "app.txt").read_text(), "v2\n")
        self.assertEqual((self.server / "local.txt").read_text(), "my change\n")
        self.assertEqual(self.commands(), ["npm run build", "systemctl --user restart opendots.service"])
        self.assertEqual(git(self.backup, "rev-parse", "main"), git(self.server, "rev-parse", "HEAD"))

    def test_installs_dependencies_only_when_they_change(self):
        self.official_commit("package.json", '{"name": "opendots", "v": 2}\n', "deps: bump")
        self.updater.start_update()
        self.assertEqual(self.wait_job()["job"]["state"], upd.DONE)
        self.assertEqual(self.commands()[0], "npm ci --no-audit --no-fund")

    def test_conflict_changes_nothing(self):
        before = git(self.server, "rev-parse", "HEAD")
        self.official_commit("local.txt", "official version\n", "feat: same file")
        self.updater.start_update()
        status = self.wait_job()
        self.assertEqual(status["job"]["state"], upd.FAILED)
        self.assertIn("local.txt", status["job"]["message"])
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.server, "status", "--porcelain"), "")
        self.assertEqual(self.commands(), [])

    def test_rolls_back_when_the_service_does_not_come_back(self):
        before = git(self.server, "rev-parse", "HEAD")
        self.official_commit("app.txt", "broken\n", "feat: broken")
        self.health.ok = False
        self.updater.start_update()
        time.sleep(0.5)
        status = self.wait_job()
        self.assertEqual(status["job"]["state"], upd.ROLLED_BACK, status["job"])
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), before)
        self.assertEqual((self.server / "app.txt").read_text(), "v1\n")
        self.assertEqual(self.commands().count("systemctl --user restart opendots.service"), 2)

    def test_build_failure_rolls_back(self):
        before = git(self.server, "rev-parse", "HEAD")
        self.official_commit("app.txt", "v3\n", "feat: v3")
        (self.tmp / "fail-npm").write_text("")
        self.updater.start_update()
        status = self.wait_job()
        self.assertIn(status["job"]["state"], (upd.ROLLED_BACK, upd.FAILED))
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), before)

    def test_refuses_dirty_tree_and_parallel_updates(self):
        (self.server / "local.txt").write_text("unsaved\n")
        with self.assertRaises(upd.StepFailed):
            self.updater.start_update()

    def test_http_requires_the_owner_token(self):
        server = upd.serve(self.updater, "127.0.0.1", 0, 1000)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base}/status", timeout=5)
        self.assertEqual(caught.exception.code, 401)
        request = urllib.request.Request(f"{base}/status",
                                         headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
        self.assertEqual(data["ahead"], 1)
        self.assertEqual(data["current"]["subject"], "local: my change")


if __name__ == "__main__":
    unittest.main()
