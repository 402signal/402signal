"""PR1 F: malformed / out-of-range ports are blocked, never ValueError."""

from __future__ import annotations

import os
import unittest
from urllib.parse import urlparse

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import probe


class PortSafetyTests(unittest.TestCase):
    def test_url_port_invalid_does_not_raise(self):
        parsed = urlparse("https://example.com:abc/x")
        self.assertIsNone(probe.url_port(parsed))
        parsed = urlparse("https://example.com:99999/x")
        self.assertIsNone(probe.url_port(parsed))
        parsed = urlparse("https://example.com:0/x")
        self.assertIsNone(probe.url_port(parsed))
        parsed = urlparse("https://example.com/x")
        self.assertEqual(probe.url_port(parsed), 443)
        parsed = urlparse("https://example.com:8443/x")
        self.assertEqual(probe.url_port(parsed), 8443)

    def test_safe_target_invalid_port(self):
        self.assertIsNone(probe.safe_target("https://example.com:abc/x"))
        self.assertIsNone(probe.safe_target("https://example.com:99999/x"))
        self.assertIsNone(probe.safe_target("https://example.com:0/x"))

    def test_pin_https_target_invalid_port(self):
        self.assertIsNone(probe._pin_https_target("https://example.com:abc/x"))
        self.assertIsNone(probe._pin_https_target("https://example.com:70000/x"))

    def test_one_request_invalid_port_is_ssrf(self):
        snap = probe._one_request("https://example.com:abc/x", "GET")
        self.assertFalse(snap.get("live"))
        self.assertEqual(snap.get("miss_reason"), "ssrf")

    def test_ssrf_invariants_still_hold(self):
        self.assertIsNone(probe.safe_target("http://example.com"))
        self.assertIsNone(probe.safe_target("https://127.0.0.1"))
        self.assertIsNone(probe.safe_target("https://user:pass@example.com/"))
        self.assertIsNone(probe.safe_target("https://10.0.0.1/x"))
        self.assertIsNone(probe.safe_target("https://169.254.169.254/latest/meta-data"))


if __name__ == "__main__":
    unittest.main()
