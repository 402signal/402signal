"""Production-style binds refuse all local/test-support modes."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from live402 import server


class BootGuardTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "LOCAL_FREE",
                "LIVE402_FIXTURE",
                "LIVE402_PQ_TEST_SUPPORT",
                "LIVE402_ALLOW_UNSAFE_DEV_MODE",
                "PORT",
                "FLY_APP_NAME",
            )
        }
        for key in (
            "LOCAL_FREE",
            "LIVE402_FIXTURE",
            "LIVE402_PQ_TEST_SUPPORT",
            "LIVE402_ALLOW_UNSAFE_DEV_MODE",
            "PORT",
            "FLY_APP_NAME",
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_loopback_allows_fixture(self):
        os.environ["LIVE402_FIXTURE"] = "1"
        server.assert_safe_http_boot("127.0.0.1")
        os.environ["LOCAL_FREE"] = "1"
        server.assert_safe_http_boot("localhost")

    def test_public_bind_refuses_fixture(self):
        os.environ["LIVE402_FIXTURE"] = "1"
        with self.assertRaises(SystemExit):
            server.assert_safe_http_boot("0.0.0.0")

    def test_public_bind_refuses_local_free(self):
        os.environ["LOCAL_FREE"] = "1"
        with self.assertRaises(SystemExit):
            server.assert_safe_http_boot("0.0.0.0")

    def test_public_bind_refuses_pq_test_support(self):
        os.environ["LIVE402_PQ_TEST_SUPPORT"] = "1"
        with self.assertRaises(SystemExit):
            server.assert_safe_http_boot("0.0.0.0")

    def test_port_env_is_public(self):
        os.environ["PORT"] = "8080"
        os.environ["LIVE402_FIXTURE"] = "1"
        with self.assertRaises(SystemExit):
            server.assert_safe_http_boot("127.0.0.1")

    def test_explicit_override_allows_local(self):
        os.environ["LIVE402_FIXTURE"] = "1"
        os.environ["LIVE402_ALLOW_UNSAFE_DEV_MODE"] = "1"
        server.assert_safe_http_boot("0.0.0.0")

    def test_explicit_override_never_allows_fly(self):
        os.environ["LIVE402_FIXTURE"] = "1"
        os.environ["LIVE402_ALLOW_UNSAFE_DEV_MODE"] = "1"
        os.environ["FLY_APP_NAME"] = "402signal"
        with self.assertRaises(SystemExit):
            server.assert_safe_http_boot("0.0.0.0")

    def test_main_calls_boot_guard(self):
        import inspect

        src = inspect.getsource(server.main)
        self.assertIn("assert_safe_http_boot", src)

    def test_override_not_in_production_config(self):
        root = Path(__file__).resolve().parents[1]
        fly = (root / "fly.toml").read_text(encoding="utf-8")
        self.assertNotIn("LIVE402_ALLOW_UNSAFE_DEV_MODE", fly)
        self.assertNotIn("LOCAL_FREE", fly)
        self.assertNotIn("LIVE402_PQ_TEST_SUPPORT", fly)
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("LIVE402_ALLOW_UNSAFE_DEV_MODE", dockerfile)
        self.assertNotIn("LOCAL_FREE=1", dockerfile)

    def test_fly_requires_liveness_and_readiness(self):
        root = Path(__file__).resolve().parents[1]
        fly = (root / "fly.toml").read_text(encoding="utf-8")
        self.assertEqual(fly.count('path = "/health"'), 1)
        self.assertEqual(fly.count('path = "/ready"'), 1)

    def test_is_public_bind_helpers(self):
        self.assertTrue(server.is_public_http_bind("0.0.0.0"))
        self.assertFalse(server.is_public_http_bind("127.0.0.1"))
        self.assertTrue(server.is_loopback_bind("127.0.0.1"))


class MainBootGuardIntegration(unittest.TestCase):
    def test_main_refuses_public_fixture(self):
        with patch.dict(
            os.environ,
            {"LIVE402_FIXTURE": "1", "PORT": "8080"},
            clear=False,
        ):
            os.environ.pop("LIVE402_ALLOW_UNSAFE_DEV_MODE", None)
            with self.assertRaises(SystemExit):
                server.main(["--host", "0.0.0.0", "--port", "8080"])


if __name__ == "__main__":
    unittest.main()
