"""Direct URL abuse controls. Unknown hosts stay 443-only. SSRF unchanged."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import probe, route


class DirectUrlPortTests(unittest.TestCase):
    def test_unknown_unusual_port_blocked(self):
        url = "https://public.example:8443/x402"
        self.assertFalse(probe.direct_url_allowed(url, None))
        with patch("live402.route._lookup_claimed", return_value=None), patch(
            "live402.probe.probe_url"
        ) as probed:
            code, body = route.run_probe({"url": url})
        self.assertEqual(code, 503)
        self.assertEqual(body.get("miss_reason"), "ssrf")
        self.assertFalse(body.get("live"))
        probed.assert_not_called()

    def test_unknown_443_may_probe(self):
        url = "https://public.example/x402"
        self.assertTrue(probe.direct_url_allowed(url, None))

    def test_catalog_known_non_443_allowed(self):
        url = "https://seller.example:8443/x402"
        item = {"url": url, "method": "POST"}
        self.assertTrue(probe.direct_url_allowed(url, item))

    def test_credentials_and_http_blocked(self):
        self.assertFalse(probe.direct_url_allowed("https://user:pass@public.example/x", None))
        self.assertFalse(probe.direct_url_allowed("http://public.example/x", None))
        self.assertFalse(probe.direct_url_allowed("https://public.example:22/x", None))


class DirectUrlProbeLimitsTests(unittest.TestCase):
    def test_probe_never_sends_arbitrary_body(self):
        calls = []

        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            calls.append({"method": method, "data": data})
            return {
                "live": False,
                "status": 405,
                "has_402_challenge": False,
                "payTo": None,
                "miss_reason": "no_402_envelope",
                "envelope": None,
            }

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._pin_https_target",
            return_value=("https://public.example/x402", [("203.0.113.10", 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            probe.probe_url("https://public.example/x402")
        post = [c for c in calls if c["method"] == "POST"]
        self.assertTrue(post)
        self.assertEqual(post[0]["data"], b"{}")


if __name__ == "__main__":
    unittest.main()
