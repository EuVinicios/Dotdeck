"""tools/release.py: the "Não lançado" section of the changelog becomes a version."""

from __future__ import annotations

import datetime
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import release  # noqa: E402

TODAY = datetime.date(2026, 10, 20)
HEAD = """# Changelog

Todas as mudanças relevantes do Dotdeck.

"""
OLD = """## [0.3.8] - 2026-10-12

### Corrigido

- Uma correção antiga.
"""


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, path in release.paths(ROOT).items():
            target = release.paths(self.root)[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target)
        self.version = release.current_version(self.root)
        self.major, self.minor, self.patch = (int(n) for n in self.version.split("."))

    def write_changelog(self, unreleased: str | None) -> None:
        text = HEAD + (f"## [Não lançado]\n{unreleased}\n" if unreleased is not None else "") + OLD
        release.paths(self.root)["changelog"].write_text(text, "utf-8")

    def read(self, name: str) -> str:
        return release.paths(self.root)[name].read_text("utf-8")

    def test_nothing_to_release_changes_nothing(self):
        for unreleased in (None, "", "\n### Corrigido\n\n<!-- loja: Only a note. -->\n"):
            with self.subTest(unreleased=unreleased):
                self.write_changelog(unreleased)
                before = {name: self.read(name) for name in release.paths(self.root)}
                self.assertEqual(release.release(self.root, TODAY), (self.version, False))
                self.assertEqual({name: self.read(name) for name in before}, before)

    def test_a_fix_bumps_the_patch_number_everywhere(self):
        self.write_changelog("""
### Corrigido

- **A caixa de mensagem cresce com o texto.**
  Continua na linha de baixo.
<!-- loja: A message box that grows with what you type. -->
<!-- loja: It goes back to one line once the message is sent & stays tidy -->
""")
        new = f"{self.major}.{self.minor}.{self.patch + 1}"
        self.assertEqual(release.release(self.root, TODAY), (new, True))
        self.assertEqual(release.current_version(self.root), new)
        changelog = self.read("changelog")
        self.assertIn(f"## [{new}] - 2026-10-20\n\n### Corrigido\n\n- **A caixa", changelog)
        self.assertNotIn("Não lançado", changelog)
        self.assertNotIn("loja:", changelog)
        self.assertIn("Continua na linha de baixo.\n\n## [0.3.8]", changelog)
        self.assertIn(f'"Project-Id-Version: Dotdeck {new}\\n"', self.read("pot"))
        releases = ET.fromstring(self.read("metainfo")).find("releases")
        first = releases[0]
        self.assertEqual((first.get("version"), first.get("date")), (new, "2026-10-20"))
        self.assertEqual(first.find("description/p").text,
                         "A message box that grows with what you type.")
        self.assertEqual([li.text for li in first.findall("description/ul/li")],
                         ["It goes back to one line once the message is sent & stays tidy"])
        self.assertEqual(releases[1].get("version"), self.version)

    def test_something_new_bumps_the_minor_number(self):
        self.write_changelog("\n### Adicionado\n\n- Uma tela nova.\n\n### Corrigido\n\n- Algo.\n")
        new = f"{self.major}.{self.minor + 1}.0"
        self.assertEqual(release.release(self.root, TODAY), (new, True))
        first = ET.fromstring(self.read("metainfo")).find("releases")[0]
        self.assertEqual(first.find("description/p").text, release.FALLBACK_SUMMARY)
        self.assertIsNone(first.find("description/ul"))

    def test_dry_run_changes_nothing(self):
        self.write_changelog("\n### Corrigido\n\n- Algo.\n")
        before = self.read("changelog")
        new = f"{self.major}.{self.minor}.{self.patch + 1}"
        self.assertEqual(release.release(self.root, TODAY, dry_run=True), (new, True))
        self.assertEqual(self.read("changelog"), before)
        self.assertEqual(release.current_version(self.root), self.version)

    def test_a_version_already_in_the_metainfo_is_an_error(self):
        self.write_changelog("\n### Corrigido\n\n- Algo.\n")
        release.release(self.root, TODAY)
        config = release.paths(self.root)["config"]
        config.write_text(config.read_text("utf-8").replace(
            f'VERSION = "{self.major}.{self.minor}.{self.patch + 1}"',
            f'VERSION = "{self.version}"'), "utf-8")
        self.write_changelog("\n### Corrigido\n\n- Outra coisa.\n")
        with self.assertRaises(release.ReleaseError):
            release.release(self.root, TODAY)


if __name__ == "__main__":
    unittest.main()
