"""Model names, providers, prices and keys as Dotdeck shows them (core/models.py, no GTK)."""

from __future__ import annotations

import copy
import locale
import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (isolated config, like every test module)
from tests.fake_server import MODEL_CONFIG, MODEL_CONFIG_032, FakeModelConfig

from src.core import models


class NumbersTest(unittest.TestCase):
    def setUp(self):
        numeric = locale.setlocale(locale.LC_NUMERIC)
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.addCleanup(locale.setlocale, locale.LC_NUMERIC, numeric)

    def test_money_keeps_two_significant_digits_below_a_cent(self):
        self.assertEqual(models.money(0.10), "0.10")
        self.assertEqual(models.money(1.28), "1.28")
        self.assertEqual(models.money(15), "15.00")
        self.assertEqual(models.money(0.0046), "0.0046")
        self.assertEqual(models.money(0.00128), "0.0013")
        self.assertEqual(models.money(0), "0.00")

    def test_price_lines(self):
        price = {"input": 0.10, "output": 0.50}
        self.assertEqual(models.price_text(price),
                         "US$ 0.10 input · US$ 0.50 output per million tokens")
        self.assertEqual(models.short_price(price), "US$ 0.10 / 0.50 per million tokens")
        for missing in (None, {}, {"input": 1}, {"input": "x", "output": 1},
                        {"input": -1, "output": 1}):
            self.assertEqual(models.price_text(missing), "")
            self.assertEqual(models.short_price(missing), "")

    def test_preset_detail(self):
        item = {"price": {"input": 0.10, "output": 0.50}, "speed": "fast", "note": "new"}
        self.assertEqual(models.preset_detail(item),
                         "US$ 0.10 / 0.50 per million tokens · fast · new")
        self.assertEqual(models.preset_detail({"speed": "deep"}), "deep thinking")
        self.assertEqual(models.preset_detail({"speed": "warp"}), "")

    def test_server_notes_are_shown_only_in_the_users_language(self):
        """The server writes notes in English: a translated Dotdeck shows the ones it knows
        in its language and leaves the others out instead of mixing languages."""
        item = {"speed": "fast", "note": "Direct with an Anthropic API key."}
        self.assertEqual(models.preset_detail(item), "fast · Direct with an Anthropic API key.")
        catalog = {"fast": "rápido",
                   "Direct with an Anthropic API key.": "Direto, com uma chave de API da "
                                                         "Anthropic."}
        with mock.patch.object(models, "_", lambda text: catalog.get(text, text)), \
                mock.patch.object(models, "translating", lambda: True):
            self.assertEqual(models.preset_detail(item),
                             "rápido · Direto, com uma chave de API da Anthropic.")
            self.assertEqual(models.preset_detail({"speed": "fast", "note": "Brand new."}),
                             "rápido")
        self.assertIn("Direct with an Anthropic API key.", models.SERVER_NOTES)
        self.assertEqual(models.server_text("  "), "")

    def test_seconds(self):
        self.assertEqual(models.seconds(1234), "1.2 s")
        self.assertEqual(models.seconds("bad"), "")

    def test_decimal_comma_in_portuguese(self):
        for name in ("pt_BR.UTF-8", "pt_BR.utf8"):
            try:
                locale.setlocale(locale.LC_NUMERIC, name)
                break
            except locale.Error:
                continue
        else:
            self.skipTest("no Portuguese locale on this machine")
        self.assertEqual(models.money(0.10), "0,10")
        self.assertEqual(models.seconds(1234), "1,2 s")


