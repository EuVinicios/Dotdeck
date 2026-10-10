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
            # msgfmt leaves fuzzy translations out of the .mo: they count as untranslated.
            fuzzy = [block.split("\n")[-2:] for block in
                     (PO / f"{language}.po").read_text(encoding="utf-8").split("\n\n")
                     if re.search(r"^#,.*\bfuzzy\b", block, re.M)]
            self.assertEqual(fuzzy, [], f"fuzzy in {language}")

    def test_an_unset_key_has_its_own_message(self):
        """"Not set" is masculine in Portuguese ("Não definido"); a key says it plainly."""
        self.assertEqual(entries(PO / "pt_BR.po")["No key yet"], ["Nenhuma chave ainda"])
        self.assertEqual(entries(PO / "es.po")["No key yet"], ["Ninguna clave todavía"])
        model_page = (PO.parent / "src" / "ui" / "settings" / "model.py").read_text()
        self.assertNotIn('_("Not set")', model_page)

    def test_the_dot_menu_opens_with_a_verb(self):
        """"Abrir", not "Aberto": the badge of the open server has a message of its own."""
        self.assertEqual(entries(PO / "pt_BR.po")["Open"], ["Abrir"])
        self.assertEqual(entries(PO / "es.po")["Open"], ["Abrir"])
        servers = (PO.parent / "src" / "ui" / "settings" / "servers.py").read_text()
        self.assertNotIn('_("Open")', servers)
        self.assertEqual(entries(PO / "pt_BR.po")["Current"], ["Atual"])

    def test_each_provider_key_has_the_right_article(self):
        """"Chave do OpenRouter" but "Chave da Anthropic" (one message per provider)."""
        catalog = entries(PO / "pt_BR.po")
        self.assertEqual(catalog["OpenRouter Key"], ["Chave do OpenRouter"])
        self.assertEqual(catalog["Anthropic Key"], ["Chave da Anthropic"])
        self.assertEqual(catalog["OpenAI-compatible Server Key"],
                         ["Chave do servidor compatível com OpenAI"])
        for msgid in ("New {provider} Key (optional)", "Set a New {provider} Key",
                      "Type the {provider} key.", "{provider} Key"):
            # The name sits after "provedor", so no article has to agree with it.
            self.assertIn("provedor {provider}", catalog[msgid][0], msgid)


if __name__ == "__main__":
    unittest.main()
