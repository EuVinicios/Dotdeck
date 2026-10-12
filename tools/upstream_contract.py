#!/usr/bin/env python3
"""What Dotdeck needs from the OpenDots web client and server, checked against a checkout.

Focus mode and the page commands (src/web/focus.js, focus.css and the page scripts in
src/web/webview.py) depend on the OpenDots page's classes, ids, accessible labels and
visible words, and the API client on its /api routes. src/web/contract.json lists every one
of them; this tool looks for each in an OpenDots source tree:

    python3 tools/upstream_contract.py --opendots <OpenDots> [--summary FILE]
    python3 tools/upstream_contract.py --table          # the contract, as Markdown

It exits with 1 when a required token is missing (an optional one, which only some servers
have, gets a note). The check is textual: it shows the words are still in the source, not
that the page still works with Dotdeck; the smoke test on the real client
(tools/screenshots.py --smoke --demo <OpenDots>/dist/client) covers that.

tests/test_contract.py runs the same check when OPENDOTS_SRC points to a checkout, and also
keeps contract.json in sync with what focus.js, focus.css and webview.py really use.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "src" / "web" / "contract.json"
WEB = ROOT / "src" / "web"

CLIENT = ("src/client/**/*.ts", "src/client/**/*.tsx", "index.html")
SERVER = ("src/server/**/*.ts",)
FILES = {"route": SERVER, "server-text": SERVER, "bundle": ("dist/client/assets/*.js",)}
KINDS = ("class", "id", "label", "text", "attribute", "storage", "hash", "prop", "bundle",
         "route", "server-text", "dialog", "absent")
METHODS = ("get", "post", "put", "patch", "delete", "all")

# HTML elements that may appear in a selector (any other bare word is not a selector).
TAGS = frozenset("""a article aside body button div em footer form h1 h2 h3 h4 h5 h6 header
    html img input label li main meta nav ol option p section select span strong svg
    textarea ul""".split())

PLACEHOLDER = "${}"
_TEMPLATE = re.compile(r"\$\{[^}]*\}")


@dataclass
class Token:
    kind: str
    value: str
    used_by: str
    breaks: str
    optional: str = ""            # why only some servers have it (missing is then a note)
    pattern: str = ""             # a regular expression to look for instead of the default
    files: tuple[str, ...] = ()   # where to look instead of the default for the kind
    dotdeck: tuple[str, ...] = () # how Dotdeck's code writes it, when not as the value

    @property
    def name(self) -> str:
        return f"{self.kind} {self.value}"


@dataclass
class Result:
    token: Token
    status: str                   # ok, missing, optional, unchecked, appeared
    where: str = ""


@dataclass
class Report:
    opendots: str
    commit: str
    results: list[Result] = field(default_factory=list)

    def by(self, status: str) -> list[Result]:
        return [r for r in self.results if r.status == status]

    @property
    def broken(self) -> list[Result]:
        return self.by("missing") + self.by("appeared")


# -- the contract ------------------------------------------------------------------------

def load(path: Path = CONTRACT) -> tuple[list[Token], list[str]]:
    """The tokens, and the strings of Dotdeck's page code that are not the page's."""
    data = json.loads(path.read_text("utf-8"))
    tokens = []
    for item in data["tokens"]:
        unknown = set(item) - {"kind", "value", "used_by", "breaks", "optional", "pattern",
                               "files", "dotdeck"}
        if unknown or item.get("kind") not in KINDS:
            raise ValueError(f"contract.json: bad entry {item}")
        tokens.append(Token(item["kind"], item["value"], item["used_by"], item["breaks"],
                            item.get("optional", ""), item.get("pattern", ""),
                            tuple(item.get("files", ())), tuple(item.get("dotdeck", ()))))
    return tokens, list(data.get("not_upstream", []))


def _value_regex(value: str) -> str:
    """The value as a regular expression; ${…} matches any template placeholder."""
    parts = _TEMPLATE.split(value)
    escaped = [re.escape(part).replace("\\&", "&").replace("&", "(?:&|&amp;)")
               for part in parts]
    return r"\$\{[^}]*\}".join(escaped)


def pattern_of(token: Token) -> str:
    if token.pattern:
        return token.pattern
    value = _value_regex(token.value)
    quote = r"['\"`]"
    kind = token.kind
    if kind in ("class", "id"):
        return rf"(?<![\w$.-]){value}(?![\w-])"
    if kind == "label":    # an attribute value, or a whole string (a constant, a dictionary)
        return rf"aria-label=\{{?\s*{quote}{value}{quote}|{quote}{value}{quote}"
    if kind == "text":     # a whole string, or the whole text of an element
        return rf"{quote}{value}{quote}|>\s*{value}\s*<"
    if kind in ("storage", "absent"):
        return rf"{quote}{value}{quote}"
    if kind == "attribute":
        name, _, attr_value = token.value.partition("=")
        if not attr_value:
            return rf"{re.escape(name)}="
        return rf"{re.escape(name)}=\{{?\s*{quote}{re.escape(attr_value.strip(chr(34)))}{quote}"
    if kind == "prop":
        return rf"\b{value}\??\s*:"
    if kind == "route":
        method, _, path = token.value.partition(" ")
        relative = re.escape(path.removeprefix("/api")).replace(r"\:", ":")
        relative = re.sub(r":\w+", r":\\w+", relative)
        verbs = "|".join(METHODS) if method.lower() == "all" else method.lower()
        return rf"\.(?:{verbs})\(\s*{quote}(?:/api)?{relative}{quote}"
    if kind == "dialog":   # the expression webview.py matches also matches the source
        return token.value
    return value           # hash, bundle, server-text: the text anywhere


def files_of(token: Token) -> tuple[str, ...]:
    return token.files or FILES.get(token.kind, CLIENT)


# -- checking an OpenDots tree -----------------------------------------------------------

def _read(opendots: Path, globs: tuple[str, ...], cache: dict) -> list[tuple[str, str]]:
    found = []
    for glob in globs:
        if glob not in cache:
            cache[glob] = [(str(path.relative_to(opendots)), path.read_text("utf-8", "replace"))
                           for path in sorted(opendots.glob(glob)) if path.is_file()]
        found += cache[glob]
    return found


def _commit(opendots: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(opendots), "rev-parse", "--short=12", "HEAD"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def check(opendots: Path, tokens: list[Token] | None = None) -> Report:
    tokens = tokens if tokens is not None else load()[0]
    report = Report(str(opendots), _commit(opendots))
    cache: dict = {}
    for token in tokens:
        files = _read(opendots, files_of(token), cache)
        if not files and token.kind == "bundle":    # not built: nothing to look at
            report.results.append(Result(token, "unchecked"))
            continue
        regex = re.compile(pattern_of(token))
        where = next((name for name, text in files if regex.search(text)), "")
        if token.kind == "absent":
            report.results.append(Result(token, "appeared" if where else "ok", where))
        elif where:
            report.results.append(Result(token, "ok", where))
        else:
            report.results.append(Result(token, "optional" if token.optional else "missing"))
    return report


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(report: Report) -> str:
    """The report for people (a GitHub job summary, the terminal)."""
    required = len(report.results) - len(report.by("optional")) - len(report.by("unchecked"))
    lines = [f"### Dotdeck's contract with OpenDots ({report.commit or 'unknown commit'})", ""]
    broken = report.broken
    if not broken:
        lines.append(f"All {required} required tokens are in this OpenDots "
                     f"({len(report.results)} in the contract).")
    else:
        lines += [f"**{len(broken)} of {required} required tokens broke.** Dotdeck uses these "
                  "names of the OpenDots page or API and they are gone (or something it does "
                  "not expect appeared):", "",
                  "| Kind | Token | Used by | What breaks |", "|---|---|---|---|"]
        for result in broken:
            token = result.token
            item = f"`{_cell(token.value)}`" + (f" (appeared in `{result.where}`)"
                                                 if result.status == "appeared" else "")
            lines.append(f"| {token.kind} | {item} | {_cell(token.used_by)} | "
                         f"{_cell(token.breaks)} |")
    for status, title in (("optional", "Optional and missing (only some servers have them)"),
                          ("unchecked", "Not checked (build the client first)")):
        if report.by(status):
            lines += ["", f"{title}: " + ", ".join(f"`{r.token.value}`"
                                                    for r in report.by(status))]
    return "\n".join(lines) + "\n"


def table(tokens: list[Token]) -> str:
    """The contract as a Markdown table."""
    lines = ["| Kind | Token | Used by | What breaks |", "|---|---|---|---|"]
    for token in tokens:
        note = f" (optional: {token.optional})" if token.optional else ""
        lines.append(f"| {token.kind} | `{_cell(token.value)}` | {_cell(token.used_by)} | "
                     f"{_cell(token.breaks)}{_cell(note)} |")
    return "\n".join(lines) + "\n"


# -- what Dotdeck's code uses (for tests/test_contract.py) -------------------------------

def js_strings(source: str) -> list[str]:
    """The string and template literals of a script (placeholders become ``${}``).

    Comments and regular expression literals are skipped. Good enough for Dotdeck's own
    scripts, which is all it is used for.
    """
    found: list[str] = []
    i, n, previous = 0, len(source), ""

    def read_string(start: int, quote: str) -> tuple[int, str]:
        j, text = start + 1, []
        while j < n:
            char = source[j]
            if char == "\\":
                text.append(source[j:j + 2])
                j += 2
                continue
            if quote == "`" and source.startswith("${", j):
                depth, k = 1, j + 2
                while k < n and depth:
                    if source[k] in "'\"`":       # a string inside the placeholder
                        k, inner = read_string(k, source[k])
                        found.append(inner)
                        continue
                    depth += {"{": 1, "}": -1}.get(source[k], 0)
                    k += 1
                text.append(PLACEHOLDER)
                j = k
                continue
            if char == quote:
                return j + 1, "".join(text)
            text.append(char)
            j += 1
        return j, "".join(text)

    while i < n:
        char = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if char in "'\"`":
            i, text = read_string(i, char)
            found.append(text)
            previous = "x"
            continue
        if char == "/" and (previous == "" or previous in "(,=:[!&|?{};+"):
            j, in_class = i + 1, False                    # a regular expression literal
            while j < n and source[j] != "\n":
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                elif source[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            previous = "x"
            continue
        if not char.isspace():
            previous = char
        i += 1
    return found


_COMPOUND = re.compile(r"(?P<tag>[a-z][a-z0-9]*|\*)?(?P<rest>(?:[.#][A-Za-z_][\w-]*"
                       r"|\[[^\]]+\]|::?[\w-]+(?:\((?:[^()]|\([^()]*\))*\))?)*)")
_ATTRIBUTE = re.compile(r"\[\s*([\w-]+)\s*(?:[~|^$*]?=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\]\s]*)))?"
                        r"\s*\]")


def _split_selector(text: str) -> list[str] | None:
    """Compound selectors of a selector list, or None when this is not a selector."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if depth == 0 and (char in ",>+~" or char.isspace()):
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts if depth == 0 else None


