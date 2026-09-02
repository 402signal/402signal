"""Payment authorization replay / work amplification. No raw payment material logged."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
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
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        os.environ.pop("LOCAL_FREE", None)

    def tearDown(self):
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)
        if self._prev_db is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self._prev_db
        self.tmp.cleanup()

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
        self.assertNotIn(replay.durable_hash(fp), logged)

    def test_restart_after_settle_does_not_settle_again(self):
        settle_calls = []

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(1)
                return 200, {"success": True}
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("rs"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        with patch("live402.facilitator.post_json", side_effect=fake_post):
            first = handle_route(body, headers, "https://402signal.com/route")
            replay.reset_memory()
            second = handle_route(body, headers, "https://402signal.com/route")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1].get("url"), second[1].get("url"))
        self.assertEqual(len(settle_calls), 1)

    def test_ttl_expiry_does_not_reopen_settle(self):
        """TTL drops the RAM cache only. Sqlite uniqueness does not expire."""
        settle_calls = []
        t = {"now": 10_000.0}

        def fake_mono():
            return t["now"]

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(1)
                return 200, {"success": True}
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("tl"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        accept = payment.match_accept(
            payment.extract_payment_payload(headers),
            payment.payment_required("https://402signal.com/route"),
        )
        fp = replay.canonical_fingerprint(payment.extract_payment_payload(headers), accept)
        with patch("live402.clock.monotonic", fake_mono), patch(
            "live402.facilitator.post_json", side_effect=fake_post
        ):
            first = handle_route(body, headers, "https://402signal.com/route")
            t["now"] += replay.COMPLETED_TTL_SECONDS + 1
            self.assertIsNone(replay.peek_completed(fp))
            second = handle_route(body, headers, "https://402signal.com/route")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(settle_calls), 1)

    def test_two_process_begin_duplicate_rejected(self):
        fp = "ab" * 32
        kind, _token = replay.begin(fp)
        self.assertEqual(kind, "run")
        db = os.environ["LIVE402_REPLAY_DB"]
        script = (
            "import os, sys\n"
            "os.environ['LIVE402_REPLAY_DB'] = %r\n"
            "from live402 import replay\n"
            "kind, token = replay.begin(%r)\n"
            "sys.stdout.write(kind)\n"
        ) % (db, fp)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), env.get("PYTHONPATH", "")]
        )
        out = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
        self.assertEqual(out.strip(), "reject")

    def test_ledger_stores_hash_not_fingerprint(self):
        headers = _headers_for(_payload("hs"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        accept = payment.match_accept(
            payment.extract_payment_payload(headers),
            payment.payment_required("https://402signal.com/route"),
        )
        fp = replay.canonical_fingerprint(payment.extract_payment_payload(headers), accept)
        with patch("live402.facilitator.post_json", side_effect=_fake_facilitator):
            handle_route(body, headers, "https://402signal.com/route")
        conn = sqlite3.connect(os.environ["LIVE402_REPLAY_DB"])
        try:
            rows = conn.execute(
                "SELECT fp_hash, state, outcome_json FROM settle_ledger"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        stored_hash, state, outcome = rows[0]
        self.assertEqual(stored_hash, hashlib.sha256(fp.encode("ascii")).hexdigest())
        self.assertNotEqual(stored_hash, fp)
        self.assertEqual(state, replay.STATE_SETTLED)
        self.assertNotIn(fp, outcome)
        self.assertNotIn("PAYMENT-SIGNATURE", outcome)
        self.assertIn("UNIQUE", replay._SCHEMA)

    def test_pending_and_unknown_are_non_terminal_no_second_settle(self):
        pending_fp = "cd" * 32
        self.assertEqual(replay.begin(pending_fp)[0], "run")
        self.assertEqual(replay.ledger_state(pending_fp), replay.STATE_PENDING)
        self.assertIn(replay.ledger_state(pending_fp), replay.NON_TERMINAL_STATES)
        replay.reset_memory()
        self.assertEqual(replay.begin(pending_fp)[0], "reject")
        self.assertEqual(replay.ledger_state(pending_fp), replay.STATE_PENDING)

        unknown_fp = "ef" * 32
        self.assertEqual(replay.begin(unknown_fp)[0], "run")
        replay.abandon(unknown_fp)
        self.assertEqual(replay.ledger_state(unknown_fp), replay.STATE_UNKNOWN)
        self.assertIn(replay.ledger_state(unknown_fp), replay.NON_TERMINAL_STATES)
        replay.reset_memory()
        self.assertEqual(replay.begin(unknown_fp)[0], "reject")
        self.assertEqual(replay.ledger_state(unknown_fp), replay.STATE_UNKNOWN)

    def test_pending_row_blocks_route_settle(self):
        settle_calls = []

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(1)
                return 200, {"success": True}
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("pd"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        accept = payment.match_accept(
            payment.extract_payment_payload(headers),
            payment.payment_required("https://402signal.com/route"),
        )
        fp = replay.canonical_fingerprint(payment.extract_payment_payload(headers), accept)
        self.assertEqual(replay.begin(fp)[0], "run")
        replay.reset_memory()
        with patch("live402.facilitator.post_json", side_effect=fake_post):
            code, _result, _extra = handle_route(
                body, headers, "https://402signal.com/route"
            )
        self.assertEqual(code, 402)
        self.assertEqual(len(settle_calls), 0)
        self.assertEqual(replay.ledger_state(fp), replay.STATE_PENDING)


if __name__ == "__main__":
    unittest.main()
