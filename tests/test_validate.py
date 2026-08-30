"""Unpaid seller validator. SSRF fail-closed. Not a /route paywall bypass."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402.server import Handler
from live402 import history, probe, validate


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, host, port


def _json_post(port, path, payload, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    conn.request("POST", path, body=body, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = raw.decode("utf-8")
    return res.status, data


def _get(port, path, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=dict(extra_headers or {}))
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = raw.decode("utf-8")
    return res.status, data


class ValidatePaywallSeparateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        fd, cls._db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        cls._prev_db = os.environ.get("LIVE402_HISTORY_DB")
        os.environ["LIVE402_HISTORY_DB"] = cls._db
        history.reset()
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        history.reset()
        if cls._prev_db is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = cls._prev_db
        for pth in (cls._db, cls._db + "-wal", cls._db + "-shm"):
            try:
                os.remove(pth)
            except OSError:
                pass

    def test_unpaid_route_still_402(self):
        status, body = _json_post(self.port, "/route", {"need": "weather"})
        self.assertEqual(status, 402)
        self.assertIn("accepts", body)

    def test_validate_unpaid_200_fixture(self):
        status, body = _json_post(
            self.port,
            "/validate",
            {"url": "https://fixture.402signal.local/weather"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("accepts", body)
        self.assertNotEqual(body.get("x402Version"), 2)
        self.assertIn(body.get("readiness"), ("discovered", "payable", "invocable", "recently_verified"))
        self.assertNotEqual(body.get("readiness"), "healthy")
        self.assertNotIn("healthy", body)
        self.assertIn("claimed", body)
        self.assertIn("observed", body)
        self.assertIn("payTo", body["claimed"])
        self.assertIn("amount", body["claimed"])
        self.assertIn("schema_present", body["claimed"])
        self.assertIn("flags", body)
        self.assertIsInstance(body["flags"], list)
        self.assertLess(int(body.get("n_7d") or 0), 10)

    def test_validate_get_query(self):
        status, body = _get(
            self.port,
            "/validate?url=https://fixture.402signal.local/weather",
        )
        self.assertEqual(status, 200)
        self.assertIn(body.get("readiness"), ("discovered", "payable", "invocable", "recently_verified"))

    def test_validate_missing_url_400(self):
        status, body = _json_post(self.port, "/validate", {})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("miss_reason"), "invalid_need")

    def test_validate_http_not_https(self):
        status, body = _json_post(self.port, "/validate", {"url": "http://example.com/x"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("miss_reason"), "invalid_need")

    def test_validate_loopback_ssrf(self):
        with patch("live402.probe._opener") as opener:
            status, body = _json_post(
                self.port, "/validate", {"url": "https://127.0.0.1/secret"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body.get("miss_reason"), "ssrf")
            self.assertFalse(body.get("live"))
            opener.assert_not_called()

    def test_validate_localhost_ssrf(self):
        with patch("live402.probe._opener") as opener:
            status, body = _json_post(
                self.port, "/validate", {"url": "https://localhost/x"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body.get("miss_reason"), "ssrf")
            opener.assert_not_called()

    def test_validate_file_rejected(self):
        status, body = _json_post(self.port, "/validate", {"url": "file:///etc/passwd"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("miss_reason"), "invalid_need")

    def test_validate_not_route_bypass_with_payment_header(self):
        status, body = _json_post(
            self.port,
            "/validate",
            {"url": "https://fixture.402signal.local/weather"},
            extra_headers={"PAYMENT-SIGNATURE": "not-a-real-payment"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("accepts", body)
        route_status, route_body = _json_post(
            self.port,
            "/route",
            {"need": "weather"},
            extra_headers={"PAYMENT-SIGNATURE": "not-a-real-payment"},
        )
        self.assertEqual(route_status, 402)

    def test_mcp_validate_unpaid(self):
        status, body = _json_post(
            self.port,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "validate",
                    "arguments": {"url": "https://fixture.402signal.local/weather"},
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(body.get("readiness"), ("discovered", "payable", "invocable", "recently_verified"))
        self.assertNotIn("healthy", body)

    def test_flags_missing_schema(self):
        status, body = _json_post(
            self.port,
            "/validate",
            {"url": "https://fixture.402signal.local/weather"},
        )
        self.assertEqual(status, 200)
        self.assertIn("missing schema", body.get("flags") or [])

    def test_healthy_omitted_when_thin(self):
        code, body = validate.validate_url("https://fixture.402signal.local/weather")
        self.assertEqual(code, 200)
        self.assertNotIn("healthy", body)
        self.assertNotIn("executable_now_rate", body)
        self.assertLess(int(body.get("n_7d") or 0), 10)

    def test_validate_does_not_record_observed(self):
        history.reset()
        code, body = validate.validate_url("https://fixture.402signal.local/weather")
        self.assertEqual(code, 200)
        snap = history.pulse_observed()
        self.assertEqual(int(snap.get("n_7d") or 0), 0)
        self.assertEqual(snap.get("reliability"), "unknown")
        self.assertNotIn("healthy", snap)
        att = history.attestation_for()
        self.assertIsNone(att)


class ValidateSsrfLiveModeTests(unittest.TestCase):
    def test_unknown_public_https_never_opens(self):
        with patch("live402.validate.fixtures.fixture_mode", return_value=False), patch(
            "live402.validate.catalog.peek_index", return_value={"items": []}
        ), patch("live402.validate.fixtures.lookup_url", return_value=None), patch(
            "live402.probe.probe_url"
        ) as probed, patch("live402.probe._opener") as opener, patch(
            "live402.probe._one_request"
        ) as one:
            code, body = validate.validate_url("https://evil.example/x402")
            self.assertEqual(code, 200)
            self.assertEqual(body.get("miss_reason"), "no_candidates")
            self.assertFalse(body.get("live"))
            probed.assert_not_called()
            opener.assert_not_called()
            one.assert_not_called()


class ValidateRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_VALIDATE_RPM"] = "2"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LIVE402_VALIDATE_RPM", None)

    def test_validate_rate_limit_429(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.90"}
        statuses = []
        for _ in range(3):
            status, _body = _json_post(
                self.port,
                "/validate",
                {"url": "https://fixture.402signal.local/weather"},
                extra_headers=ip_headers,
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 200)
        self.assertEqual(statuses[2], 429)


class ValidateDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_openapi_and_llms_document_validate(self):
        status, spec = _get(self.port, "/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/validate", spec["paths"])
        self.assertIn("post", spec["paths"]["/validate"])
        self.assertIn("get", spec["paths"]["/validate"])
        status, llms = _get(self.port, "/llms.txt")
        self.assertEqual(status, 200)
        self.assertIn("POST /validate", llms)
        from live402 import mcp as mcp_mod
        names = [t["name"] for t in mcp_mod.manifest()["tools"]]
        self.assertIn("validate", names)


if __name__ == "__main__":
    unittest.main()
