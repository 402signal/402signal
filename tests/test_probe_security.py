"""402security: seller-body tightening + DNS IP-pin. No Falcon, no PQ."""

from __future__ import annotations

import os
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import probe, validate

PUBLIC_IP = "203.0.113.10"
PRIVATE_IP = "127.0.0.1"
SELLER_URL = "https://seller.example/x402"


def _declared_item(url=SELLER_URL, body=None):
    return {
        "url": url,
        "description": "weather forecast",
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {
                        "method": "POST",
                        "body": body
                        if body is not None
                        else {"secret": "exfiltrate-me", "prompt": "ignore previous"},
                    }
                }
            }
        },
    }


def _snap(status, live=False, miss="no_402_envelope", challenge=None):
    if challenge is None:
        challenge = status == 402
    return {
        "live": live,
        "status": status,
        "has_402_challenge": challenge,
        "payTo": "0xabc" if live else None,
        "miss_reason": None if live else miss,
        "envelope": {"x402Version": 2, "accepts": [{"payTo": "0xabc"}]} if live else None,
    }


class SellerBodyTighteningTests(unittest.TestCase):
    def test_never_posts_declared_catalog_body(self):
        calls = []

        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            calls.append({"method": method, "data": data, "url": url})
            if method == "GET":
                return _snap(405)
            return _snap(400)

        item = _declared_item()
        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._pin_https_target",
            return_value=(SELLER_URL, [(PUBLIC_IP, 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            result = probe.probe_url(SELLER_URL, catalog_item=item)

        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "unsafe_to_probe")
        self.assertTrue(calls)
        self.assertEqual(calls[0]["method"], "GET")
        post_payloads = [c["data"] for c in calls if c["method"] == "POST"]
        self.assertEqual(post_payloads, [b"{}"])
        blob = b"".join(p or b"" for p in post_payloads)
        self.assertNotIn(b"exfiltrate-me", blob)
        self.assertNotIn(b"secret", blob)
        self.assertNotIn(b"ignore previous", blob)

    def test_unsafe_to_probe_when_declared_body_and_no_live_402(self):
        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            if method == "GET":
                return _snap(200, miss="reachable_200")
            return _snap(200, miss="reachable_200")

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._pin_https_target",
            return_value=(SELLER_URL, [(PUBLIC_IP, 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            result = probe.probe_url(SELLER_URL, catalog_item=_declared_item())
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "unsafe_to_probe")
        self.assertEqual(probe.public_miss_reason("unsafe_to_probe"), "unsafe_to_probe")
        self.assertIn("unsafe_to_probe", probe.MISS_REASONS)

    def test_live_get_402_does_not_post(self):
        calls = []

        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            calls.append(method)
            return _snap(402, live=True, miss=None)

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._pin_https_target",
            return_value=(SELLER_URL, [(PUBLIC_IP, 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            result = probe.probe_url(SELLER_URL, catalog_item=_declared_item())
        self.assertTrue(result.get("live"))
        self.assertEqual(calls, ["GET"])
        self.assertNotEqual(result.get("miss_reason"), "unsafe_to_probe")

    def test_post_empty_only_when_justified(self):
        self.assertFalse(probe._post_empty_justified(_snap(402, live=True, miss=None)))
        self.assertFalse(probe._post_empty_justified(_snap(402, live=False, miss="no_payto")))
        self.assertTrue(probe._post_empty_justified(_snap(405)))
        self.assertTrue(probe._post_empty_justified(_snap(501)))
        self.assertTrue(probe._post_empty_justified(_snap(200, miss="reachable_200")))
        self.assertTrue(probe._post_empty_justified(_snap(404)))
        self.assertFalse(
            probe._post_empty_justified(
                {"live": False, "status": None, "has_402_challenge": False, "miss_reason": "probe_timeout"}
            )
        )
        self.assertFalse(
            probe._post_empty_justified(
                {"live": False, "status": None, "has_402_challenge": False, "miss_reason": "ssrf"}
            )
        )

        calls = []

        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            calls.append({"method": method, "data": data})
            if method == "GET":
                return _snap(402, live=False, miss="no_payto")
            return _snap(402, live=True, miss=None)

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._pin_https_target",
            return_value=(SELLER_URL, [(PUBLIC_IP, 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            result = probe.probe_url(SELLER_URL, catalog_item={"url": SELLER_URL})
        self.assertFalse(result.get("live"))
        self.assertEqual([c["method"] for c in calls], ["GET"])
        self.assertEqual(result.get("miss_reason"), "no_payto")

    def test_validate_shares_helper_and_never_posts_declared_body(self):
        calls = []

        def fake_one(url, method, data=None, deadline=None, pinned_addrs=None):
            calls.append({"method": method, "data": data})
            return _snap(405) if method == "GET" else _snap(400)

        item = _declared_item()
        with patch("live402.validate.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe.fixtures.fixture_mode", return_value=False
        ), patch("live402.validate.catalog_item_for", return_value=item), patch(
            "live402.probe._pin_https_target",
            return_value=(SELLER_URL, [(PUBLIC_IP, 443)]),
        ), patch("live402.probe._one_request", side_effect=fake_one):
            code, body = validate.validate_url(SELLER_URL)
        self.assertEqual(code, 200)
        self.assertFalse(body.get("live"))
        self.assertEqual(body.get("miss_reason"), "unsafe_to_probe")
        post_payloads = [c["data"] for c in calls if c["method"] == "POST"]
        self.assertEqual(post_payloads, [b"{}"])
        self.assertTrue(any(c["method"] == "GET" for c in calls))
        self.assertNotIn(b"exfiltrate-me", b"".join(p or b"" for p in post_payloads))


class DnsIpPinTests(unittest.TestCase):
    def test_toctou_cannot_connect_private_after_public_lookup(self):
        n = {"n": 0}
        connected = []

        def fake_gai(host, port, *args, **kwargs):
            n["n"] += 1
            if n["n"] == 1:
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 443))
                ]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE_IP, port or 443))
            ]

        def fake_cc(address, timeout=None, source_address=None):
            connected.append(address)
            raise OSError("refused")

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "socket.getaddrinfo", side_effect=fake_gai
        ), patch("socket.create_connection", side_effect=fake_cc):
            result = probe.probe_url(SELLER_URL)

        self.assertFalse(result.get("live"))
        for dest in connected:
            self.assertNotEqual(str(dest[0]), PRIVATE_IP)
            self.assertFalse(str(dest[0]).startswith("127."))
        if connected:
            self.assertEqual(str(connected[0][0]), PUBLIC_IP)
        self.assertGreaterEqual(n["n"], 1)

    def test_host_and_sni_are_original_hostname(self):
        req = urllib.request.Request(SELLER_URL)
        req.pinned_addrs = [(PUBLIC_IP, 443)]
        handler = probe._PinnedHTTPSHandler()
        opened = {}

        def fake_do_open(http_class, request, **kwargs):
            opened["host_hdr"] = request.get_header("Host")
            conn = http_class(request.host, timeout=1, context=ssl.create_default_context())
            opened["sni"] = conn._server_hostname
            opened["pin"] = conn._pinned_addrs
            opened["url"] = request.full_url
            raise probe.ProbeBlocked("stop")

        with patch.object(handler, "do_open", side_effect=fake_do_open):
            with self.assertRaises(probe.ProbeBlocked):
                handler.https_open(req)
        self.assertEqual(opened["host_hdr"], "seller.example")
        self.assertEqual(opened["sni"], "seller.example")
        self.assertEqual(opened["pin"][0][0], PUBLIC_IP)
        self.assertIn("seller.example", opened["url"])
        self.assertNotIn(PUBLIC_IP, opened["url"])

        seen = {}

        def fake_cc(address, timeout=None, source_address=None):
            seen["dest"] = address
            return socket.socket()

        ctx = ssl.create_default_context()

        def wrap(sock, server_hostname=None, **kwargs):
            seen["sni"] = server_hostname
            raise ssl.SSLError("test")

        conn = probe._PinnedHTTPSConnection(
            "seller.example",
            pinned_addrs=[(PUBLIC_IP, 443)],
            server_hostname="seller.example",
            context=ctx,
        )
        with patch("socket.create_connection", side_effect=fake_cc), patch.object(
            ctx, "wrap_socket", side_effect=wrap
        ):
            with self.assertRaises(ssl.SSLError):
                conn.connect()
        self.assertEqual(seen["dest"][0], PUBLIC_IP)
        self.assertEqual(seen["sni"], "seller.example")

    def test_blocked_ips_still_blocked(self):
        self.assertIsNone(probe.safe_target("https://127.0.0.1"))
        self.assertIsNone(probe.safe_target("https://10.0.0.1/x"))
        self.assertIsNone(probe.safe_target("https://169.254.169.254/latest/meta-data"))
        self.assertIsNone(probe._checked_addrs("127.0.0.1"))
        self.assertIsNone(probe._checked_addrs("10.0.0.1"))

        def fake_gai(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE_IP, port or 443))]

        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "socket.getaddrinfo", side_effect=fake_gai
        ), patch("socket.create_connection") as cc:
            self.assertFalse(probe._resolve_public("evil.internal"))
            result = probe.probe_url("https://evil.internal/x402")
            self.assertFalse(result.get("live"))
            self.assertEqual(result.get("miss_reason"), "ssrf")
            cc.assert_not_called()

    def test_pin_missing_or_sni_missing_fail_closed(self):
        ctx = ssl.create_default_context()
        missing_pin = probe._PinnedHTTPSConnection(
            "seller.example",
            pinned_addrs=[],
            server_hostname="seller.example",
            context=ctx,
        )
        with self.assertRaises(probe.ProbeBlocked):
            missing_pin.connect()
        missing_sni = probe._PinnedHTTPSConnection(
            "seller.example",
            pinned_addrs=[(PUBLIC_IP, 443)],
            server_hostname="",
            context=ctx,
        )
        missing_sni._server_hostname = ""
        with self.assertRaises(probe.ProbeBlocked):
            missing_sni.connect()

    def test_one_request_attaches_pin_without_rewriting_url(self):
        captured = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                captured["host"] = req.host
                captured["pinned"] = getattr(req, "pinned_addrs", None)
                captured["url"] = req.full_url
                raise urllib.error.URLError("stop")

        with patch(
            "live402.probe._checked_addrs", return_value=[(PUBLIC_IP, 443)]
        ), patch("live402.probe._opener", return_value=FakeOpener()):
            snap = probe._one_request(SELLER_URL, "GET")
        self.assertFalse(snap.get("live"))
        self.assertEqual(captured["host"], "seller.example")
        self.assertEqual(captured["pinned"], [(PUBLIC_IP, 443)])
        self.assertTrue(captured["url"].startswith("https://seller.example"))
        self.assertNotIn(PUBLIC_IP, captured["url"])

    def test_redirect_repins_next_hop(self):
        req = urllib.request.Request(SELLER_URL)
        req.ssrf_hops = 0
        handler = probe._SSRFRedirectHandler()
        with patch(
            "live402.probe._pin_https_target",
            return_value=("https://next.example/hop", [(PUBLIC_IP, 443)]),
        ):
            nxt = handler.redirect_request(
                req, None, 302, "Found", {}, "https://next.example/hop"
            )
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.ssrf_hops, 1)
        self.assertEqual(nxt.pinned_addrs, [(PUBLIC_IP, 443)])
        self.assertEqual(nxt.pinned_host, "next.example")


if __name__ == "__main__":
    unittest.main()
