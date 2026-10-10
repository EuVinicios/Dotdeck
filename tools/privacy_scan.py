#!/usr/bin/env python3
"""Look for secrets and private addresses in what Dotdeck ships (a CI guard).

    python3 tools/privacy_scan.py              # the files tracked by git
    python3 tools/privacy_scan.py DIR [DIR…]   # any folder, e.g. an unpacked snap

Dotdeck is public: a stranger who installs it must get an empty app, with nobody's
servers, keys or folders in it. This scan looks for generic shapes only (never anyone's
real values): API keys (``sk-or-``, ``sk-ant-``, ``sk-proj-``), Telegram bot tokens, home
folders (``/home/<name>``), private and Tailscale addresses (IPv4, and Tailscale's IPv6
prefix ``fd7a:115c:a1e0``) and ``*.ts.net`` names. The examples the help texts use are
allowed: 192.168.0.10, localhost and 127.0.0.1, and the Tailscale name ``tail1234``; a
line that is an example on purpose (a test of address parsing) says so with
``privacy: example``. Networks (``100.64.0.0/10``) are not addresses. Each finding prints
as ``file:line: kind`` with the value masked, and the scan exits with 1.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 8_000_000

# Placeholders the docs and tests use instead of a real name.
HOME_EXAMPLES = {"user", "username", "you", "your-user", "runner", "ubuntu", "me", "seu-usuario",
                 "usuario"}
# Addresses in help texts and placeholders (ui/server_form.py, README).
IP_EXAMPLES = {"192.168.0.10", "127.0.0.1", "0.0.0.0"}
TAILNET_EXAMPLES = {"tail1234"}
MARKER = "privacy: example"

PATTERNS = [
    ("OpenRouter key", re.compile(r"sk-or-(?:v1-)?[A-Za-z0-9]{24,}")),
    ("Anthropic key", re.compile(r"sk-ant-(?:[a-z]{3}\d{2}-)?[A-Za-z0-9_-]{24,}")),
    ("OpenAI key", re.compile(r"sk-proj-[A-Za-z0-9_-]{24,}|\bsk-[A-Za-z0-9]{40,}")),
    ("Telegram bot token", re.compile(r"\b\d{6,}:AA[\w-]{30,}")),
    ("home folder", re.compile(r"/home/([a-z_][a-z0-9_.-]{0,31})")),
    # Four numbers, not part of a longer dotted version (1.2.3.4.5) nor a network
    # (10.0.0.0/8); the full stop that ends a sentence is not part of the address.
    ("private address", re.compile(r"(?<!\d)(?<!\d\.)(\d{1,3}(?:\.\d{1,3}){3})"
                                   r"(?!\d|\.\d|/\d)")),
    ("Tailscale address", re.compile(r"(?<![0-9a-f:])fd7a:115c:a1e0(?::[0-9a-f]{0,4}){1,5}"
                                     r"(?![0-9a-f:]|/\d)", re.IGNORECASE)),
    ("Tailscale name", re.compile(r"\b[\w-]+\.(tail[0-9a-z]+)\.ts\.net\b")),
]


def _allowed(kind: str, match: re.Match) -> bool:
    if kind == "home folder":
        return match.group(1).rstrip(".-") in HOME_EXAMPLES    # "/home/user." ends a sentence
    if kind == "private address":
        text = match.group(1)
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return True                     # a version number, not an address
        tailnet = address in ipaddress.ip_network("100.64.0.0/10")
        return text in IP_EXAMPLES or not (address.is_private or tailnet) or \
            address.is_loopback
    if kind == "Tailscale name":
        return match.group(1) in TAILNET_EXAMPLES
    return False


def mask(value: str) -> str:
    return value[:6] + "…" if len(value) > 8 else "…"


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """``(line, kind, masked value)`` for each finding in ``text``."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if MARKER in line:
            continue
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if not _allowed(kind, match):
                    found.append((number, kind, mask(match.group(0))))
    return found


def tracked_files(root: Path) -> list[Path]:
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True,
                                capture_output=True).stdout.decode().split("\0")
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [root / name for name in listed if name]


def files_under(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink())
    return found


def scan(files: list[Path], base: Path) -> list[str]:
    report = []
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192] and not path.name.endswith(".mo"):
            continue                       # binary (pictures); .mo files are text inside
        text = raw.decode("utf-8", "replace")
        try:
            shown = path.relative_to(base)
        except ValueError:
            shown = path
        for number, kind, value in scan_text(text):
            report.append(f"{shown}:{number}: {kind} ({value})")
    return report


def main(argv: list[str]) -> int:
    if argv:
        paths = [Path(a) for a in argv]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            print(f"privacy scan: not found: {', '.join(missing)}", file=sys.stderr)
            return 2
        files, base = files_under(paths), Path.cwd()
    else:
        files, base = tracked_files(ROOT), ROOT
    report = scan(files, base)
    for line in report:
        print(line)
    print(f"privacy scan: {len(files)} files, {len(report)} findings")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
