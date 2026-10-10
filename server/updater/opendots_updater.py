#!/usr/bin/env python3
"""OpenDots updater: check the official project and update this server with one click.

Runs next to OpenDots as a user service and answers Dotdeck with the OpenDots owner token.
An update merges the official branch into the local one (local commits are kept), installs
dependencies when they changed, builds, restarts the service and checks it is healthy. If
any step fails it returns the code, the build and the service to how they were before.

    opendots_updater.py --repo ~/github/OpenDots [--port 4311] [--service opendots.service]

Only the Python standard library, git, npm and systemctl are used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("opendots-updater")

IDLE, RUNNING, DONE, FAILED, ROLLED_BACK = "idle", "running", "done", "failed", "rolled-back"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class StepFailed(Exception):
    pass


class Updater:
    def __init__(self, repo: Path, service: str, health_url: str, token: str, state_dir: Path,
                 upstream_remote: str = "origin", upstream_branch: str = "main",
                 backup_remote: str = "backup", health_timeout: float = 90.0):
        self.repo = repo
        self.service = service
        self.health_url = health_url
        self.token = token
        self.upstream = f"{upstream_remote}/{upstream_branch}"
        self.upstream_remote = upstream_remote
        self.upstream_branch = upstream_branch
        self.backup_remote = backup_remote
        self.health_timeout = health_timeout
        self.state_path = state_dir / "state.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        saved = self._load()
        self.checked_at = saved.get("checked_at")
        self.job = saved.get("job") or {"state": IDLE, "log": []}
        if self.job.get("state") == RUNNING:  # the updater itself was restarted mid-job
            self.job.update(state=FAILED, message="The updater stopped during the update.",
                            finished_at=now())

    # -- helpers ---------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"checked_at": self.checked_at, "job": self.job}, indent=2))
        os.replace(tmp, self.state_path)

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                                timeout=300)
        if check and result.returncode != 0:
            raise StepFailed(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
        return result.stdout.strip()

    def _commit(self, ref: str) -> dict:
        fmt = "%H%x1f%h%x1f%s%x1f%cI"
        try:
            full, short, subject, date = self.git("log", "-1", f"--format={fmt}", ref).split("\x1f")
        except (StepFailed, ValueError):
            return {}
        return {"commit": full, "short": short, "subject": subject, "date": date}

    # -- status and check ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            job = dict(self.job)
        news = []
        behind = ahead = 0
        try:
            behind = int(self.git("rev-list", "--count", f"HEAD..{self.upstream}"))
            ahead = int(self.git("rev-list", "--count", f"{self.upstream}..HEAD"))
            for line in self.git("log", "--format=%h%x1f%s%x1f%cI", "-n", "30",
                                 f"HEAD..{self.upstream}").splitlines():
                short, subject, date = line.split("\x1f")
                news.append({"short": short, "subject": subject, "date": date})
        except StepFailed as error:
            log.warning("status: %s", error)
        dirty = bool(self.git("status", "--porcelain", "--untracked-files=no", check=False))
        return {"version": 1, "repository": self.repo.name, "service": self.service,
                "current": self._commit("HEAD"), "upstream": self._commit(self.upstream),
                "behind": behind, "ahead": ahead, "news": news, "dirty": dirty,
                "checked_at": self.checked_at, "job": job}

    def check(self) -> dict:
        with self._fetch_lock:
            self.git("fetch", "--quiet", self.upstream_remote, self.upstream_branch)
            self.checked_at = now()
            with self._lock:
                self._save()
        return self.status()

    # -- update ----------------------------------------------------------------------------

    def start_update(self) -> dict:
        with self._lock:
            if self.job.get("state") == RUNNING:
                raise StepFailed("An update is already running.")
            if self.git("status", "--porcelain", "--untracked-files=no", check=False):
                raise StepFailed("The server has uncommitted changes; commit or stash them "
                                 "before updating.")
            self.job = {"state": RUNNING, "step": "fetch", "message": "", "log": [],
                        "started_at": now(), "finished_at": None,
                        "from": self.git("rev-parse", "HEAD")}
            self._save()
        threading.Thread(target=self._run, daemon=True).start()
        return self.status()

    def _step(self, step: str, message: str) -> None:
        with self._lock:
            self.job.update(step=step, message=message)
            self.job["log"] = (self.job.get("log") or [])[-60:] + [f"{now()} {message}"]
            self._save()
        log.info("%s: %s", step, message)

    def _run_command(self, step: str, command: list[str], timeout: int = 900) -> None:
        self._step(step, " ".join(command))
        result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True,
                                timeout=timeout)
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        with self._lock:
            self.job["log"] = (self.job.get("log") or [])[-60:] + tail
            self._save()
        if result.returncode != 0:
            raise StepFailed(f"{' '.join(command)} failed (exit {result.returncode}).")

    def _healthy(self) -> bool:
        deadline = time.monotonic() + self.health_timeout
        request = urllib.request.Request(self.health_url,
                                         headers={"Authorization": f"Bearer {self.token}"})
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(2)
        return False

    def _deploy(self, before: str, step_prefix: str = "") -> None:
        changed = self.git("diff", "--name-only", before, "HEAD", "--",
                           "package.json", "package-lock.json")
        if changed:
            self._run_command(f"{step_prefix}install", ["npm", "ci", "--no-audit", "--no-fund"])
        self._run_command(f"{step_prefix}build", ["npm", "run", "build"])
        self._run_command(f"{step_prefix}restart",
                          ["systemctl", "--user", "restart", self.service], timeout=120)
        self._step(f"{step_prefix}health", "Waiting for OpenDots to answer…")
        if not self._healthy():
            raise StepFailed("OpenDots did not come back after the restart.")

    def _finish(self, state: str, message: str) -> None:
        with self._lock:
            self.job.update(state=state, message=message, finished_at=now(),
                            to=self.git("rev-parse", "HEAD", check=False))
            self.job["log"] = (self.job.get("log") or [])[-60:] + [f"{now()} {message}"]
            self._save()
        log.info("update %s: %s", state, message)

    def _run(self) -> None:
        before = self.job["from"]
        try:
            self._step("fetch", f"Fetching {self.upstream}…")
            self.check()
            behind = int(self.git("rev-list", "--count", f"HEAD..{self.upstream}"))
            if behind == 0:
                self._finish(DONE, "Already up to date.")
                return
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.git("update-ref", f"refs/updater/before-{stamp}", before)
            self._step("merge", f"Merging {behind} official change(s), keeping local commits…")
            identity = [] if self.git("config", "user.email", check=False) else \
                ["-c", "user.name=OpenDots Updater", "-c", "user.email=updater@localhost"]
            merged = subprocess.run(
                ["git", *identity, "merge", "--no-edit", "-m",
                 f"Merge the official OpenDots ({self._commit(self.upstream).get('short', '')})",
                 self.upstream], cwd=self.repo, capture_output=True, text=True, timeout=300)
            if merged.returncode != 0:
                conflicts = self.git("diff", "--name-only", "--diff-filter=U", check=False)
                self.git("merge", "--abort", check=False)
                self._finish(FAILED, "The official changes conflict with local commits"
                             + (f" in: {', '.join(conflicts.split())}" if conflicts else "")
                             + ". Nothing was changed.")
                return
        except (StepFailed, subprocess.TimeoutExpired) as error:
            self.git("merge", "--abort", check=False)
            self._finish(FAILED, f"{error} Nothing was changed.")
            return
        try:
            self._deploy(before)
        except (StepFailed, subprocess.TimeoutExpired) as error:
            self._rollback(before, str(error))
            return
        if self.backup_remote in self.git("remote", check=False).split():
            pushed = subprocess.run(["git", "push", "--quiet", self.backup_remote, "HEAD:main"],
                                    cwd=self.repo, capture_output=True, text=True, timeout=120)
            if pushed.returncode != 0:
                self._step("backup", "Could not push to the backup remote (update kept).")
        self._finish(DONE, f"Updated to {self._commit('HEAD').get('short', '')}.")

    def _rollback(self, before: str, reason: str) -> None:
        self._step("rollback", f"{reason} Restoring the previous version…")
        try:
            after = self.git("rev-parse", "HEAD")
            self.git("reset", "--hard", before)
            # Reinstall if the failed version had different dependencies.
            changed = self.git("diff", "--name-only", before, after, "--",
                               "package.json", "package-lock.json")
            if changed:
                self._run_command("rollback-install", ["npm", "ci", "--no-audit", "--no-fund"])
            self._run_command("rollback-build", ["npm", "run", "build"])
            self._run_command("rollback-restart",
                              ["systemctl", "--user", "restart", self.service], timeout=120)
            healthy = self._healthy()
        except (StepFailed, subprocess.TimeoutExpired) as error:
            self._finish(FAILED, f"{reason} The rollback also failed: {error}")
            return
        self._finish(ROLLED_BACK, f"{reason} The previous version is back"
                     + ("." if healthy else ", but OpenDots is not answering yet."))


def serve(updater: Updater, host: str, port: int, interval_hours: float) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenDotsUpdater/1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _reply(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            return bool(updater.token) and hmac.compare_digest(supplied.encode(),
                                                               updater.token.encode())

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                self._reply(401, {"error": "Enter your owner access token."})
            elif self.path == "/status":
                self._reply(200, updater.status())
            else:
                self._reply(404, {"error": "Not found."})

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                self._reply(401, {"error": "Enter your owner access token."})
                return
            try:
                if self.path == "/check":
                    self._reply(200, updater.check())
                elif self.path == "/update":
                    self._reply(202, updater.start_update())
                else:
                    self._reply(404, {"error": "Not found."})
            except StepFailed as error:
                self._reply(409, {"error": str(error)})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    def periodic():
        while True:
            try:
                updater.check()
            except StepFailed as error:
                log.warning("periodic check: %s", error)
            time.sleep(max(0.1, interval_hours) * 3600)

    threading.Thread(target=periodic, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--service", default="opendots.service")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=4311)
    parser.add_argument("--state-dir", type=Path,
                        default=Path(os.environ.get("XDG_STATE_HOME",
                                                    Path.home() / ".local" / "state"))
                        / "opendots-updater")
    parser.add_argument("--interval-hours", type=float, default=6.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    repo = args.repo.expanduser().resolve()
    env = read_env(args.env_file or repo / ".env")
    token = env.get("OWNER_TOKEN", "")
    if len(token) < 24:
        log.error("OWNER_TOKEN missing in %s: the updater refuses to run without it.",
                  args.env_file or repo / ".env")
        return 2
    host = args.host or env.get("HOST", "127.0.0.1")
    health = f"http://127.0.0.1:{env.get('PORT', '4310')}/api/state"
    updater = Updater(repo, args.service, health, token, args.state_dir)
    server = serve(updater, host, args.port, args.interval_hours)
    log.info("Listening on %s:%s for %s", host, args.port, repo)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