def selector_atoms(text: str) -> set[tuple[str, str]] | None:
    """Classes, ids and attributes in a CSS selector; None if ``text`` is not a selector."""
    text = text.replace(PLACEHOLDER, " ").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", text):      # a colour
        return None
    parts = _split_selector(text)
    if not text or not parts:
        return None
    atoms: set[tuple[str, str]] = set()
    featured = False
    for part in parts:
        match = _COMPOUND.fullmatch(part)
        if not match or not part:
            return None
        tag, rest = match.group("tag"), match.group("rest")
        if tag and tag != "*" and tag not in TAGS:
            return None
        featured = featured or bool(rest) or bool(tag)
        for pseudo in re.finditer(r"::?[\w-]+\(((?:[^()]|\([^()]*\))*)\)", rest):
            inner = selector_atoms(pseudo.group(1))
            if inner:
                atoms |= inner
        plain = re.sub(r"::?[\w-]+\((?:[^()]|\([^()]*\))*\)", "", rest)
        for attribute in _ATTRIBUTE.finditer(plain):
            name = attribute.group(1)
            value = next((g for g in attribute.groups()[1:] if g is not None), None)
            if name.startswith("data-dotdeck-"):
                continue                                  # Dotdeck's own marks
            if name == "aria-label" and value is not None:
                atoms.add(("aria", value))
            else:
                atoms.add(("attr", f'{name}="{value}"' if value is not None else name))
        plain = _ATTRIBUTE.sub("", plain)
        atoms |= {("class", c) for c in re.findall(r"\.([A-Za-z_][\w-]*)", plain)}
        atoms |= {("id", i) for i in re.findall(r"#([A-Za-z_][\w-]*)", plain)}
    return atoms if featured else None


