#!/usr/bin/env python3
"""OpenDots updater: check the official project and update this server with one click.

Runs next to OpenDots as a user service and answers Dotdeck with the OpenDots owner token.
An update merges the official branch into the local one (local commits are kept), installs
dependencies when they changed, runs the project's own checks (typecheck and tests), builds,
restarts the service and checks it is healthy. Failing checks undo the merge before anything
is built or restarted; if a later step fails it returns the code, the build and the service
to how they were before.

    opendots_updater.py --repo ~/github/OpenDots [--port 4311] [--host 127.0.0.1]
                        [--service opendots.service] [--branch main]
                        [--checks "npm run typecheck; npm test" | --no-checks]

It listens on 127.0.0.1 unless told otherwise (--host, or UPDATER_HOST in the environment
or in the OpenDots .env); it never follows the HOST that OpenDots itself listens on.

Only the Python standard library, git, npm and systemctl are used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping

log = logging.getLogger("opendots-updater")

IDLE, RUNNING, DONE, FAILED, ROLLED_BACK = "idle", "running", "done", "failed", "rolled-back"
DEFAULT_HOST = "127.0.0.1"
# Without --checks: these package.json scripts, in this order, when the project has them.
DEFAULT_CHECKS = (("typecheck", ["npm", "run", "typecheck"]), ("test", ["npm", "test"]))
OUTPUT_LINES = 40     # lines of a failed command kept in the job
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def listen_host(flag: str | None, environ: Mapping[str, str], env_file: Mapping[str, str]) -> str:
    """--host, else UPDATER_HOST (process environment, then the OpenDots .env), else loopback.

    OpenDots' own HOST is deliberately ignored: OpenDots may serve the whole network while the
    updater, which rebuilds and restarts it, stays on this machine unless asked otherwise.
    """
    return flag or environ.get("UPDATER_HOST") or env_file.get("UPDATER_HOST") or DEFAULT_HOST


def parse_checks(text: str | None) -> list[list[str]] | None:
    """Commands separated by ";" ("none" or "off" turns the checks off).

    None (not configured) means the default: the typecheck and test scripts of package.json.
    """
    if text is None or not text.strip():
        return None
    if text.strip().lower() in ("none", "off", "no", "false", "0"):
        return []
    return [shlex.split(part) for part in text.split(";") if part.strip()]


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class StepFailed(Exception):
    def __init__(self, message: str, output: list[str] | None = None):
        super().__init__(message)
        self.output = output or []


class ChecksFailed(StepFailed):
    """The project's own checks failed on the merged code: nothing was built or restarted."""


