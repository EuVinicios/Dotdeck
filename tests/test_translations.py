"""The translations: every message of po/dotdeck.pot is translated in pt_BR and es."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PO = Path(__file__).resolve().parent.parent / "po"


def entries(path: Path) -> dict[str, list[str]]:
    """msgid → its msgstr values (one, or one per plural form)."""
    found: dict[str, list[str]] = {}
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        if block.lstrip().startswith("#~"):
            continue                       # obsolete
        msgid = _joined(block, "msgid")
        if not msgid:
            continue                       # the header
        values = [_joined(block, key) for key in ("msgstr", "msgstr[0]", "msgstr[1]")]
        found[msgid] = [v for v in values if v is not None]
    return found


def _joined(block: str, key: str) -> str | None:
    match = re.search(rf'^{re.escape(key)} ((?:"(?:[^"\\]|\\.)*"\n?)+)', block, re.M)
    if match is None:
        return None
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1)))


class TranslationTest(unittest.TestCase):
    def test_every_message_is_translated(self):
        template = entries(PO / "dotdeck.pot")
        self.assertTrue(template)
        for language in ("pt_BR", "es"):
            catalog = entries(PO / f"{language}.po")
            missing = [m for m in template if not catalog.get(m) or not all(catalog[m])]
            self.assertEqual(missing, [], f"untranslated in {language}")

    def test_an_unset_key_has_its_own_message(self):
        """"Not set" is masculine in Portuguese ("Não definido"); a key says it plainly."""
        self.assertEqual(entries(PO / "pt_BR.po")["No key yet"], ["Nenhuma chave ainda"])
        self.assertEqual(entries(PO / "es.po")["No key yet"], ["Ninguna clave todavía"])
        model_page = (PO.parent / "src" / "ui" / "settings" / "model.py").read_text()
        self.assertNotIn('_("Not set")', model_page)


if __name__ == "__main__":
    unittest.main()
