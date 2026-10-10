from __future__ import annotations

import unittest

from tests.helpers import run_until

from src.core import server  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402


class NormalizeUrlTest(unittest.TestCase):
    def test_local_addresses_default_to_http(self):
        self.assertEqual(server.normalize_url("localhost:4310"), "http://localhost:4310")
        self.assertEqual(server.normalize_url("127.0.0.1:4310/"), "http://127.0.0.1:4310")
        self.assertEqual(server.normalize_url("192.168.1.20:4310"),  # privacy: example
                         "http://192.168.1.20:4310")  # privacy: example
        self.assertEqual(server.normalize_url("100.101.102.103:4310"),  # privacy: example
                         "http://100.101.102.103:4310")  # privacy: example
        self.assertEqual(server.normalize_url("homelab:4310"), "http://homelab:4310")
        self.assertEqual(server.normalize_url("[::1]:4310"), "http://[::1]:4310")

    def test_public_names_default_to_https(self):
        self.assertEqual(server.normalize_url("dots.example.com"), "https://dots.example.com")
        self.assertEqual(server.normalize_url("meu-homeserver.ts.net:4310"),
                         "https://meu-homeserver.ts.net:4310")

    def test_explicit_scheme_path_and_default_port(self):
        self.assertEqual(server.normalize_url(" HTTPS://Dots.Example.com:443/app?x=1 "),
                         "https://dots.example.com")
        self.assertEqual(server.normalize_url("http://host.lan:80"), "http://host.lan")

    def test_rejects_bad_input(self):
        for text in ("", "   ", "ftp://host", "http://", "http://user:pw@host", "http://host:99999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                server.normalize_url(text)

    def test_candidates_try_other_scheme_and_default_port(self):
        self.assertEqual(server.candidate_urls("homelab.ts.net"), [
            "https://homelab.ts.net", "http://homelab.ts.net",
            "https://homelab.ts.net:4310", "http://homelab.ts.net:4310"])
        self.assertEqual(server.candidate_urls("http://localhost:4310"), ["http://localhost:4310"])
        self.assertEqual(server.candidate_urls("localhost"),
                         ["http://localhost", "https://localhost", "http://localhost:4310",
                          "https://localhost:4310"])

    def test_cleartext_warning_only_for_public_hosts(self):
        self.assertTrue(server.is_cleartext_remote("http://dots.example.com"))
        self.assertFalse(server.is_cleartext_remote("https://dots.example.com"))
        self.assertFalse(server.is_cleartext_remote("http://192.168.0.10:4310"))
        self.assertFalse(server.is_cleartext_remote(
            "http://100.101.102.103:4310"))  # privacy: example
        self.assertFalse(server.is_cleartext_remote(
            "http://homelab.tail0000.ts.net:4310"))  # privacy: example

    def test_origin_of(self):
        self.assertEqual(server.origin_of("http://127.0.0.1:4310/#/spaces/1"),
                         "http://127.0.0.1:4310")
        self.assertIsNone(server.origin_of("about:blank"))
        self.assertIsNone(server.origin_of("file:///etc/passwd"))


class ClassifyTest(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(server.classify(200, {"settings": {"name": "Home"}}, False).name, "Home")
        unlock = {"error": "Enter your owner access token to unlock OpenDots."}
        self.assertEqual(server.classify(401, unlock, False).kind, server.TOKEN_REQUIRED)
        self.assertEqual(server.classify(401, unlock, True).kind, server.TOKEN_INVALID)
        self.assertEqual(server.classify(403, {"error": "Unrecognized host."}, False).kind,
                         server.LOCAL_ONLY)
        self.assertEqual(server.classify(200, None, False).kind, server.NOT_OPENDOTS)
        self.assertEqual(server.classify(404, {"error": "x"}, False).kind, server.NOT_OPENDOTS)
        self.assertEqual(server.classify(401, None, False).kind, server.NOT_OPENDOTS)


class ProbeTest(unittest.TestCase):
    def probe(self, url, token=None):
        box = []
        server.ServerClient(url, token).probe(box.append)
        self.assertTrue(run_until(lambda: box, 15))
        return box[0]

    def test_probe_against_fake_server(self):
        with FakeOpenDots(token="s3cret-token-with-24-chars!", dots=["Chefe", "Zelador"]) as fake:
            self.assertEqual(self.probe(fake.url).kind, server.TOKEN_REQUIRED)
            self.assertEqual(self.probe(fake.url, "wrong").kind, server.TOKEN_INVALID)
            result = self.probe(fake.url, "s3cret-token-with-24-chars!")
            self.assertTrue(result.ok)
            self.assertEqual(result.name, "OpenDots Test")
            self.assertEqual(result.dots, ["Chefe", "Zelador"])
            self.assertIn(("/api/state", "Bearer s3cret-token-with-24-chars!"), fake.requests)

    def test_local_only_and_unreachable(self):
        with FakeOpenDots(local_only=True) as fake:
            self.assertEqual(self.probe(fake.url).kind, server.LOCAL_ONLY)
        url = fake.url  # the server is stopped now
        result = self.probe(url)
        self.assertEqual(result.kind, server.UNREACHABLE)
        self.assertIn("127.0.0.1", result.message)


if __name__ == "__main__":
    unittest.main()
