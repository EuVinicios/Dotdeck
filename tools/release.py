#!/usr/bin/env python3
"""Turn the "Não lançado" section of CHANGELOG.md into a version (the release workflow).

    python3 tools/release.py               # bump the version if there is something to release
    python3 tools/release.py --dry-run     # say what it would do, change nothing
    python3 tools/release.py --current     # print the version in src/config.py

A change reaches the users when its pull request adds a line to "## [Não lançado]" in
CHANGELOG.md. On every merge into main the Lançamento workflow (.github/workflows/
auto-release.yml) runs this script, which picks the next version and writes it everywhere
it lives:

- the version: the patch number goes up, or the minor one when the section has
  "### Adicionado" (something new, not only fixes);
- src/config.py (VERSION) and po/dotdeck.pot (Project-Id-Version);
- CHANGELOG.md: "## [Não lançado]" becomes "## [X.Y.Z] - YYYY-MM-DD";
- the metainfo: a <release> with the notes the stores show, in English. They come from
  the section's ``<!-- loja: … -->`` comments, one per line: the first is the summary and
  the others become the list. Without any, the summary says "Fixes and improvements."
  The comments leave the changelog, since GitHub's release notes are made from it.

Nothing to release (no section, or a section without entries) changes nothing. The last
line printed is ``version=X.Y.Z`` and ``changed=true|false``, for $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import datetime
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.euvinicios.Dotdeck"
UNRELEASED = "## [Não lançado]"
STORE_NOTE = re.compile(r"^[ \t]*<!--\s*loja:\s*(.*?)\s*-->[ \t]*\n?", re.M)
FALLBACK_SUMMARY = "Fixes and improvements."


class ReleaseError(Exception):
    pass


def paths(root: Path) -> dict[str, Path]:
    return {
        "changelog": root / "CHANGELOG.md",
        "config": root / "src" / "config.py",
        "pot": root / "po" / "dotdeck.pot",
        "metainfo": root / "data" / f"{APP_ID}.metainfo.xml",
    }


def current_version(root: Path = ROOT) -> str:
    match = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', paths(root)["config"].read_text("utf-8"), re.M)
    if not match:
        raise ReleaseError("src/config.py sem VERSION = \"X.Y.Z\"")
    return match.group(1)


def unreleased(changelog: str) -> tuple[int, int, str] | None:
    """Where the "Não lançado" section is (heading start, section end) and its body."""
    start = changelog.find(f"\n{UNRELEASED}")
    if start < 0:
        return None
    start += 1
    body_start = start + len(UNRELEASED)
    following = re.search(r"^## \[", changelog[body_start:], re.M)
    end = body_start + following.start() if following else len(changelog)
    return start, end, changelog[body_start:end]


def has_entries(body: str) -> bool:
    """A list item outside the store notes: headings alone are not a change."""
    return bool(re.search(r"^\s*[-*] \S", STORE_NOTE.sub("", body), re.M))


def next_version(version: str, body: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if re.search(r"^### Adicionado\s*$", body, re.M):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def metainfo_release(version: str, date: str, notes: list[str]) -> str:
    summary, items = (notes[0], notes[1:]) if notes else (FALLBACK_SUMMARY, [])
    lines = [f'    <release version="{version}" date="{date}">',
             "      <description>",
             f"        <p>{html.escape(summary, quote=False)}</p>"]
    if items:
        lines.append("        <ul>")
        lines += [f"          <li>{html.escape(item, quote=False)}</li>" for item in items]
        lines.append("        </ul>")
    lines += ["      </description>", "    </release>"]
    return "\n".join(lines) + "\n"


def _replace_once(text: str, pattern: str, replacement: str, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise ReleaseError(f"não achei {what}")
    return new


def release(root: Path = ROOT, today: datetime.date | None = None,
            dry_run: bool = False) -> tuple[str, bool]:
    """Write the next version if the changelog has something to release."""
    files = paths(root)
    changelog = files["changelog"].read_text("utf-8")
    version = current_version(root)
    section = unreleased(changelog)
    if not section or not has_entries(section[2]):
        return version, False
    start, end, body = section
    new = next_version(version, body)
    date = (today or datetime.date.today()).isoformat()
    metainfo = files["metainfo"].read_text("utf-8")
    if f'<release version="{new}"' in metainfo:
        raise ReleaseError(f"a metainfo já tem a versão {new}")
    if dry_run:
        return new, True

    notes = [note for note in STORE_NOTE.findall(body) if note]
    clean = re.sub(r"\n{3,}", "\n\n", STORE_NOTE.sub("", body))
    files["changelog"].write_text(
        changelog[:start] + f"## [{new}] - {date}" + clean + changelog[end:], "utf-8")
    files["config"].write_text(_replace_once(
        files["config"].read_text("utf-8"), r'^VERSION = "[^"]*"$', f'VERSION = "{new}"',
        "VERSION em src/config.py"), "utf-8")
    files["pot"].write_text(_replace_once(
        files["pot"].read_text("utf-8"), r'^"Project-Id-Version: Dotdeck [^\\"]*\\n"$',
        f'"Project-Id-Version: Dotdeck {new}\\\\n"', "Project-Id-Version em po/dotdeck.pot"),
        "utf-8")
    files["metainfo"].write_text(_replace_once(
        metainfo, r"^(\s*<releases>\n)", lambda m: m.group(1) + metainfo_release(new, date, notes),
        "<releases> na metainfo"), "utf-8")
    return new, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="não muda nada")
    parser.add_argument("--current", action="store_true", help="só mostra a versão atual")
    args = parser.parse_args(argv)
    try:
        if args.current:
            print(current_version())
            return 0
        version, changed = release(dry_run=args.dry_run)
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    if changed:
        print(f"{'Lançaria' if args.dry_run else 'Versão'} {version}")
    else:
        print(f"Nada a lançar: a versão continua {version}")
    print(f"version={version}")
    print(f"changed={'true' if changed and not args.dry_run else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
