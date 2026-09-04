"""Payment authorization replay / work amplification. No raw payment material logged.

SEC-TEST-003: mocked-facilitator state-machine coverage beyond concurrent identical
auth and sequential replay. No live spend.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
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

from live402 import algo_tx, discover, facilitator, payment, replay
from live402.route import handle_route


def _payload(nonce="11", resource_url="https://402signal.com/route"):
    nonce_hex = hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()
    body = {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
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
                "nonce": "0x" + nonce_hex,
            },
        },
    }
    if resource_url:
        body["resource"] = {"url": resource_url}
    return body


class _Headers(dict):
    def get(self, key, default=None):
        for name, val in self.items():
            if str(name).lower() == str(key).lower():
                return val
        return default


def _headers_for(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _Headers({"PAYMENT-SIGNATURE": base64.b64encode(raw).decode("ascii")})


def _weather_body():
    return {"need": "weather", "url": "https://fixture.402signal.local/weather"}


def _decode_payment_header(raw):
    if not raw:
        return None
    blob = base64.b64decode(str(raw).encode("ascii"), validate=False)
    return json.loads(blob.decode("utf-8"))


def _settlement_receipt(**extra):
    receipt = {
        "success": True,
        "transaction": "0x" + ("cd" * 32),
        "network": payment.BASE_CAIP2,
        "payer": "0x1111111111111111111111111111111111111111",
    }
    receipt.update(extra)
    return receipt


def _fake_facilitator(url, body, headers=None, timeout=20.0):
    _ = headers, timeout, body
    if str(url).rstrip("/").endswith("/verify"):
        return 200, {"isValid": True}
    if str(url).rstrip("/").endswith("/settle"):
        return 200, _settlement_receipt()
    return 404, {"error": "unexpected"}


def _counting_facilitator(verify_calls, settle_calls, settle_response=None):
    """Mock facilitator.post_json. settle_response None uses a successful settle."""

    def fake_post(url, body, headers=None, timeout=20.0):
        _ = headers, timeout, body
        if str(url).rstrip("/").endswith("/verify"):
            verify_calls.append(url)
            return 200, {"isValid": True}
        if str(url).rstrip("/").endswith("/settle"):
            settle_calls.append(url)
            if settle_response is not None:
                return settle_response
            return 200, _settlement_receipt()
        return 404, {"error": "unexpected"}

    return fake_post


class ReplayFingerprintTests(unittest.TestCase):
    @staticmethod
    def _accept(rail):
        return next(
            row
            for row in payment.payment_required(discover.ROUTE)["accepts"]
            if payment.rail_of_accept(row) == rail
        )

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

    def test_base_wrapper_signature_and_numeric_spelling_are_semantic_replay(self):
        accept = self._accept("base")
        original = _payload("economic-base")
        variant = copy.deepcopy(original)
        variant["resource"]["description"] = "unsigned metadata changed"
        variant["extensions"] = {"attacker": {"marker": "not signed"}}
        variant["payload"]["signature"] = "0x" + ("ef" * 65)
        variant["payload"]["authorization"]["from"] = (
            variant["payload"]["authorization"]["from"].upper().replace("0X", "0x")
        )
        variant["payload"]["authorization"]["value"] = "0003000"
        variant["payload"]["authorization"]["validAfter"] = "000"
        self.assertEqual(
            replay.canonical_fingerprint(original, accept),
            replay.canonical_fingerprint(variant, accept),
        )
        changed = copy.deepcopy(original)
        changed["payload"]["authorization"]["nonce"] = "0x" + ("01" * 32)
        self.assertNotEqual(
            replay.canonical_fingerprint(original, accept),
            replay.canonical_fingerprint(changed, accept),
        )

    def test_base_permit2_binds_authorization_not_signature_or_wrapper(self):
        accept = self._accept("base")
        payload = _payload("unused")
        payload["payload"] = {
            "signature": "0x" + ("ab" * 65),
            "permit2Authorization": {
                "permitted": {"token": payment.USDC_BASE, "amount": "3000"},
                "from": "0x1111111111111111111111111111111111111111",
                "spender": "0x402085c248EeA27D92E8b30b2C58ed07f9E20001",
                "nonce": "1234",
                "deadline": "9999999999",
                "witness": {"to": payment.DEFAULT_PAYTO, "validAfter": "0"},
            },
        }
        variant = copy.deepcopy(payload)
        variant["payload"]["signature"] = "0x" + ("cd" * 65)
        variant["resource"]["description"] = "unsigned"
        self.assertEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(variant, accept),
        )
        variant["payload"]["permit2Authorization"]["deadline"] = "9999999998"
        self.assertNotEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(variant, accept),
        )

    def test_solana_binds_message_not_signatures_metadata_or_base64_padding(self):
        accept = self._accept("solana")
        message = b"\x80\x00economic-solana-message"
        first = b"\x01" + (b"A" * 64) + message
        second = b"\x01" + (b"B" * 64) + message
        payload = {
            "x402Version": 2,
            "resource": {"url": discover.ROUTE, "description": "one"},
            "payload": {"transaction": base64.b64encode(first).decode("ascii")},
        }
        variant = copy.deepcopy(payload)
        variant["resource"]["description"] = "two"
        variant["payload"]["transaction"] = base64.b64encode(second).decode("ascii").rstrip("=")
        self.assertEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(variant, accept),
        )
        changed = copy.deepcopy(payload)
        changed_raw = b"\x01" + (b"A" * 64) + message + b"!"
        changed["payload"]["transaction"] = base64.b64encode(changed_raw).decode("ascii")
        self.assertNotEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(changed, accept),
        )

    def test_algorand_binds_unsigned_group_and_index_not_signature_wrapper(self):
        accept = self._accept("algorand")
        txn = {
            "aamt": 3000,
            "arcv": b"R" * 32,
            "fv": 1,
            "gh": b"G" * 32,
            "lv": 100,
            "snd": b"S" * 32,
            "type": "axfer",
            "xaid": int(payment.USDC_ALGORAND_ASA),
        }

        def encoded(sig, value):
            raw = algo_tx.msgpack_encode({"sig": sig, "txn": value})
            return base64.b64encode(raw).decode("ascii")

        payload = {
            "x402Version": 2,
            "resource": {"url": discover.ROUTE},
            "payload": {"paymentIndex": 0, "paymentGroup": [encoded(b"A" * 64, txn)]},
        }
        variant = copy.deepcopy(payload)
        variant["resource"]["description"] = "unsigned"
        variant["payload"]["paymentGroup"] = [encoded(b"B" * 64, txn).rstrip("=")]
        self.assertEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(variant, accept),
        )
        changed_txn = dict(txn, aamt=3001)
        changed = copy.deepcopy(payload)
        changed["payload"]["paymentGroup"] = [encoded(b"A" * 64, changed_txn)]
        self.assertNotEqual(
            replay.canonical_fingerprint(payload, accept),
            replay.canonical_fingerprint(changed, accept),
        )

    def test_invalid_rail_authorizations_fail_closed(self):
        with self.assertRaises(ValueError):
            replay.canonical_fingerprint(
                {"payload": {"authorization": {"nonce": "0x00"}}},
                self._accept("base"),
            )
        with self.assertRaises(ValueError):
            replay.canonical_fingerprint(
                {"payload": {"transaction": base64.b64encode(b"\x00bad").decode()}},
                self._accept("solana"),
            )
        with self.assertRaises(ValueError):
            replay.canonical_fingerprint(
                {"payload": {"paymentIndex": 0, "paymentGroup": ["bm90LW1zZ3BhY2s="]}},
                self._accept("algorand"),
            )


class ReplayDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        replay.reset()

    def tearDown(self):
        replay.reset()
        os.environ.pop(replay.CUTOVER_ACK_ENV, None)
        if self._prev_db is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self._prev_db
        self.tmp.cleanup()

    def test_wal_uses_full_synchronous_and_is_ready(self):
        self.assertTrue(replay.durable_ready())
        conn = replay._connect()
        self.assertEqual(int(conn.execute("PRAGMA synchronous").fetchone()[0]), 2)
        self.assertEqual(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")

    def test_production_refuses_non_volume_replay_path(self):
        with patch.dict(os.environ, {"LIVE402_FIXTURE": ""}, clear=False):
            self.assertFalse(replay.durable_ready())

    def test_legacy_hashes_replay_exact_but_block_new_identity_until_safe_cutover(self):
        payload = _payload("legacy")
        accept = next(
            row
            for row in payment.payment_required(discover.ROUTE)["accepts"]
            if payment.rail_of_accept(row) == "base"
        )
        legacy_fp = replay.legacy_fingerprint(payload, accept)
        old_outcome = {
            "c": 503,
            "b": {
                "live": False,
                "miss_reason": "no_candidates",
                "paymentPayload": {
                    "signature": "LEGACY-SECRET-CANARY",
                    "authorization": {"nonce": "LEGACY-SECRET-CANARY"},
                },
                "facilitator_response": {"errorReason": "LEGACY-SECRET-CANARY"},
                "billing": {
                    "model": payment.ROUTING_BILLING_MODEL,
                    "condition": payment.ROUTING_SETTLEMENT_CONDITION,
                    "asset": "USDC",
                    "amount_atomic": payment.AMOUNT_ATOMIC,
                    "display_amount": payment.AMOUNT_USD,
                    "rail": "base",
                    "settlement_attempted": False,
                    "settled": False,
                },
            },
            "e": {
                "PAYMENT-RESPONSE": payment.payment_response_header(
                    _settlement_receipt(
                        errorReason="LEGACY-SECRET-CANARY",
                        paymentPayload={"signature": "LEGACY-SECRET-CANARY"},
                    )
                )
            },
        }
        conn = sqlite3.connect(os.environ["LIVE402_REPLAY_DB"])
        conn.executescript(
            """
            CREATE TABLE settle_ledger (
                fp_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                outcome_json TEXT,
                created_at REAL NOT NULL,
                CONSTRAINT settle_fp_hash_unique UNIQUE (fp_hash)
            );
            """
        )
        conn.execute(
            "INSERT INTO settle_ledger (fp_hash,state,outcome_json,created_at) VALUES (?,?,?,?)",
            (
                replay.durable_hash(legacy_fp),
                replay.STATE_NOT_SETTLED,
                json.dumps(old_outcome),
                1.0,
            ),
        )
        conn.commit()
        conn.close()

        semantic_fp = replay.canonical_fingerprint(payload, accept)
        kind, exact = replay.begin(semantic_fp, legacy_fp=legacy_fp)
        self.assertEqual(kind, "cached")
        self.assertEqual(exact[1]["miss_reason"], "no_candidates")
        self.assertNotIn("LEGACY-SECRET-CANARY", json.dumps(exact))
        self.assertIsNone(exact[2])
        self.assertFalse(replay.durable_ready())

        variant = copy.deepcopy(payload)
        variant["resource"]["description"] = "legacy hash cannot reconstruct this"
        self.assertEqual(
            replay.canonical_fingerprint(variant, accept), semantic_fp
        )
        self.assertNotEqual(replay.legacy_fingerprint(variant, accept), legacy_fp)
        self.assertEqual(
            replay.begin(
                semantic_fp,
                legacy_fp=replay.legacy_fingerprint(variant, accept),
            )[0],
            "reject",
        )

        os.environ[replay.CUTOVER_ACK_ENV] = replay.CUTOVER_ACK_VALUE
        replay.reset_memory()
        self.assertTrue(replay.durable_ready())
        self.assertEqual(
            replay.begin(
                semantic_fp,
                legacy_fp=replay.legacy_fingerprint(variant, accept),
            )[0],
            "run",
        )
        replay.abandon(semantic_fp)


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

    def test_concurrent_semantic_auth_variants_one_probe_one_settle(self):
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
                return 200, _settlement_receipt()
            return 404, {"error": "unexpected"}

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            probe_calls.append(url)
            probe_started.set()
            release_probe.wait(timeout=2)
            seller_accept = {
                "scheme": "exact",
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": "10000",
                "payTo": payment.DEFAULT_PAYTO,
                "maxTimeoutSeconds": 60,
            }
            envelope = {"x402Version": 2, "accepts": [seller_accept]}
            return {
                "url": url,
                "live": True,
                "status": 402,
                "has_402_challenge": True,
                "payable": True,
                "invocable": True,
                "payTo": payment.DEFAULT_PAYTO,
                "envelope": envelope,
                "selected_payment": payment.selected_payment_fields(
                    payment.validate_observed_accept(seller_accept, envelope)
                ),
            }

        original = _payload("cc")
        variant = copy.deepcopy(original)
        variant["resource"]["description"] = "unsigned concurrent mutation"
        variant["payload"]["signature"] = "0x" + ("ef" * 65)
        request_headers = [_headers_for(original), _headers_for(variant)]
        body = _weather_body()
        results = []

        def worker(headers):
            results.append(
                handle_route(body, headers, "https://402signal.com/route")
            )

        with patch("live402.facilitator.post_json", side_effect=fake_post), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ), patch("live402.probe.route_need") as route_need:
            t1 = threading.Thread(target=worker, args=(request_headers[0],))
            t2 = threading.Thread(target=worker, args=(request_headers[1],))
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
                return 200, _settlement_receipt()
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("dd"))
        body = _weather_body()
        with patch("live402.facilitator.post_json", side_effect=fake_post):
            first = handle_route(body, headers, "https://402signal.com/route")
            second = handle_route(body, headers, "https://402signal.com/route")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(settle_calls), 1)

    def test_fingerprint_not_logged(self):
        headers = _headers_for(_payload("ee"))
        body = _weather_body()
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
                return 200, _settlement_receipt()
            return 404, {"error": "unexpected"}

        original = _payload("rs")
        variant = copy.deepcopy(original)
        variant["resource"]["description"] = "unsigned restart mutation"
        variant["payload"]["signature"] = "0x" + ("ef" * 65)
        headers = _headers_for(original)
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        with patch("live402.facilitator.post_json", side_effect=fake_post):
            first = handle_route(body, headers, "https://402signal.com/route")
            replay.reset_memory()
            second = handle_route(
                body, _headers_for(variant), "https://402signal.com/route"
            )
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
                return 200, _settlement_receipt()
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
                return 200, _settlement_receipt()
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
        self.assertEqual(code, 503)
        self.assertEqual(len(settle_calls), 0)
        self.assertEqual(replay.ledger_state(fp), replay.STATE_PENDING)


class StateMachineReplayTests(unittest.TestCase):
    """SEC-TEST-003: settlement_pending, lost settle, resource mismatch, related B–H cases."""

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

    def test_settlement_pending_is_non_terminal_no_second_settle(self):
        """x402 settlement_pending is not success. Retry must not settle again."""
        verify_calls = []
        settle_calls = []
        pending = (
            200,
            {
                "success": False,
                "errorReason": "settlement_pending",
                "transaction": "0x" + ("cd" * 32),
                "network": "eip155:8453",
            },
        )
        headers = _headers_for(_payload("sp"))
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls, pending),
        ):
            first = handle_route(body, headers, discover.ROUTE)
            second = handle_route(body, headers, discover.ROUTE)
        self.assertEqual(first[0], 503)
        self.assertEqual(second[0], 503)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertIsNone(first[2])
        self.assertEqual(first, second)
        self.assertEqual(first[1]["miss_reason"], "settlement_unknown")
        self.assertEqual(first[1]["billing"]["settlement_state"], "unknown")
        self.assertTrue(first[1]["billing"]["settlement_attempted"])
        self.assertIsNone(first[1]["billing"]["settled"])
        self.assertNotIn("settlement_pending", json.dumps(first))
        self.assertNotEqual(first[1].get("live"), True)

    def test_lost_facilitator_settle_response_idempotent_retry(self):
        """Crash-after-broadcast / lost settle response: retry must not double settle."""
        verify_calls = []
        settle_calls = []
        lost = (None, {"error": "facilitator_unavailable"})
        headers = _headers_for(_payload("lf"))
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls, lost),
        ):
            first = handle_route(body, headers, discover.ROUTE)
            second = handle_route(body, headers, discover.ROUTE)
        self.assertEqual(first[0], 503)
        self.assertEqual(second[0], 503)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[1]["billing"]["settlement_state"], "unknown")
        self.assertIsNone(first[1]["billing"]["settled"])
        self.assertNotEqual(first[1].get("live"), True)

    def test_lost_settle_outcome_write_failure_still_blocks_after_restart(self):
        verify_calls = []
        settle_calls = []
        headers = _headers_for(_payload("lost-disk"))
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(
                verify_calls,
                settle_calls,
                (None, {"error": "FACILITATOR-SECRET-CANARY"}),
            ),
        ), patch("live402.replay._ledger_finish", return_value=None):
            first = handle_route(_weather_body(), headers, discover.ROUTE)
            replay.reset_memory()
            second = handle_route(_weather_body(), headers, discover.ROUTE)
        self.assertEqual(first[0], 503)
        self.assertEqual(second[0], 503)
        self.assertEqual(first[1]["billing"]["settlement_state"], "unknown")
        self.assertEqual(second[1]["billing"]["settlement_state"], "unknown")
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(len(settle_calls), 1)
        self.assertNotIn("FACILITATOR-SECRET-CANARY", json.dumps((first, second)))

    def test_free_miss_outcome_write_failure_fails_closed_after_restart(self):
        headers = _headers_for(_payload("free-disk"))
        with patch("live402.facilitator.verify", return_value=facilitator.FacilitatorResult(ok=True, body={"isValid": True})) as verify, patch(
            "live402.route.run_probe",
            return_value=(
                503,
                {
                    "live": False,
                    "payable": False,
                    "invocable": False,
                    "selected_payment": None,
                    "miss_reason": "no_candidates",
                },
            ),
        ) as probe, patch("live402.facilitator.settle") as settle, patch(
            "live402.replay._ledger_finish", return_value=None
        ):
            first = handle_route(_weather_body(), headers, discover.ROUTE)
            replay.reset_memory()
            second = handle_route(_weather_body(), headers, discover.ROUTE)
        self.assertEqual(first[1]["billing"]["settlement_state"], "not_attempted")
        self.assertEqual(second[1]["billing"]["settlement_state"], "unknown")
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(probe.call_count, 1)
        settle.assert_not_called()

    def test_reservation_storage_failure_performs_no_work(self):
        headers = _headers_for(_payload("reserve-disk"))
        with patch("live402.replay._ledger_reserve", return_value="reject"), patch(
            "live402.facilitator.verify"
        ) as verify, patch("live402.route.run_probe") as probe, patch(
            "live402.facilitator.settle"
        ) as settle:
            result = handle_route(_weather_body(), headers, discover.ROUTE)
        self.assertEqual(result[0], 503)
        self.assertEqual(result[1]["billing"]["settlement_state"], "unknown")
        self.assertIsNone(result[1]["billing"]["settlement_attempted"])
        verify.assert_not_called()
        probe.assert_not_called()
        settle.assert_not_called()

    def test_same_auth_different_resource_does_not_settle(self):
        """SEC-ROUTER-002: /route payment reused on /mcp is 402; no second settle."""
        verify_calls = []
        settle_calls = []
        headers = _headers_for(_payload("rs", resource_url=discover.ROUTE))
        body = _weather_body()
        mcp_resource = discover.ORIGIN + "/mcp"
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            first = handle_route(body, headers, discover.ROUTE)
            second = handle_route(body, headers, mcp_resource, bazaar=payment.BAZAAR_MCP)
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 402)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertNotEqual(second[1].get("live"), True)
        self.assertNotEqual(mcp_resource, discover.ROUTE)

    def test_mutated_unsigned_resource_cannot_replay_cross_endpoint_result(self):
        """Economic identity stays one-use while cached output remains endpoint-scoped."""
        verify_calls = []
        settle_calls = []
        original = _payload("resource-scope", resource_url=discover.ROUTE)
        changed = copy.deepcopy(original)
        mcp_resource = discover.ORIGIN + "/mcp"
        changed["resource"]["url"] = mcp_resource
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            first = handle_route(body, _headers_for(original), discover.ROUTE)
            second = handle_route(
                body,
                _headers_for(changed),
                mcp_resource,
                bazaar=payment.BAZAAR_MCP,
            )
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 503)
        self.assertEqual(second[1]["billing"]["settlement_state"], "unknown")
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(len(settle_calls), 1)

    def test_concurrent_identical_auth_holds_during_settle(self):
        """In-flight settle is non-terminal: waiter must not issue a second settle."""
        settle_started = threading.Event()
        release_settle = threading.Event()
        verify_calls = []
        settle_calls = []

        def fake_post(url, body, headers=None, timeout=20.0):
            _ = headers, timeout, body
            if str(url).rstrip("/").endswith("/verify"):
                verify_calls.append(url)
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                settle_calls.append(url)
                settle_started.set()
                release_settle.wait(timeout=2)
                return 200, _settlement_receipt()
            return 404, {"error": "unexpected"}

        headers = _headers_for(_payload("cs"))
        body = _weather_body()
        results = []

        def worker():
            results.append(handle_route(body, headers, discover.ROUTE))

        with patch("live402.facilitator.post_json", side_effect=fake_post):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            self.assertTrue(settle_started.wait(timeout=2))
            t2.start()
            release_settle.set()
            t1.join(timeout=3)
            t2.join(timeout=3)

        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], results[1][0])
        self.assertIn(results[0][0], (200, 503))

    def test_failed_verify_is_cached_and_never_settles(self):
        """Failed verify is a cached 402. Retry must not verify or settle again."""
        verify_calls = []
        settle_calls = []

        def reject(url, body, headers=None, timeout=20.0):
            _ = body, headers, timeout
            if str(url).rstrip("/").endswith("/verify"):
                verify_calls.append(url)
                return 200, {"isValid": False, "invalidReason": "invalid_payload"}
            settle_calls.append(url)
            raise AssertionError("settle must not run after failed verify")

        headers = _headers_for(_payload("fv"))
        body = _weather_body()
        with patch("live402.facilitator.post_json", side_effect=reject), patch(
            "live402.probe.probe_url"
        ) as mock_url:
            first = handle_route(body, headers, discover.ROUTE)
            second = handle_route(body, headers, discover.ROUTE)
        self.assertEqual(first[0], 402)
        self.assertEqual(second[0], 402)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(len(settle_calls), 0)
        mock_url.assert_not_called()

    def test_terminal_settle_failure_does_not_settle_again(self):
        """Terminal facilitator settle failure is cached. Retry must not settle again."""
        verify_calls = []
        settle_calls = []
        failed = (200, {"success": False, "errorReason": "unexpected_settle_error"})
        headers = _headers_for(_payload("tf"))
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls, failed),
        ):
            first = handle_route(body, headers, discover.ROUTE)
            second = handle_route(body, headers, discover.ROUTE)
        self.assertEqual(first[0], 402)
        self.assertEqual(second[0], 402)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertNotIn("unexpected_settle_error", json.dumps((first, second)))
        self.assertNotIn("PAYMENT-RESPONSE", first[2] or {})
        self.assertEqual(first[1]["billing"]["settlement_state"], "rejected")

    def test_empty_body_400_is_not_cached_valid_retry_settles_once(self):
        """400 body errors are not replay-cached. A later valid body may settle once."""
        verify_calls = []
        settle_calls = []
        headers = _headers_for(_payload("eb"))
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            bad = handle_route({}, headers, discover.ROUTE)
            good = handle_route(_weather_body(), headers, discover.ROUTE)
            replayed = handle_route(_weather_body(), headers, discover.ROUTE)
        self.assertEqual(bad[0], 400)
        self.assertEqual(good[0], 200)
        self.assertEqual(replayed[0], 200)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 2)

    def test_nested_constraints_400_skips_settle_and_valid_retry_can_run(self):
        """A plausible but unsupported policy shape must not become unconstrained."""
        verify_calls = []
        settle_calls = []
        headers = _headers_for(_payload("nc"))
        bad_body = {
            **_weather_body(),
            "constraints": {"max_price_usd": 0.01, "networks": ["base"]},
        }
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            bad = handle_route(bad_body, headers, discover.ROUTE)
            good = handle_route(
                {**_weather_body(), "max_price_usd": 0.01, "networks": ["base"]},
                headers,
                discover.ROUTE,
            )
            replayed = handle_route(
                {**_weather_body(), "max_price_usd": 0.01, "networks": ["base"]},
                headers,
                discover.ROUTE,
            )
        self.assertEqual(bad[0], 400)
        self.assertEqual(
            bad[1].get("error"),
            "constraints must be specified as top-level fields",
        )
        self.assertEqual(good[0], 200)
        self.assertEqual(replayed[0], 200)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 2)

    def test_distinct_nonces_each_settle_once(self):
        """Different authorization nonces are different fingerprints (no cache cross-talk)."""
        verify_calls = []
        settle_calls = []
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            first = handle_route(body, _headers_for(_payload("n1")), discover.ROUTE)
            second = handle_route(body, _headers_for(_payload("n2")), discover.ROUTE)
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(settle_calls), 2)
        self.assertEqual(len(verify_calls), 2)


if __name__ == "__main__":
    unittest.main()