class Updater:
    def __init__(self, repo: Path, service: str, health_url: str, token: str, state_dir: Path,
                 upstream_remote: str = "origin", upstream_branch: str = "main",
                 backup_remote: str = "backup", health_timeout: float = 90.0,
                 branch: str = "main", checks: list[list[str]] | None = None,
                 checks_timeout: float = 1200.0):
        self.repo = repo
        self.service = service
        self.health_url = health_url
        self.token = token
        self.upstream = f"{upstream_remote}/{upstream_branch}"
        self.upstream_remote = upstream_remote
        self.upstream_branch = upstream_branch
        self.backup_remote = backup_remote
        self.health_timeout = health_timeout
        self.branch = branch              # the local branch that is updated (and backed up)
        self.checks = checks              # None: the default scripts of package.json
        self.checks_timeout = checks_timeout
        self.state_path = state_dir / "state.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._preview_cache: tuple[tuple[str, str], dict] | None = None
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

    def _current_branch(self) -> str:
        """The checked-out branch, or "" on a detached HEAD."""
        return self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)

    # -- what an update would do -----------------------------------------------------------

    def preview(self) -> dict:
        """What merging the official branch now would do, without touching the checkout.

        ``conflicts``: files that would conflict with local commits (None when this git
        cannot tell, before 2.38). ``untracked``: files the official branch adds that exist
        here outside git (untracked or ignored), which a merge would refuse or overwrite.
        """
        head = self.git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        upstream = self.git("rev-parse", "--verify", "--quiet", self.upstream, check=False)
        if not head or not upstream:
            return {"conflicts": None, "untracked": []}
        with self._preview_lock:
            if self._preview_cache and self._preview_cache[0] == (head, upstream):
                return dict(self._preview_cache[1])
        merged_already = subprocess.run(["git", "merge-base", "--is-ancestor", upstream, head],
                                        cwd=self.repo, capture_output=True).returncode == 0
        result = {"conflicts": [], "untracked": []} if merged_already \
            else self._merge_preview(head, upstream)
        with self._preview_lock:
            self._preview_cache = ((head, upstream), result)
        return dict(result)

    def _merge_preview(self, head: str, upstream: str) -> dict:
        # --write-tree only adds objects to the repository; refs, index and files stay as is.
        merged = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only",
                                 "--no-messages", "-z", head, upstream],
                                cwd=self.repo, capture_output=True, text=True, timeout=300)
        tree, conflicts = "", None
        if merged.returncode in (0, 1):
            parts = merged.stdout.split("\0")
            tree = parts[0].strip()
            conflicts = sorted({part for part in parts[1:] if part}) if merged.returncode else []
        else:
            log.info("merge preview unavailable: %s", merged.stderr.strip())
        # Files the merge would add: from the merged tree, or (older git) from the official
        # side since the common ancestor.
        target = [head, tree] if tree else [f"{head}...{upstream}"]
        added = self.git("diff", "--name-only", "-z", "--no-renames", "--diff-filter=A",
                         *target, check=False).split("\0")
        untracked = sorted(path for path in added
                           if path and os.path.lexists(self.repo / path))
        return {"conflicts": conflicts, "untracked": untracked}

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
        preview = self.preview() if behind else {"conflicts": [], "untracked": []}
        return {"version": 2, "repository": self.repo.name, "service": self.service,
                "branch": self._current_branch(), "expected_branch": self.branch,
                "current": self._commit("HEAD"), "upstream": self._commit(self.upstream),
                "behind": behind, "ahead": ahead, "news": news, "dirty": dirty,
                "conflicts": preview["conflicts"], "untracked": preview["untracked"],
                "checked_at": self.checked_at, "job": job}

    def check(self) -> dict:
        with self._fetch_lock:
            self.git("fetch", "--quiet", self.upstream_remote, self.upstream_branch)
            self.checked_at = now()
            with self._lock:
                self._save()
        return self.status()

    # -- update ----------------------------------------------------------------------------

    def _refusal(self) -> str | None:
        """Why the checkout cannot be updated as it is now (None: it can)."""
        if self.git("status", "--porcelain", "--untracked-files=no", check=False):
            return "The server has uncommitted changes; commit or stash them before updating."
        branch = self._current_branch()
        if branch != self.branch:
            where = f"on branch {branch}" if branch else "on a detached HEAD"
            return (f"The server is {where}, not {self.branch}; run git checkout {self.branch} "
                    "there before updating.")
        untracked = self.preview()["untracked"]
        if untracked:
            return ("The official changes add files that already exist on the server outside "
                    f"git: {', '.join(untracked[:5])}{'…' if len(untracked) > 5 else ''}. "
                    "Move them away before updating.")
        return None

    def start_update(self) -> dict:
        with self._lock:
            if self.job.get("state") == RUNNING:
                raise StepFailed("An update is already running.")
            refusal = self._refusal()
            if refusal:
                raise StepFailed(refusal)
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

    def _run_command(self, step: str, command: list[str], timeout: float = 900,
                     env: dict[str, str] | None = None) -> None:
        self._step(step, " ".join(command))
        try:
            result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True,
                                    timeout=timeout, env=env)
            output, code = (result.stdout or "") + (result.stderr or ""), result.returncode
        except subprocess.TimeoutExpired as error:
            output, code = _text(error.stdout) + _text(error.stderr), None
        lines = [_ANSI.sub("", line).rstrip() for line in output.strip().splitlines()]
        tail = [line for line in lines if line.strip()][-OUTPUT_LINES:]
        with self._lock:
            self.job["log"] = (self.job.get("log") or [])[-60:] + tail[-15:]
            self._save()
        if code is None:
            raise StepFailed(f"{' '.join(command)} did not finish in {int(timeout)} s.", tail)
        if code != 0:
            raise StepFailed(f"{' '.join(command)} failed (exit {code}).", tail)

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

    def _dependencies_changed(self, a: str, b: str) -> bool:
        return bool(self.git("diff", "--name-only", a, b, "--",
                             "package.json", "package-lock.json"))

    def check_commands(self) -> list[list[str]]:
        """The checks run on the merged code (the default ones come from package.json)."""
        if self.checks is not None:
            return [list(command) for command in self.checks]
        try:
            scripts = json.loads((self.repo / "package.json").read_text("utf-8")).get("scripts")
        except (OSError, ValueError, AttributeError):
            scripts = None
        scripts = scripts if isinstance(scripts, dict) else {}
        return [list(command) for name, command in DEFAULT_CHECKS if name in scripts]

    def _run_checks(self) -> None:
        commands = self.check_commands()
        if not commands:
            self._step("checks", "No checks to run (no typecheck or test script, or turned "
                       "off).")
            return
        env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
        for command in commands:
            try:
                self._run_command("checks", command, timeout=self.checks_timeout, env=env)
            except StepFailed as error:
                raise ChecksFailed(str(error), error.output) from None

    def _deploy(self, step_prefix: str = "") -> None:
        self._run_command(f"{step_prefix}build", ["npm", "run", "build"])
        self._run_command(f"{step_prefix}restart",
                          ["systemctl", "--user", "restart", self.service], timeout=120)
        self._step(f"{step_prefix}health", "Waiting for OpenDots to answer…")
        if not self._healthy():
            raise StepFailed("OpenDots did not come back after the restart.")

    def _finish(self, state: str, message: str, output: list[str] | None = None) -> None:
        with self._lock:
            self.job.update(state=state, message=message, finished_at=now(),
                            to=self.git("rev-parse", "HEAD", check=False))
            if output:
                self.job["output"] = output[-OUTPUT_LINES:]
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
            refusal = self._refusal()     # the fetch may have brought new official files
            if refusal:
                self._finish(FAILED, f"{refusal} Nothing was changed.")
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
            if self._dependencies_changed(before, "HEAD"):
                self._run_command("install", ["npm", "ci", "--no-audit", "--no-fund"])
            # Before the build: failing checks leave dist/ and the running service alone.
            self._run_checks()
            self._deploy()
        except ChecksFailed as error:
            self._undo_merge(before, error)
            return
        except (StepFailed, subprocess.TimeoutExpired) as error:
            self._rollback(before, str(error))
            return
        if self.backup_remote in self.git("remote", check=False).split():
            ref = f"refs/heads/{self.branch}"
            pushed = subprocess.run(["git", "push", "--quiet", self.backup_remote, f"{ref}:{ref}"],
                                    cwd=self.repo, capture_output=True, text=True, timeout=120)
            if pushed.returncode != 0:
                self._step("backup", "Could not push to the backup remote (update kept).")
        self._finish(DONE, f"Updated to {self._commit('HEAD').get('short', '')}.")

    def _undo_merge(self, before: str, error: ChecksFailed) -> None:
        """The checks failed on the merged code: back to ``before``; nothing was rebuilt or
        restarted, so only the code (and the dependencies, if they changed) go back."""
        self._step("rollback", f"{error} Undoing the merge…")
        reason = (f"{error} The official changes were not applied; OpenDots was not rebuilt or "
                  "restarted and keeps running the previous version.")
        if error.output:
            reason += " Last lines:\n" + "\n".join(error.output[-10:])
        try:
            after = self.git("rev-parse", "HEAD")
            self.git("reset", "--hard", before)
            if self._dependencies_changed(before, after):
                self._run_command("rollback-install", ["npm", "ci", "--no-audit", "--no-fund"])
        except (StepFailed, subprocess.TimeoutExpired) as undo:
            self._finish(FAILED, f"{reason}\nRestoring the previous dependencies also failed "
                         f"({undo}); run npm ci on the server before OpenDots restarts.",
                         error.output)
            return
        self._finish(FAILED, reason, error.output)

    def _rollback(self, before: str, reason: str) -> None:
        self._step("rollback", f"{reason} Restoring the previous version…")
        try:
            after = self.git("rev-parse", "HEAD")
            self.git("reset", "--hard", before)
            # Reinstall if the failed version had different dependencies.
            if self._dependencies_changed(before, after):
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


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def serve(updater: Updater, host: str, port: int, interval_hours: float) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenDotsUpdater/2"

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
    parser.add_argument("--host", help=f"address to listen on (default: UPDATER_HOST, else "
                        f"{DEFAULT_HOST}; the HOST of OpenDots is not used)")
    parser.add_argument("--port", type=int, default=4311)
    parser.add_argument("--branch", default="main",
                        help="the local branch that is updated and backed up (default: main)")
    parser.add_argument("--checks", help='commands run on the merged code before the build, '
                        'separated by ";" (default: UPDATER_CHECKS, else the typecheck and '
                        'test scripts of package.json)')
    parser.add_argument("--no-checks", action="store_true", help="update without checks")
    parser.add_argument("--checks-timeout", type=float, default=1200.0,
                        help="seconds each check may take (default: 1200)")
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
    host = listen_host(args.host, os.environ, env)
    checks = [] if args.no_checks else parse_checks(
        args.checks if args.checks is not None
        else os.environ.get("UPDATER_CHECKS", env.get("UPDATER_CHECKS")))
    health = f"http://127.0.0.1:{env.get('PORT', '4310')}/api/state"
    updater = Updater(repo, args.service, health, token, args.state_dir, branch=args.branch,
                      checks=checks, checks_timeout=args.checks_timeout)
    server = serve(updater, host, args.port, args.interval_hours)
    log.info("Listening on %s:%s for %s (branch %s; checks: %s)", host, args.port, repo,
             args.branch, "; ".join(" ".join(c) for c in checks) if checks is not None
             else "the typecheck and test scripts of package.json")
    if not is_loopback(host):
        log.warning("Listening beyond this machine (%s): anyone who reaches port %s can try "
                    "the owner token. Prefer 127.0.0.1 unless Dotdeck runs elsewhere.",
                    host, args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
