"""Dotdeck's contract with the OpenDots page and API (src/web/contract.json).

ContractSyncTest keeps the list in sync with Dotdeck's code: every class, id, label, word and
route that focus.js, focus.css, webview.py and the API client use is listed, and every listed
one is still used. UpstreamContractTest checks the list against an OpenDots checkout given in
OPENDOTS_SRC and is skipped without it; .github/workflows/upstream.yml runs it on pull requests
against the official main branch (built, for the checks on the bundle), and the maintainer's
server runs it every night.

No WebKit and no display: this is plain text processing.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import upstream_contract as contract  # noqa: E402


class ContractSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens, cls.not_upstream = contract.load()
        cls.usage = contract.dotdeck_usage()

    def test_the_contract_is_well_formed(self):
        names = [token.name for token in self.tokens]
        self.assertEqual(sorted({n for n in names if names.count(n) > 1}), [])
        for token in self.tokens:
            self.assertTrue(token.used_by and token.breaks, token.name)
            re.compile(contract.pattern_of(token))
        self.assertEqual(len(self.not_upstream), len(set(self.not_upstream)))
        listed = set().union(*(contract.keys_of(token) for token in self.tokens))
        self.assertEqual([s for s in self.not_upstream if ("literal", s) in listed], [])

    def test_everything_dotdeck_uses_is_in_the_contract(self):
        known = set().union(*(contract.keys_of(token) for token in self.tokens))
        unlisted = [f"{kind} {value!r} in {', '.join(sorted(where))}"
                    for (kind, value), where in sorted(self.usage.items())
                    if (kind, value) not in known
                    and not (kind == "literal" and value in self.not_upstream)]
        self.assertEqual(unlisted, [], "add what comes from the OpenDots page or API to "
                         "src/web/contract.json, and Dotdeck's own strings to its "
                         "not_upstream list")

    def test_every_token_is_still_used(self):
        stale = [token.name for token in self.tokens
                 if token.kind != "absent" and not contract.keys_of(token) & self.usage.keys()]
        self.assertEqual(stale, [], "remove these from src/web/contract.json")
        unused = [s for s in self.not_upstream if ("literal", s) not in self.usage]
        self.assertEqual(unused, [], "remove these from not_upstream in src/web/contract.json")

    def test_new_selectors_labels_and_words_are_noticed(self):
        """A guard on the guard: what a change to focus.js or webview.py could add."""
        usage: dict = {}
        script = """
          // 'a comment' is not a string
          const a = $$(`${DOTS} .template-app button[aria-label="Archive chat"]`);
          if (text(label) === 'Archived' && /it's a regex/.test(b)) {
            open(`#/x/${id ? `/y/${id}` : ''}`);
          }
          const c = { 'Inbox': 'inbox' }, d = '#c0ffee';
        """
        for literal in contract.js_strings(script):
            contract._literal_usage(literal, "test", usage)
        self.assertIn(("aria", "Archive chat"), usage)
        self.assertIn(("class", "template-app"), usage)
        self.assertIn(("literal", "Archived"), usage)
        self.assertIn(("literal", "Inbox"), usage)
        self.assertIn(("literal", "#c0ffee"), usage)            # a colour, not an id
        self.assertIn(("literal", "/y/${}"), usage)
        self.assertNotIn(("literal", "a comment"), usage)
        self.assertNotIn(("literal", "s a regex/.test(b)) open("), usage)

        python = ('X = """document.querySelector(\'button[aria-label="Pin"]\')"""\n'
                  'Y = {"pin": \'.chat-persona-actions button[aria-label="Pin chat"]\'}\n'
                  'Z = f"/api/things/{thing_id}?full=1"\n'
                  '_Known(r"Pin this\\\\?", "Pin?", "", "", None)\n'
                  'W = "focus.js"\n')
        contract.python_usage(python, "test.py", usage, page_scripts=True)
        self.assertIn(("aria", "Pin"), usage)
        self.assertIn(("aria", "Pin chat"), usage)
        self.assertIn(("class", "chat-persona-actions"), usage)
        self.assertIn(("route", "/api/things/:param"), usage)
        self.assertIn(("dialog", "Pin this\\\\?"), usage)
        self.assertNotIn(("class", "js"), usage)

        css: dict = {}
        contract.css_usage("/* .old */ @media (min-width: 9px) { html[data-dotdeck-focus] "
                           ".a > .b:has(> .c), p:not(.d)::before { color: red; } }", "t.css",
                           css)
        self.assertEqual(set(css), {("class", c) for c in "abcd"})

    def test_matching_against_an_opendots_tree(self):
        tree = Path(tempfile.mkdtemp(prefix="dotdeck-contract-"))
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "src" / "client").mkdir(parents=True)
        (tree / "src" / "server").mkdir(parents=True)
        (tree / "src" / "client" / "App.tsx").write_text("""
          <div className={`app template-app ${collapsed ? 'nav-collapsed' : ''}`}>
            <nav className="dots-nav" aria-label="Dots">
              <button aria-label={`Edit ${item.name} settings`} />
              <span>Scheduled & activity</span>
          const pages = view === 'space' ? 'Pages' : 'Memories';
        """)
        (tree / "src" / "server" / "routes.ts").write_text("""
          app.get('/api/state', state);
          app.route('/api', workspace);   // workspace.ts: app.put('/dots/:id', ...)
          app.put('/dots/:id', save);
        """)
        tokens = [contract.Token(kind, value, "test", "test", **extra) for kind, value, extra in (
            ("class", "template-app", {}), ("class", "nav-collapsed", {}),
            ("class", "dot-nav", {}),                       # only "dots-nav" is there
            ("label", "Dots", {}), ("label", "Edit ${name} settings", {}),
            ("label", "Agents", {}),
            ("text", "Scheduled & activity", {}), ("text", "Pages", {}),
            ("text", "Activity", {}),
            ("route", "GET /api/state", {}), ("route", "PUT /api/dots/:id", {}),
            ("route", "POST /api/dots", {}),
            ("route", "GET /api/model-config", {"optional": "some servers"}),
            ("bundle", "__reactFiber$", {}),
            ("absent", "opendots-language", {}))]
        report = contract.check(tree, tokens)
        status = {r.token.name: r.status for r in report.results}
        self.assertEqual(status, {
            "class template-app": "ok", "class nav-collapsed": "ok", "class dot-nav": "missing",
            "label Dots": "ok", "label Edit ${name} settings": "ok", "label Agents": "missing",
            "text Scheduled & activity": "ok", "text Pages": "ok", "text Activity": "missing",
            "route GET /api/state": "ok", "route PUT /api/dots/:id": "ok",
            "route POST /api/dots": "missing", "route GET /api/model-config": "optional",
            "bundle __reactFiber$": "unchecked", "absent opendots-language": "ok"})

        (tree / "src" / "client" / "i18n.ts").write_text("const key = 'opendots-language';\n")
        report = contract.check(tree, tokens)
        self.assertIn("absent opendots-language", [r.token.name for r in report.broken])
        text = contract.markdown(report)
        self.assertIn("| absent | `opendots-language` (appeared in `src/client/i18n.ts`)", text)
        self.assertIn("| label | `Agents` |", text)
        self.assertIn("Optional and missing (only some servers have them): "
                      "`GET /api/model-config`", text)


@unittest.skipUnless(os.environ.get("OPENDOTS_SRC"),
                     "set OPENDOTS_SRC to an OpenDots checkout (built: npm ci && npm run build)")
class UpstreamContractTest(unittest.TestCase):
    def test_opendots_has_everything_dotdeck_uses(self):
        source = Path(os.environ["OPENDOTS_SRC"]).expanduser()
        self.assertTrue((source / "src" / "client").is_dir(), f"not an OpenDots tree: {source}")
        report = contract.check(source)
        text = contract.markdown(report)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(text)
        self.assertEqual([result.token.name for result in report.broken], [], "\n" + text)


if __name__ == "__main__":
    unittest.main()
