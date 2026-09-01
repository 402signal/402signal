"""Payment authorization replay / work amplification. No raw payment material logged."""

from __future__ import annotations

import os
import threading
import unittest
from io import StringIO
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import payment, replay
from live402.route import handle_route


def _payload(nonce="11"):
    return {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "base",
            "asset": "USDC",
            "currency": payment.USDC_BASE,
            "amount": payment.AMOUNT_ATOMIC,
            "payTo": payment.DEFAULT_PAYTO,
            "maxTimeoutSeconds": 60,
        },
        "payload": {
            "signature": "0x" + ("ab" * 65),
            "authorization": {
                "from": "0x1111111111111111111111111111111111111111",
                "to": payment.DEFAULT_PAYTO,
                "value": payment.AMOUNT_ATOMIC,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + (nonce * 32),
            },
        },
    }


class _Headers(dict):
    def get(self, key, default=None):
        for name, val in self.items():
            if str(name).lower() == str(key).lower():
                return val
        return default


def _headers_for(payload):
    import base64
    import json

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _Headers({"PAYMENT-SIGNATURE": base64.b64encode(raw).decode("ascii")})


def _fake_facilitator(url, body, headers=None, timeout=20.0):
    _ = headers, timeout, body
    if str(url).rstrip("/").endswith("/verify"):
        return 200, {"isValid": True}
    if str(url).rstrip("/").endswith("/settle"):
        return 200, {"success": True, "network": "eip155:8453"}
    return 404, {"error": "unexpected"}


class ReplayFingerprintTests(unittest.TestCase):
    def test_same_payload_and_rail_same_digest(self):
        accept = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": payment.AMOUNT_ATOMIC,
            "payTo": payment.DEFAULT_PAYTO,
        }
        a = replay.canonical_fingerprint(_payload("aa"), accept)
        b = replay.canonical_fingerprint(_payload("aa"), accept)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)
        c = replay.canonical_fingerprint(_payload("bb"), accept)
        self.assertNotEqual(a, c)


class ConcurrentReplayTests(unittest.TestCase):
    def setUp(self):
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        os.environ.pop("LOCAL_FREE", None)

    def tearDown(self):
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)

    def test_concurrent_identical_auth_one_probe_one_settle(self):
        probe_started = threading.Event()
        release_probe = threading.Event()
        probe_calls = []
        settle_calls = []

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(url)
                return 200, {"success": True, "transaction": "0x" + ("cd" * 32)}
            return 404, {"error": "unexpected"}

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            probe_calls.append(url)
            probe_started.set()
            release_probe.wait(timeout=2)
            return {
                "url": url,
                "live": True,
                "status": 402,
                "has_402_challenge": True,
                "payable": True,
                "invocable": True,
                "payTo": payment.DEFAULT_PAYTO,
                "selected_payment": {
                    "rail": "base",
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount_atomic": 10000,
                    "payTo": payment.DEFAULT_PAYTO,
                },
            }

        headers = _headers_for(_payload("cc"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        results = []

        def worker():
            results.append(
                handle_route(body, headers, "https://402signal.com/route")
            )

        with patch("live402.facilitator.post_json", side_effect=fake_post), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ), patch("live402.probe.route_need") as route_need:
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            self.assertTrue(probe_started.wait(timeout=2))
            t2.start()
            release_probe.set()
            t1.join(timeout=3)
            t2.join(timeout=3)
            route_need.assert_not_called()

        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], results[1][0])
        self.assertIn(results[0][0], (200, 503))

    def test_sequential_replay_does_not_settle_again(self):
        settle_calls = []

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(1)
                return 200, {"success": True}
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("dd"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        with patch("live402.facilitator.post_json", side_effect=fake_post):
            first = handle_route(body, headers, "https://402signal.com/route")
            second = handle_route(body, headers, "https://402signal.com/route")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(settle_calls), 1)

    def test_fingerprint_not_logged(self):
        headers = _headers_for(_payload("ee"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        accept = payment.match_accept(
            payment.extract_payment_payload(headers),
            payment.payment_required("https://402signal.com/route"),
        )
        fp = replay.canonical_fingerprint(payment.extract_payment_payload(headers), accept)
        buf = StringIO()
        with patch("sys.stderr", buf), patch(
            "live402.facilitator.post_json", side_effect=_fake_facilitator
        ):
            handle_route(body, headers, "https://402signal.com/route")
        logged = buf.getvalue()
        self.assertNotIn(fp, logged)
        self.assertNotIn("0x" + ("ab" * 65), logged)
        sig = headers.get("PAYMENT-SIGNATURE")
        self.assertNotIn(sig, logged)


if __name__ == "__main__":
    unittest.main()