def _route(path: str) -> str:
    return re.sub(r":\w+|\$\{\}", ":param", path.split("?", 1)[0])


def _literal_usage(literal: str, where: str, usage: dict) -> None:
    if literal.startswith("/api/"):
        usage.setdefault(("route", _route(literal)), set()).add(where)
        return
    atoms = selector_atoms(literal)
    if atoms is None:
        usage.setdefault(("literal", literal), set()).add(where)
    for atom in atoms or ():
        usage.setdefault(atom, set()).add(where)


def css_usage(css: str, where: str, usage: dict) -> None:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for prelude in re.findall(r"([^{}]+)\{[^{}]*\}", css):
        for selector in prelude.split(","):
            atoms = selector_atoms(selector.strip())
            for atom in atoms or ():
                usage.setdefault(atom, set()).add(where)


def _python_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else PLACEHOLDER
                       for part in node.values)
    return None


def python_usage(source: str, where: str, usage: dict, page_scripts: bool) -> None:
    """Routes the API client calls; with ``page_scripts``, also the scripts and selectors
    it runs on the page and the page's dialogs it recognizes (webview.py)."""
    tree = ast.parse(source)
    inside_fstring = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                      for part in node.values}
    for node in ast.walk(tree):
        if page_scripts and isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_Known":
            pattern = _python_string(node.args[0]) if node.args else None
            if pattern is not None:
                usage.setdefault(("dialog", pattern), set()).add(where)
        if id(node) in inside_fstring:
            continue
        text = _python_string(node)
        if text is None:
            continue
        if text.startswith("/api/"):
            usage.setdefault(("route", _route(text)), set()).add(where)
        elif not page_scripts:
            continue
        elif "querySelector" in text or "document." in text:
            for literal in js_strings(text):
                _literal_usage(literal, where, usage)
        elif "{" in text and re.search(r"\[data-dotdeck-[\w-]+", text):   # a style sheet
            css_usage(text.replace(PLACEHOLDER, ""), where, usage)
        elif re.search(r"\[(?:aria-label|role|data-dotdeck-[\w-]+)[=\]]", text):
            atoms = selector_atoms(text)
            if atoms is None:
                usage.setdefault(("unparsed", text), set()).add(where)
            for atom in atoms or ():
                usage.setdefault(atom, set()).add(where)


