"""The OpenDots updater against throwaway git repositories.

npm and systemctl are fakes on PATH for every subprocess: these tests never build anything
or restart a real service. ``fail-npm`` makes every npm command fail and ``fail-npm-<word>``
only the ones with that word (``fail-npm-test`` fails ``npm test``).
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
PACKAGE = json.dumps({"name": "opendots", "scripts": {
    "build": "vite build", "test": "vitest run", "typecheck": "tsc --noEmit"}}) + "\n"
CHECKS = ["npm run typecheck", "npm test"]


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
                              f"[ -f {root}/fail-{name} ] && exit 1\n"
                              f"for word in \"$@\"; do [ -f \"{root}/fail-{name}-$word\" ] && "
                              f"{{ echo \"FAIL tests/$word.test.ts > it breaks\"; "
                              f"printf '\\033[31mTests  1 failed\\033[0m\\n' >&2; exit 1; }}; "
                              f"done\nexit 0\n")
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
        (self.upstream_work / "package.json").write_text(PACKAGE)
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
        # The checks run on the merged code before anything is built or restarted.
        self.assertEqual(self.commands(), CHECKS + ["npm run build",
                                                    "systemctl --user restart opendots.service"])
        self.assertEqual(git(self.backup, "rev-parse", "main"), git(self.server, "rev-parse", "HEAD"))

    def test_installs_dependencies_only_when_they_change(self):
        self.official_commit("package.json", PACKAGE.replace('"opendots"', '"opendots", "v": 2'),
                             "deps: bump")
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

    def test_failing_checks_undo_the_merge_without_building_or_restarting(self):
        before = git(self.server, "rev-parse", "HEAD")
        self.official_commit("app.txt", "v2\n", "feat: breaks a test")
        (self.tmp / "fail-npm-test").write_text("")
        self.updater.start_update()
        status = self.wait_job()
        job = status["job"]
        self.assertEqual(job["state"], upd.FAILED, job)
        self.assertEqual(git(self.server, "rev-parse", "HEAD"), before)
        self.assertEqual((self.server / "app.txt").read_text(), "v1\n")
        self.assertEqual(git(self.server, "status", "--porcelain"), "")
        # Nothing built, nothing restarted: the running OpenDots never saw the new code.
        self.assertEqual(self.commands(), CHECKS)
        self.assertIn("npm test failed (exit 1)", job["message"])
        self.assertIn("not rebuilt or restarted", job["message"])
        self.assertIn("FAIL tests/test.test.ts > it breaks", job["message"])
        self.assertIn("Tests  1 failed", job["output"])          # colours stripped
        self.assertEqual(status["behind"], 1)                     # still to install

    def test_failing_checks_restore_the_previous_dependencies(self):
        self.official_commit("package.json", PACKAGE.replace('"opendots"', '"opendots", "v": 3'),
                             "deps: bump that breaks the types")
        (self.tmp / "fail-npm-typecheck").write_text("")
        self.updater.start_update()
        self.assertEqual(self.wait_job()["job"]["state"], upd.FAILED)
        self.assertEqual(self.commands(), ["npm ci --no-audit --no-fund", "npm run typecheck",
                                           "npm ci --no-audit --no-fund"])

    def test_checks_can_be_chosen_or_turned_off(self):
        self.official_commit("app.txt", "v2\n", "feat: v2")
        self.updater.checks = upd.parse_checks("npm run lint; npm test -- --run")
        self.assertEqual(self.updater.check_commands(),
                         [["npm", "run", "lint"], ["npm", "test", "--", "--run"]])
        self.updater.checks = upd.parse_checks("none")
        self.updater.start_update()
        self.assertEqual(self.wait_job()["job"]["state"], upd.DONE)
        self.assertEqual(self.commands(), ["npm run build", "systemctl --user restart opendots.service"])
        self.assertIsNone(upd.parse_checks(None))
        self.assertIsNone(upd.parse_checks("  "))     # unset or empty: the default scripts

    def test_default_checks_follow_package_json(self):
        (self.server / "package.json").write_text('{"name": "opendots", "scripts": {"test": "x"}}')
        self.assertEqual(self.updater.check_commands(), [["npm", "test"]])
        (self.server / "package.json").write_text("not json")
        self.assertEqual(self.updater.check_commands(), [])

    def test_status_previews_conflicts_before_updating(self):
        self.official_commit("docs.txt", "hi\n", "docs: readme")
        status = self.updater.check()
        self.assertEqual((status["conflicts"], status["untracked"]), ([], []))
        self.official_commit("local.txt", "official version\n", "feat: same file")
        status = self.updater.check()
        self.assertEqual(status["behind"], 2)
        self.assertEqual(status["conflicts"], ["local.txt"])
        self.assertEqual(git(self.server, "status", "--porcelain"), "")   # read-only
        self.assertFalse((self.server / ".git" / "MERGE_HEAD").exists())

    def test_refuses_files_outside_git_that_the_update_would_overwrite(self):
        self.official_commit("notes.txt", "official\n", "feat: notes")
        self.official_commit("cache.db", "official\n", "feat: ships a cache")
        (self.server / "notes.txt").write_text("mine, untracked\n")
        (self.server / ".git" / "info" / "exclude").write_text("cache.db\n")
        (self.server / "cache.db").write_text("mine, ignored\n")   # git would overwrite it
        status = self.updater.check()
        self.assertEqual(status["untracked"], ["cache.db", "notes.txt"])
        with self.assertRaises(upd.StepFailed) as caught:
            self.updater.start_update()
        self.assertIn("cache.db, notes.txt", str(caught.exception))
        self.assertEqual((self.server / "cache.db").read_text(), "mine, ignored\n")
        self.assertEqual(self.commands(), [])

    def test_refuses_another_branch_and_pushes_main_explicitly(self):
        self.official_commit("app.txt", "v2\n", "feat: v2")
        git(self.server, "checkout", "-q", "-b", "experiment")
        status = self.updater.check()
        self.assertEqual((status["branch"], status["expected_branch"]), ("experiment", "main"))
        with self.assertRaises(upd.StepFailed) as caught:
            self.updater.start_update()
        self.assertIn("on branch experiment, not main", str(caught.exception))
        git(self.server, "checkout", "-q", "--detach")
        with self.assertRaises(upd.StepFailed) as caught:
            self.updater.start_update()
        self.assertIn("detached HEAD", str(caught.exception))

        git(self.server, "checkout", "-q", "main")
        self.updater.start_update()
        self.assertEqual(self.wait_job()["job"]["state"], upd.DONE)
        self.assertEqual(git(self.backup, "rev-parse", "main"), git(self.server, "rev-parse", "main"))
        self.assertEqual(git(self.backup, "branch", "--format=%(refname:short)"), "main")

    def test_listens_on_this_machine_unless_told_otherwise(self):
        opendots_env = {"HOST": "0.0.0.0", "OWNER_TOKEN": TOKEN}
        self.assertEqual(upd.listen_host(None, {}, opendots_env), "127.0.0.1")
        self.assertEqual(upd.listen_host(None, {}, {**opendots_env, "UPDATER_HOST": "192.168.0.10"}),
                         "192.168.0.10")
        self.assertEqual(upd.listen_host(None, {"UPDATER_HOST": "::1"}, opendots_env), "::1")
        self.assertEqual(upd.listen_host("0.0.0.0", {"UPDATER_HOST": "::1"}, opendots_env),
                         "0.0.0.0")
        self.assertTrue(upd.is_loopback("127.0.0.1") and upd.is_loopback("::1"))
        self.assertFalse(upd.is_loopback("0.0.0.0"))

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
