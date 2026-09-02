"""Facilitator HTTP client: allowlist, no redirects, bounded bodies."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import facilitator, payment


class AllowlistTests(unittest.TestCase):
    def test_official_endpoints_allowed(self):
        for url in facilitator.ALLOWLISTED_URLS:
            self.assertTrue(facilitator.facilitator_url_allowed(url))

    def test_rejects_caller_controlled_and_credentials(self):
        self.assertFalse(facilitator.facilitator_url_allowed("https://evil.example/verify"))
        self.assertFalse(
            facilitator.facilitator_url_allowed("http://facilitator.payai.network/verify")
        )
        self.assertFalse(
            facilitator.facilitator_url_allowed(
                "https://user:pass@facilitator.payai.network/verify"
            )
        )
        self.assertFalse(
            facilitator.facilitator_url_allowed("https://facilitator.payai.network/verify?next=1")
        )
        self.assertFalse(
            facilitator.facilitator_url_allowed("https://facilitator.payai.network/other")
        )

    def test_post_json_refuses_non_allowlisted(self):
        status, payload = facilitator.post_json(
            "https://evil.example/verify", {"x": 1}, headers={"Authorization": "Bearer x"}
        )
        self.assertIsNone(status)
        self.assertEqual(payload.get("error"), "invalid_facilitator_url")

    def test_call_rejects_non_allowlisted_url(self):
        result = facilitator._call(
            "solana", "https://evil.example/verify", {}, 1.0
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_facilitator_url")


class NoRedirectTests(unittest.TestCase):
    def test_redirect_handler_returns_none(self):
        handler = facilitator.NoRedirectHandler()
        req = urllib.request.Request(
            facilitator.PAYAI_VERIFY_URL,
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer secret-token"},
        )
        nxt = handler.redirect_request(
            req, None, 302, "Found", {}, "https://evil.example/steal"
        )
        self.assertIsNone(nxt)

    def test_post_json_does_not_follow_redirect(self):
        opened = []

        class FakeOpener:
            def open(self, req, timeout=None):
                opened.append(req.full_url)
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found", {"Location": "https://evil.example/x"}, io.BytesIO(b"")
                )

        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            status, payload = facilitator.post_json(
                facilitator.PAYAI_VERIFY_URL,
                {"ok": True},
                headers={"Authorization": "Bearer secret-token"},
            )
        self.assertEqual(opened, [facilitator.PAYAI_VERIFY_URL])
        self.assertEqual(status, 302)
        self.assertNotIn("raw", payload)
        self.assertNotIn("secret-token", json.dumps(payload))


class BoundedBodyTests(unittest.TestCase):
    def test_oversized_valid_json_prefix_is_not_accepted(self):
        prefix = b'{"isValid":true}'
        raw = prefix + (b" " * (facilitator.MAX_BODY - len(prefix) + 1))
        self.assertEqual(facilitator._read_capped(io.BytesIO(raw)), b"")

    def test_success_and_error_bodies_capped_without_raw_fragment(self):
        huge = b'{"isValid":true,"note":"' + (b"A" * (70 * 1024)) + b'"}'

        class FakeResp:
            status = 200

            def read(self, n):
                return huge[:n]

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp()

        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            status, payload = facilitator.post_json(facilitator.PAYAI_VERIFY_URL, {})
        self.assertEqual(status, 200)
        self.assertNotIn("raw", payload)

        class Err:
            code = 500

            def read(self, n=None):
                return b"secret-facilitator-body " + (b"B" * 200)

        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            # Force HTTPError path
            class Boom:
                def open(self, req, timeout=None):
                    raise urllib.error.HTTPError(
                        facilitator.PAYAI_VERIFY_URL,
                        500,
                        "err",
                        {},
                        io.BytesIO(b"not-json SECRET-FACILITATOR-BODY"),
                    )

            with patch("urllib.request.build_opener", return_value=Boom()):
                status, payload = facilitator.post_json(facilitator.PAYAI_VERIFY_URL, {})
        self.assertEqual(status, 500)
        self.assertNotIn("raw", payload)
        dumped = json.dumps(payload)
        self.assertNotIn("SECRET-FACILITATOR-BODY", dumped)


class OfficialRailsTests(unittest.TestCase):
    def test_endpoints_are_the_three_rails(self):
        base_v, base_s = facilitator.endpoints_for("base")
        sol_v, sol_s = facilitator.endpoints_for("solana")
        algo_v, algo_s = facilitator.endpoints_for("algorand")
        self.assertTrue(base_v.startswith("https://api.cdp.coinbase.com/"))
        self.assertTrue(sol_v.startswith(payment.SOLANA_FACILITATOR))
        self.assertTrue(algo_v.startswith(payment.ALGORAND_FACILITATOR))
        self.assertTrue(all(facilitator.facilitator_url_allowed(u) for u in (base_v, base_s, sol_v, sol_s, algo_v, algo_s)))


if __name__ == "__main__":
    unittest.main()
