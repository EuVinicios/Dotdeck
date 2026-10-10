"""tools/privacy_scan.py: the CI guard that keeps keys and private addresses out of Dotdeck.

The secret-shaped values below are built at run time, so this file never holds one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import privacy_scan  # noqa: E402


def kinds(text: str) -> list[str]:
    return [kind for _line, kind, _value in privacy_scan.scan_text(text)]


class PrivacyScanTest(unittest.TestCase):
    def test_finds_every_kind(self):
        samples = {
            "OpenRouter key": "key = " + "sk-or-" + "v1-" + "0a1b2c3d" * 8,
            "Anthropic key": "ANTHROPIC_API_KEY=" + "sk-ant-" + "api03-" + "Zx9_" * 10,
            "OpenAI key": "sk-" + "proj-" + "Q" * 40,
            "Telegram bot token": "TOKEN=" + "7" * 10 + ":AA" + "h" * 33,
            "home folder": "cd " + "/home/" + "alice" + "/projects",
            "private address": "http://" + "192.168." + "7.42:4310",
            "Tailscale name": "https://box." + "tail" + "9f8e7d.ts.net",
            "Tailscale address": "http://[" + "fd7a:" + "115c:a1e0:ab12::1]:4310",
        }
        for kind, text in samples.items():
            with self.subTest(kind=kind):
                self.assertEqual(kinds(text), [kind])

    def test_tailscale_and_other_private_ranges(self):
        for address in ("10." + "1.2.3", "172." + "20.0.5", "100." + "70.1.2"):
            self.assertEqual(kinds(f"host {address} here"), ["private address"], address)

    def test_the_end_of_a_sentence_hides_nothing(self):
        """An address that ends a sentence in a guide: the full stop is not part of it."""
        self.assertEqual(kinds("Acesse " + "100." + "70.1.2."), ["private address"])
        self.assertEqual(kinds("veja " + "/home/" + "joao."), ["home folder"])
        self.assertEqual(kinds("release 1.2.3.4.5."), [])         # a longer version number
        self.assertEqual(kinds("em /home/user."), [])

    def test_documented_examples_and_fakes_pass(self):
        for text in ("http://localhost:4310", "http://127.0.0.1:4310",
                     "http://192.168.0.10:4310 or https://homelab.tail1234.ts.net",
                     "network 100.64.0.0/10", "release 1.2.3.4", "GTK 4.0",
                     "network " + "fd7a:" + "115c:a1e0::/48",
                     "sk-or-test-key", "sk-or-v1-not-a-real-key-0123456789abcdef",
                     "sk-ant-test-key", "/home/user/.config", "/home/runner/work",
                     "public 8.8.8.8", "http://" + "192.168.9.9" + "  # privacy: example"):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_values_are_masked_in_the_report(self):
        secret = "sk-or-" + "v1-" + "5" * 64
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "leak.txt").write_text(f"OPENAI_API_KEY={secret}\n")
            (Path(folder) / "icon.png").write_bytes(b"\x89PNG\0\0" + secret.encode())
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "privacy_scan.py"),
                                     folder], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("leak.txt:1: OpenRouter key (sk-or-…)", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("icon.png", result.stdout)            # binaries are skipped

    def test_the_source_tree_is_clean(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "privacy_scan.py")],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