def dotdeck_usage(root: Path = ROOT) -> dict[tuple[str, str], set[str]]:
    """Everything of the OpenDots page and API that Dotdeck's code refers to."""
    usage: dict[tuple[str, str], set[str]] = {}
    web = root / "src" / "web"
    focus = (web / "focus.js").read_text("utf-8")
    for literal in js_strings(focus):
        _literal_usage(literal, "focus.js", usage)
    for prop in re.findall(r"\bprops\.(\w+)", focus):     # React props read from fibers
        usage.setdefault(("prop", prop), set()).add("focus.js")
    css_usage((web / "focus.css").read_text("utf-8"), "focus.css", usage)
    python_usage((web / "webview.py").read_text("utf-8"), "webview.py", usage, True)
    for path in sorted((root / "src").rglob("*.py")):
        if path.name != "webview.py" or path.parent != web:
            python_usage(path.read_text("utf-8"), str(path.relative_to(root)), usage, False)
    # The token's sessionStorage key reaches bridge.js and focus.js through their config.
    match = re.search(r'^TOKEN_STORAGE_KEY = "([^"]+)"', (root / "src" / "config.py")
                      .read_text("utf-8"), re.M)
    if match:
        usage.setdefault(("literal", match.group(1)), set()).add("config.py")
    return usage


def keys_of(token: Token) -> set[tuple[str, str]]:
    """How a token shows up in dotdeck_usage()."""
    value = _TEMPLATE.sub(PLACEHOLDER, token.value)
    keys = {("literal", _TEMPLATE.sub(PLACEHOLDER, alias)) for alias in token.dotdeck}
    if token.kind in ("class", "id"):
        keys |= {(token.kind, value), ("literal", value)}
    elif token.kind == "label":
        keys |= {("aria", value), ("literal", value)}
    elif token.kind == "attribute":
        keys |= {("attr", value), ("literal", value)}
    elif token.kind == "route":
        keys.add(("route", _route(token.value.partition(" ")[2])))
    elif token.kind == "dialog":
        keys.add(("dialog", token.value))
    elif token.kind == "prop":
        keys.add(("prop", token.value))
    elif token.kind in ("text", "storage", "hash", "bundle", "server-text") and not token.dotdeck:
        keys.add(("literal", value))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--opendots", type=Path, help="an OpenDots checkout (built for "
                        "the bundle checks: npm ci && npm run build)")
    parser.add_argument("--summary", type=Path, help="append the report, in Markdown, here "
                        "(e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--table", action="store_true", help="print the contract as a "
                        "Markdown table and exit")
    args = parser.parse_args()
    tokens, _ = load()
    if args.table:
        sys.stdout.write(table(tokens))
        return 0
    if not args.opendots:
        parser.error("--opendots is required (or --table)")
    report = check(args.opendots, tokens)
    text = markdown(report)
    sys.stdout.write(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(text)
    return 1 if report.broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
