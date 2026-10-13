"""The OpenDots page in Dotdeck's language (src/web/translations, applied by focus.js).

No WebKit and no display: the catalogs and how webview.py picks one. tests/test_focus.py
checks the page itself.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CATALOGS = sorted((ROOT / "src" / "web" / "translations").glob("*.json"))
PLACEHOLDER = re.compile(r"\{\w+\}")


class CatalogTest(unittest.TestCase):
    def test_there_is_a_catalog_for_portuguese(self):
        self.assertIn("pt_BR.json", [path.name for path in CATALOGS])

    def test_every_catalog_is_well_formed(self):
        for path in CATALOGS:
            with self.subTest(path.name):
                data = json.loads(path.read_text("utf-8"))
                self.assertEqual(sorted(data), ["about", "strings"])
                strings = data["strings"]
                self.assertTrue(strings)
                for key, value in strings.items():
                    # focus.js compares the page's text with its spaces folded and trimmed.
                    self.assertEqual(key, " ".join(key.split()), key)
                    self.assertTrue(value.strip(), key)
                    self.assertNotEqual(key, value)
                    # Every part that varies comes back once, and nothing else is invented.
                    self.assertEqual(sorted(PLACEHOLDER.findall(key)),
                                     sorted(PLACEHOLDER.findall(value)), key)
                    self.assertEqual(len(set(PLACEHOLDER.findall(key))),
                                     len(PLACEHOLDER.findall(key)), key)
                    self.assertNotRegex(key, r"\{\w+\}\{\w+\}", "two parts in a row can't "
                                        "be told apart")

    def test_the_words_focus_js_reads_are_left_to_it(self):
        """focus.js finds these on the page in English; the page may show them translated."""
        strings = json.loads((ROOT / "src/web/translations/pt_BR.json").read_text("utf-8"))
        for word in ("Activity", "Memories", "Pages", "Scheduled & activity"):
            self.assertIn(word, strings["strings"])
        focus = (ROOT / "src/web/focus.js").read_text("utf-8")
        self.assertNotIn("textContent", focus.split("const text = ")[1].split("};")[0],
                         "text() must read what the page wrote, not what it shows")


class PickingTest(unittest.TestCase):
    def setUp(self):
        from src.web import webview
        self.webview = webview
        webview.page_translations.cache_clear()
        self.addCleanup(webview.page_translations.cache_clear)

    def test_dotdeck_s_language_picks_the_catalog(self):
        strings = self.webview.page_translations("pt_BR")
        self.assertEqual(strings["Memories"], "Memórias")
        self.assertEqual(strings["Message {name}…"], "Mensagem para {name}…")

    def test_english_and_unknown_languages_leave_the_page_as_it_is(self):
        self.assertEqual(self.webview.page_translations(None), {})
        self.assertEqual(self.webview.page_translations("en"), {})
        self.assertEqual(self.webview.page_translations("xx_YY"), {})

    def test_a_region_falls_back_to_the_language(self):
        with mock.patch.object(Path, "read_text", autospec=True,
                               side_effect=self._only("pt.json")) as read:
            strings = self.webview.page_translations("pt_PT")
        self.assertEqual(strings, {"Memories": "Memórias"})
        self.assertEqual([call.args[0].name for call in read.call_args_list],
                         ["pt_PT.json", "pt.json"])

    def test_a_broken_catalog_is_ignored(self):
        broken = mock.patch.object(Path, "read_text", autospec=True, return_value="{oops")
        with broken, self.assertLogs("src.web.webview", "WARNING"):
            self.assertEqual(self.webview.page_translations("pt_BR"), {})

    def test_the_language_comes_from_dotdeck_s_catalog(self):
        from src import i18n
        catalog = mock.Mock(spec=["info"])
        catalog.info.return_value = {"language": "pt_BR"}
        with mock.patch.object(i18n, "_translation", catalog), \
                mock.patch.object(i18n, "translating", return_value=True):
            self.assertEqual(i18n.language(), "pt_BR")
        with mock.patch.object(i18n, "translating", return_value=False):
            self.assertIsNone(i18n.language())

    @staticmethod
    def _only(name: str):
        def read(path, *_args, **_kwargs):
            if path.name == name:
                return json.dumps({"about": "", "strings": {"Memories": "Memórias", "x": 1}})
            raise FileNotFoundError(path)
        return read


if __name__ == "__main__":
    unittest.main()