class NamesTest(unittest.TestCase):
    def test_derived_names(self):
        self.assertEqual(models.derive_name("anthropic/claude-haiku-5.5"), "Claude Haiku 5.5")
        self.assertEqual(models.derive_name("claude-haiku-5-5"), "Claude Haiku 5.5")
        self.assertEqual(models.derive_name("claude-opus-5-5"), "Claude Opus 5.5")
        self.assertEqual(models.derive_name("deepseek/deepseek-v4-flash-0731"),
                         "DeepSeek V4 Flash 0731")
        self.assertEqual(models.derive_name("openai/gpt-audio-mini"), "GPT Audio Mini")
        self.assertEqual(models.derive_name("anthropic/claude-haiku-5.5:batch"),
                         "Claude Haiku 5.5")
        self.assertEqual(models.derive_name(""), "")

    def test_display_name_prefers_the_server(self):
        self.assertEqual(models.display_name(MODEL_CONFIG), "Claude Haiku 5.5")
        # 0.3.2: the preset name without its hint in parentheses
        self.assertEqual(models.display_name(MODEL_CONFIG_032), "DeepSeek V4 Flash")
        self.assertEqual(models.display_name(MODEL_CONFIG_032, "anthropic/claude-sonnet-5.5"),
                         "Claude Sonnet 5.5")
        self.assertEqual(models.display_name(MODEL_CONFIG_032, "x-ai/grok-9"), "Grok 9")
        self.assertEqual(models.display_name({"model": None}), "")
        self.assertEqual(models.strip_hint("Name (fast, low cost)"), "Name")
        self.assertEqual(models.strip_hint("(only a hint)"), "(only a hint)")

    def test_at_a_glance(self):
        self.assertEqual(models.at_a_glance("anthropic/claude-haiku-5.5", "openrouter",
                                            MODEL_CONFIG), "Claude Haiku 5.5 · OpenRouter")
        # Without /api/model-config (or before it answers) the id still reads well.
        self.assertEqual(models.at_a_glance("claude-haiku-5-5", "anthropic"),
                         "Claude Haiku 5.5 · Anthropic")
        self.assertEqual(models.at_a_glance("claude-haiku-5-5", "claude"),
                         "Claude Haiku 5.5 · Anthropic")
        # A cached answer about another model does not lend it its name.
        self.assertEqual(models.at_a_glance("deepseek/deepseek-v4-flash-0731", "openrouter",
                                            MODEL_CONFIG), "DeepSeek V4 Flash · OpenRouter")
        self.assertEqual(models.at_a_glance("", "openrouter", MODEL_CONFIG), "")
        self.assertEqual(models.at_a_glance("m/x", ""), "X")

    def test_key_titles_are_one_message_per_provider(self):
        """Portuguese needs "do OpenRouter" but "da Anthropic": no "{provider} Key" slot."""
        self.assertEqual(models.key_title("openrouter"), "OpenRouter Key")
        self.assertEqual(models.key_title("anthropic"), "Anthropic Key")
        self.assertEqual(models.key_title("claude"), "Anthropic Key")
        self.assertEqual(models.key_title("openai-compatible"), "OpenAI-compatible Server Key")
        self.assertEqual(models.key_title(""), "API Key")
        self.assertEqual(models.key_title("mistral"), "mistral Key")
        self.assertTrue(set(models.CHOOSABLE) <= set(models.KEY_TITLES))


