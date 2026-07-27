"""Config validation tests. See implementation.md "Config" and "Tests"."""

import os
import unittest
from unittest.mock import patch

from config import REMOVED_KEYS, ConfigError, load_config, normalize_base_url, parse_base_urls

BASE_ENV = {"BASE_URLS": "https://xxxclub.to"}


def with_env(**overrides):
    env = dict(BASE_ENV)
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


class TestRemovedKeys(unittest.TestCase):
    def test_each_removed_key_is_rejected(self):
        for key in REMOVED_KEYS:
            with self.subTest(key=key):
                with with_env(**{key: "1"}):
                    with self.assertRaises(ConfigError) as ctx:
                        load_config()
                self.assertIn(key, str(ctx.exception))

    def test_message_names_the_replacement(self):
        with with_env(MAX_REQUESTS_PER_SECOND="0.5"):
            with self.assertRaises(ConfigError) as ctx:
                load_config()
        self.assertIn("REQUESTS_PER_SECOND_PER_DOMAIN", str(ctx.exception))

    def test_empty_value_is_not_treated_as_set(self):
        with with_env(MAX_CONCURRENCY=""):
            load_config()  # must not raise


class TestRateValidation(unittest.TestCase):
    def test_rejects_unusable_rates(self):
        for value in ("0", "-1", "inf", "nan", "-inf", "abc", "0.001"):
            with self.subTest(value=value):
                with with_env(REQUESTS_PER_SECOND_PER_DOMAIN=value):
                    with self.assertRaises(ConfigError):
                        load_config()

    def test_accepts_fractional_rates(self):
        for value in ("0.5", "1", "0.01", "3"):
            with self.subTest(value=value):
                with with_env(REQUESTS_PER_SECOND_PER_DOMAIN=value):
                    cfg = load_config()
                self.assertEqual(cfg["REQUESTS_PER_SECOND_PER_DOMAIN"], float(value))

    def test_rejects_unusable_cooldown_and_timeout(self):
        for key in ("BLOCK_COOLDOWN_SECONDS", "REQUEST_TIMEOUT_SECONDS"):
            for value in ("0", "-5", "inf", "nan", "x"):
                with self.subTest(key=key, value=value):
                    with with_env(**{key: value}):
                        with self.assertRaises(ConfigError):
                            load_config()


class TestBaseUrls(unittest.TestCase):
    def test_three_domains(self):
        with with_env(BASE_URLS="https://xxxclub.to,https://xxxclub.cc,https://xxxclub.me"):
            cfg = load_config()
        self.assertEqual(
            cfg["BASE_URLS"],
            ["https://xxxclub.to", "https://xxxclub.cc", "https://xxxclub.me"],
        )

    def test_whitespace_is_trimmed(self):
        with with_env(BASE_URLS=" https://xxxclub.to , https://xxxclub.cc "):
            cfg = load_config()
        self.assertEqual(cfg["BASE_URLS"], ["https://xxxclub.to", "https://xxxclub.cc"])

    def test_duplicate_host_is_rejected(self):
        """Two queue slots for one domain would double that domain's rate and
        concurrency while the startup log still reported the intended figure."""
        for raw in (
            "https://xxxclub.to,https://xxxclub.to",
            "https://xxxclub.to,https://XXXCLUB.TO",
            "https://xxxclub.to,https://xxxclub.to/",
        ):
            with self.subTest(raw=raw):
                with with_env(BASE_URLS=raw):
                    with self.assertRaises(ConfigError) as ctx:
                        parse_base_urls()
                self.assertIn("more than once", str(ctx.exception))

    def test_rejects_malformed_entries(self):
        cases = [
            "https://xxxclub.to,",                      # empty entry
            "ftp://xxxclub.to",                         # scheme
            "xxxclub.to",                               # no scheme
            "https://user:pass@xxxclub.to",             # credentials
            "https://xxxclub.to/torrents",              # path
            "https://xxxclub.to?a=1",                   # query
            "https://",                                 # no host
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with with_env(BASE_URLS=raw):
                    with self.assertRaises(ConfigError):
                        parse_base_urls()

    def test_port_is_preserved(self):
        self.assertEqual(normalize_base_url("http://localhost:8080"), "http://localhost:8080")

    def test_falls_back_to_legacy_base_url(self):
        with patch.dict(os.environ, {"BASE_URL": "https://xxxclub.cc"}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg["BASE_URLS"], ["https://xxxclub.cc"])

    def test_default_when_nothing_is_set(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg["BASE_URLS"], ["https://xxxclub.to"])


if __name__ == "__main__":
    unittest.main()
