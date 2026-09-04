"""SEC-TEST-001: MCP JSON-RPC batch is not multi-route accounting.

POST /mcp rejects a JSON array via http_body.loads_json_object before
payment or handle_mcp. A single JSON-RPC object still hits the paid
tools/call route gate. No Fly. No product accounting change.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import http_body
from live402.http_body import BodyReadError
from live402.server import Handler

FAKE_PAYMENT = {"PAYMENT-SIGNATURE": "not-a-real-payment"}


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _host, port = httpd.server_address
    return httpd, port


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


def _tools_call_route(req_id, need):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": "route", "arguments": {"need": need}},
    }


class McpBatchAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        cls.httpd, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_loads_json_object_rejects_two_call_batch(self):
        raw = json.dumps(
            [
                _tools_call_route(1, "weather"),
                _tools_call_route(2, "token balance"),
            ]
        ).encode("utf-8")
        with self.assertRaises(BodyReadError) as ctx:
            http_body.loads_json_object(raw)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.error, "JSON object required")

    def test_mcp_jsonrpc_batch_rejected_paid_object_unchanged(self):
        """SEC-TEST-001: array of two paid tools/call routes is 400, not dual charge."""
        batch = [
            _tools_call_route(1, "weather"),
            _tools_call_route(2, "token balance"),
        ]
        with patch("live402.mcp.handle_mcp") as mock_mcp, patch(
            "live402.facilitator.verify"
        ) as mock_verify, patch("live402.facilitator.settle") as mock_settle:
            status, body = _json_post(
                self.port, "/mcp", batch, extra_headers=FAKE_PAYMENT
            )
            mock_mcp.assert_not_called()
            mock_verify.assert_not_called()
            mock_settle.assert_not_called()
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "JSON object required")
        self.assertNotIn("accepts", body if isinstance(body, dict) else {})

        status, body = _json_post(
            self.port,
            "/mcp",
            _tools_call_route(1, "weather"),
            extra_headers=FAKE_PAYMENT,
        )
        self.assertEqual(status, 402)
        self.assertIn("accepts", body)
        self.assertEqual(len(body.get("accepts") or []), 3)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["3000", "3000", "3000"])


if __name__ == "__main__":
    unittest.main()