class ProvidersTest(unittest.TestCase):
    def test_provider_of(self):
        self.assertEqual(models.provider_of(MODEL_CONFIG), "openrouter")
        self.assertEqual(models.provider_of({"provider": "claude"}), "anthropic")
        self.assertEqual(models.provider_of({"baseUrl": "https://openrouter.ai/api/v1"}),
                         "openrouter")
        self.assertEqual(models.provider_of({"baseUrl": "https://api.anthropic.com/v1/"}),
                         "anthropic")
        self.assertEqual(models.provider_of({"baseUrl": "http://localhost:8080/v1"}),
                         "openai-compatible")
        self.assertEqual(models.provider_of({}), "")

    def test_new_servers_list_every_provider_with_its_key(self):
        self.assertTrue(models.modern(MODEL_CONFIG))
        listed = models.providers(MODEL_CONFIG)
        self.assertEqual([(p["id"], p["hasKey"]) for p in listed],
                         [("openrouter", True), ("anthropic", False),
                          ("openai-compatible", False)])
        self.assertEqual(listed[2]["baseUrl"], "")          # null on the server
        self.assertTrue(models.has_key(MODEL_CONFIG, "openrouter"))
        self.assertFalse(models.has_key(MODEL_CONFIG, "anthropic"))
        only_keys = {k: v for k, v in MODEL_CONFIG.items() if k != "providers"}
        self.assertEqual([p["id"] for p in models.providers(only_keys)], list(models.CHOOSABLE))

    def test_old_servers_have_one_key(self):
        self.assertFalse(models.modern(MODEL_CONFIG_032))
        self.assertEqual(models.providers(MODEL_CONFIG_032),
                         [{"id": "openrouter", "name": "OpenRouter", "hasKey": True,
                           "baseUrl": "https://openrouter.ai/api/v1"}])
        self.assertTrue(models.has_key(MODEL_CONFIG_032, "openrouter"))
        self.assertFalse(models.has_key(MODEL_CONFIG_032, "anthropic"))
        old_claude = dict(MODEL_CONFIG_032, provider="claude", hasApiKey=False)
        self.assertEqual(models.providers(old_claude)[0]["name"], "Anthropic")
        self.assertFalse(models.has_key(old_claude, "anthropic"))

    def test_presets_by_provider(self):
        self.assertEqual([p["id"] for p in models.presets(MODEL_CONFIG, "anthropic")],
                         ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
        self.assertEqual(len(models.presets(MODEL_CONFIG, "openrouter")), 3)
        self.assertEqual(models.presets(MODEL_CONFIG, "openai-compatible"), [])
        self.assertEqual(len(models.presets(MODEL_CONFIG_032, "openrouter")), 3)
        self.assertEqual(models.presets({"presets": [{"id": ""}, "x", {"name": "n"}]}), [])

    def test_call_models(self):
        call = models.call_models(MODEL_CONFIG)
        self.assertEqual(call, {"voice": "openai/gpt-audio-mini",
                                "transcribe": "google/gemini-2.5-flash-lite",
                                "thinking": "anthropic/claude-haiku-5.5", "gateway": True,
                                "reported": True})
        old = models.call_models(MODEL_CONFIG_032)
        self.assertEqual((old["voice"], old["transcribe"], old["thinking"], old["reported"]),
                         ("openai/gpt-audio-mini", "", "deepseek/deepseek-v4-flash-0731", False))
        direct = models.call_models({"voiceModel": "gpt-realtime", "model": "m/x"})
        self.assertFalse(direct["gateway"])

    def test_model_ids_per_provider(self):
        self.assertIsNone(models.model_problem("openrouter", "anthropic/claude-haiku-5.5"))
        self.assertIsNotNone(models.model_problem("openrouter", "claude-haiku-5-5"))
        self.assertIsNone(models.model_problem("anthropic", "claude-haiku-5-5"))
        self.assertIsNotNone(models.model_problem("anthropic", "anthropic/claude-haiku-5.5"))
        self.assertIsNone(models.model_problem("openai-compatible", "llama3"))
        self.assertIsNone(models.model_problem("", "anything-goes"))      # 0.3.2 servers
        self.assertIsNotNone(models.model_problem("openai-compatible", "two words"))
        self.assertIsNotNone(models.model_problem("openrouter", ""))
        self.assertIsNone(models.base_url_problem("http://192.168.0.10:8080/v1"))
        self.assertIsNotNone(models.base_url_problem("ftp://x"))
        self.assertIsNotNone(models.base_url_problem(""))


class FakeServerTest(unittest.TestCase):
    """The stand-in follows the contract both ways (it is what the UI tests trust)."""

    def test_new_server_switches_provider_and_keeps_keys_apart(self):
        fake = FakeModelConfig(MODEL_CONFIG)
        self.assertEqual(fake.post({"provider": "anthropic", "model": "anthropic/x"})[0], 400)
        self.assertEqual(fake.post({"provider": "bogus"})[0], 400)
        self.assertEqual(fake.post({"extra": "x"})[0], 400)
        status, answer = fake.post({"provider": "anthropic", "model": "claude-haiku-5-5",
                                    "apiKey": "sk-ant-test"})
        self.assertEqual((status, answer["provider"]), (200, "anthropic"))
        _status, data = fake.get()
        self.assertEqual(data["keys"], {"openrouter": True, "anthropic": True,
                                        "openai-compatible": False})
        self.assertEqual(data["baseUrl"], "https://api.anthropic.com/v1/")
        self.assertEqual(data["call"]["thinkingModel"], "claude-haiku-5-5")
        self.assertEqual(fake.test({}), (200, {"ok": True, "ms": 1234}))

    def test_old_server_ignores_what_it_does_not_know(self):
        fake = FakeModelConfig(MODEL_CONFIG_032)
        status, answer = fake.post({"provider": "anthropic", "model": "m/x"})
        self.assertEqual((status, answer["model"]), (200, "m/x"))
        self.assertEqual(fake.get()[1]["provider"], "openrouter")
        self.assertEqual(fake.test({})[0], 404)
        self.assertEqual(FakeModelConfig(None).get()[0], 404)
        self.assertEqual(MODEL_CONFIG_032, copy.deepcopy(MODEL_CONFIG_032))   # not mutated


if __name__ == "__main__":
    unittest.main()
