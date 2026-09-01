"""Production request logs must not include secrets, query, or bodies."""

from __future__ import annotations

import json
import os
import unittest
from io import StringIO
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import payment, replay, server
from live402.route import handle_route
from tests.test_pay_replay import _fake_facilitator, _headers_for, _payload
from tests.test_route import _get_full, _json_post, _serve


class _DummyHandler(server.Handler):
    def __init__(self):
        self.path = "/preview?need=secret-need&policy=under-one-cent"
        self.command = "GET"
        self._request_id = "abc123deadbeef00"
        self._req_started = 0.0
        self._logged_access = False
        self.client_address = ("127.0.0.1", 9)
        self.headers = {}


class AccessLogTests(unittest.TestCase):
    def test_log_request_is_path_only(self):
        handler = _DummyHandler()
        buf = StringIO()
        with patch("sys.stderr", buf), patch("time.monotonic", return_value=0.2):
            handler.log_request(200)
        text = buf.getvalue()
        self.assertIn("request_id=abc123deadbeef00", text)
        self.assertIn("method=GET", text)
        self.assertIn("path=/preview", text)
        self.assertIn("status=200", text)
        self.assertIn("endpoint=preview", text)
        self.assertIn("latency_ms=", text)
        self.assertNotIn("secret-need", text)
        self.assertNotIn("policy=", text)
        self.assertNotIn("need=", text)
        self.assertNotIn("?", text)

    def test_validate_query_not_logged(self):
        handler = _DummyHandler()
        handler.path = "/validate?url=https://seller.example/x402?token=abc"
        handler.command = "GET"
        buf = StringIO()
        with patch("sys.stderr", buf):
            handler.log_request(200)
        text = buf.getvalue()
        self.assertIn("path=/validate", text)
        self.assertNotIn("seller.example", text)
        self.assertNotIn("token=abc", text)

    def test_default_request_line_not_emitted(self):
        handler = _DummyHandler()
        handler.path = "/route?need=hidden"
        buf = StringIO()
        with patch("sys.stderr", buf):
            handler.log_message('"%s" %s %s', "GET /route?need=hidden HTTP/1.1", "402", "-")
        text = buf.getvalue()
        self.assertNotIn("need=hidden", text)
        self.assertNotIn("PAYMENT-SIGNATURE", text)


class LiveLogCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["FLY_APP_NAME"] = "402signal-test"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("FLY_APP_NAME", None)

    def test_preview_and_validate_logs_omit_query(self):
        buf = StringIO()
        with patch("sys.stderr", buf):
            _get_full(self.port, "/preview?need=secret-need&policy=cheap")
            _get_full(self.port, "/validate?url=https://fixture.402signal.local/weather")
        text = buf.getvalue()
        self.assertNotIn("secret-need", text)
        self.assertNotIn("policy=cheap", text)
        self.assertNotIn("fixture.402signal.local", text)
        self.assertNotIn("PAYMENT-SIGNATURE", text)
        self.assertNotIn("X-PAYMENT", text)

    def test_payment_header_absent_from_logs(self):
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        headers = {
            "PAYMENT-SIGNATURE": "this-is-not-a-real-payment-blob",
            "X-PAYMENT": "legacy-payment-blob",
        }
        buf = StringIO()
        try:
            with patch("sys.stderr", buf):
                _json_post(self.port, "/route", {"need": "weather"}, extra_headers=headers)
        finally:
            os.environ.pop("CDP_ACCESS_TOKEN", None)
            replay.reset()
        text = buf.getvalue()
        self.assertNotIn("this-is-not-a-real-payment-blob", text)
        self.assertNotIn("legacy-payment-blob", text)
        self.assertNotIn('"need": "weather"', text)


class SettlementLogTests(unittest.TestCase):
    def setUp(self):
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"

    def tearDown(self):
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)

    def test_settle_log_has_no_txid(self):
        tx = "0x" + ("ab" * 32)
        headers = _headers_for(_payload("lg"))
        buf = StringIO()

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                return 200, {"success": True, "transaction": tx, "network": "eip155:8453"}
            return 404, {}

        with patch("sys.stderr", buf), patch(
            "live402.facilitator.post_json", side_effect=fake_post
        ):
            handle_route(
                {"need": "weather", "url": "https://fixture.402signal.local/weather"},
                headers,
                "https://402signal.com/route",
            )
        text = buf.getvalue()
        self.assertIn("settlement_success=true", text)
        self.assertIn("rail=base", text)
        self.assertIn("request_id=", text)
        self.assertNotIn(tx, text)
        sig = headers.get("PAYMENT-SIGNATURE")
        self.assertNotIn(sig, text)


class ClientIpTests(unittest.TestCase):
    def test_xff_never_trusted(self):
        os.environ.pop("FLY_APP_NAME", None)
        os.environ.pop("FLY_ALLOC_ID", None)
        os.environ.pop("FLY_MACHINE_ID", None)

        class H:
            headers = {"X-Forwarded-For": "198.51.100.9", "Fly-Client-IP": "203.0.113.9"}
            client_address = ("127.0.0.1", 9)

        self.assertEqual(server.client_ip(H()), "127.0.0.1")

    def test_fly_client_ip_only_on_fly(self):
        os.environ["FLY_APP_NAME"] = "402signal-test"

        class H:
            headers = {"X-Forwarded-For": "198.51.100.9", "Fly-Client-IP": "203.0.113.9"}
            client_address = ("127.0.0.1", 9)

        try:
            self.assertEqual(server.client_ip(H()), "203.0.113.9")
        finally:
            os.environ.pop("FLY_APP_NAME", None)
        self.assertEqual(server.client_ip(H()), "127.0.0.1")


class RateLimiterBoundTests(unittest.TestCase):
    def test_map_is_bounded(self):
        limiter = server._RateLimiter(max_keys=8)
        for i in range(40):
            limiter.allow("k%s" % i, 10)
        self.assertLessEqual(limiter.key_count(), 8)


if __name__ == "__main__":
    unittest.main()
