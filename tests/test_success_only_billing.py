"""Success-only $0.003 economics and replay. All I/O is mocked."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from io import StringIO
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import discover, facilitator, mcp, payment, replay, server
from live402.route import _billable_winner, _paid_execute, handle_route

RESOURCE = "https://402signal.com/route"
TYPED_MISSES = (
    "no_candidates",
    "no_402_envelope",
    "no_payto",
    "reachable_200",
    "probe_timeout",
    "quote_expired",
    "upstream_5xx",
    "ssrf",
    "no_input_schema",
    "constraints_unmet",
    "probe_budget_exhausted",
    "probe_limit_reached",
    "unsafe_to_probe",
)


def _routing_accept(rail: str = "base") -> dict:
    accepts = payment.payment_required(RESOURCE)["accepts"]
    return next(row for row in accepts if payment.rail_of_accept(row) == rail)


def _seller_accept(
    rail: str = "base", *, amount: str = "10000", pay_to: str | None = None
) -> dict:
    if rail == "solana":
        network, asset, default = (
            payment.SOLANA_MAINNET,
            payment.USDC_SOLANA_MINT,
            payment.DEFAULT_PAYTO_SOLANA,
        )
    elif rail == "algorand":
        network, asset, default = (
            payment.ALGORAND_MAINNET,
            payment.USDC_ALGORAND_ASA,
            payment.DEFAULT_PAYTO_ALGORAND,
        )
    else:
        network, asset, default = (
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            payment.DEFAULT_PAYTO,
        )
    return {
        "scheme": "exact",
        "network": network,
        "asset": asset,
        "amount": amount,
        "payTo": pay_to or default,
        "maxTimeoutSeconds": 60,
    }


def _winner(rail: str = "base", *, amount: str = "10000") -> dict:
    accept = _seller_accept(rail, amount=amount)
    envelope = {"x402Version": 2, "accepts": [accept]}
    option = payment.validate_observed_accept(accept, envelope)
    assert option is not None
    return {
        "url": "https://seller.example/x402",
        "live": True,
        "payable": True,
        "invocable": True,
        "status": 402,
        "payTo": accept["payTo"],
        "envelope": envelope,
        "selected_payment": payment.selected_payment_fields(option),
        "batch_id": "winner-batch",
    }


def _miss(reason: str = "no_candidates") -> dict:
    return {
        "live": False,
        "payable": False,
        "invocable": False,
        "selected_payment": None,
        "miss_reason": reason,
        "batch_id": "miss-batch",
    }


def _verified() -> facilitator.FacilitatorResult:
    return facilitator.FacilitatorResult(ok=True, body={"isValid": True})


def _settled() -> facilitator.FacilitatorResult:
    return facilitator.FacilitatorResult(ok=True, body={"success": True})


def _payload(nonce: str = "55") -> dict:
    return {
        "x402Version": 2,
        "resource": {"url": RESOURCE},
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
                "nonce": "0x" + (nonce * 32),
            },
        },
    }


def _headers(payload: dict) -> dict:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return {"PAYMENT-SIGNATURE": base64.b64encode(raw).decode("ascii")}


class RequirementAndBoundaryTests(unittest.TestCase):
    def test_all_rails_advertise_symmetric_success_only_price(self):
        required = payment.payment_required(RESOURCE)
        self.assertEqual(len(required["accepts"]), 3)
        self.assertEqual({row["amount"] for row in required["accepts"]}, {"3000"})
        self.assertEqual(
            {row["extra"]["displayAmount"] for row in required["accepts"]},
            {"$0.003"},
        )
        algo = next(
            row
            for row in required["accepts"]
            if payment.rail_of_accept(row) == "algorand"
        )
        self.assertEqual(
            algo["extra"]["unsignedGroup"]["paymentTxn"]["amount"], 3000
        )
        self.assertEqual(required["amount"], "$0.003")
        self.assertEqual(required["billing"]["model"], "success_only_v1")
        self.assertFalse(required["billing"]["typed_misses_settled"])
        self.assertTrue(required["billing"]["seller_payment_separate"])

        route_responses = discover.openapi_spec()["paths"]["/route"]["post"]["responses"]
        for status in ("200", "503"):
            schema = route_responses[status]["content"]["application/json"]["schema"]
            self.assertIn("billing", schema["required"])
        self.assertIn("billing", mcp.OUTPUT_SCHEMA["required"])

    def test_billable_winner_requires_current_envelope_constraints_and_payto_policy(self):
        good = _winner()
        self.assertTrue(_billable_winner({}, 200, good))

        catalog_only = dict(good)
        catalog_only.pop("envelope")
        catalog_only["target"] = {"accepts": [_seller_accept()]}
        self.assertFalse(_billable_winner({}, 200, catalog_only))

        mismatched = _winner()
        mismatched["selected_payment"] = dict(mismatched["selected_payment"])
        mismatched["selected_payment"]["payTo"] = (
            "0x2222222222222222222222222222222222222222"
        )
        self.assertFalse(_billable_winner({}, 200, mismatched))

        wrong_facilitator = _winner()
        wrong_facilitator["selected_payment"] = dict(
            wrong_facilitator["selected_payment"]
        )
        wrong_facilitator["selected_payment"]["facilitator"] = (
            "https://attacker.example/facilitator"
        )
        self.assertFalse(_billable_winner({}, 200, wrong_facilitator))

        reachable_200 = _winner()
        reachable_200["status"] = 200
        self.assertFalse(_billable_winner({}, 200, reachable_200))

        missing_resource = _winner()
        missing_resource["url"] = None
        self.assertFalse(_billable_winner({}, 200, missing_resource))

        mixed_payto = _winner()
        mixed_payto["payTo"] = "0x2222222222222222222222222222222222222222"
        self.assertFalse(_billable_winner({}, 200, mixed_payto))

        too_expensive = _winner(amount="10000")
        self.assertFalse(
            _billable_winner({"max_price_usd": 0.005}, 200, too_expensive)
        )

        pending = _winner()
        pending["payTo_pending"] = True
        self.assertFalse(_billable_winner({}, 200, pending))
        self.assertTrue(
            _billable_winner({"accept_payTo_change": True}, 200, pending)
        )


class PaidExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_replay_db = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(
            self.tmp.name, "replay.sqlite"
        )
        replay.reset()
        self.parsed = {"x402Version": 2, "payload": {}}
        self.accept = _routing_accept()
        self.deadline = time.monotonic() + 60

    def tearDown(self):
        replay.reset()
        if self.previous_replay_db is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self.previous_replay_db
        self.tmp.cleanup()

    def _execute(self, body: dict | None = None):
        return _paid_execute(
            body or {"need": "weather"},
            self.parsed,
            self.accept,
            RESOURCE,
            None,
            self.deadline,
        )

    def test_success_verifies_probes_settles_marks_and_attaches_once(self):
        with patch("live402.facilitator.verify", return_value=_verified()) as verify, patch(
            "live402.route.run_probe", return_value=(200, _winner())
        ) as probe, patch("live402.facilitator.settle", return_value=_settled()) as settle, patch(
            "live402.history.mark_batch_settled"
        ) as mark, patch(
            "live402.route._attach_pq_trust", side_effect=lambda _c, result, _b: result
        ) as attach:
            code, body, extra = self._execute()
        self.assertEqual(code, 200)
        self.assertEqual(
            body["billing"],
            {
                "model": "success_only_v1",
                "condition": "live_eligible_route_found",
                "asset": "USDC",
                "amount_atomic": "3000",
                "display_amount": "$0.003",
                "rail": "base",
                "settlement_attempted": True,
                "settled": True,
            },
        )
        self.assertIn("PAYMENT-RESPONSE", extra)
        verify.assert_called_once()
        probe.assert_called_once()
        settle.assert_called_once()
        mark.assert_called_once_with("winner-batch")
        attach.assert_called_once()

    def test_every_typed_miss_is_free_terminal_candidate_without_pq(self):
        for reason in TYPED_MISSES:
            with self.subTest(reason=reason), patch(
                "live402.facilitator.verify", return_value=_verified()
            ) as verify, patch(
                "live402.route.run_probe", return_value=(503, _miss(reason))
            ) as probe, patch("live402.facilitator.settle") as settle, patch(
                "live402.history.mark_batch_settled"
            ) as mark, patch("live402.route._attach_pq_trust") as attach:
                code, body, extra = self._execute()
            self.assertEqual(code, 503)
            self.assertEqual(body["miss_reason"], reason)
            self.assertFalse(body["billing"]["settlement_attempted"])
            self.assertFalse(body["billing"]["settled"])
            self.assertIsNone(extra)
            verify.assert_called_once()
            probe.assert_called_once()
            settle.assert_not_called()
            mark.assert_not_called()
            attach.assert_not_called()

    def test_malformed_200_is_downgraded_without_economic_or_pq_action(self):
        cases = []
        no_envelope = _winner()
        no_envelope.pop("envelope")
        cases.append(no_envelope)
        wrong_selected = _winner()
        wrong_selected["selected_payment"] = dict(wrong_selected["selected_payment"])
        wrong_selected["selected_payment"]["amount_atomic"] = 9999
        cases.append(wrong_selected)
        string_live = _winner()
        string_live["live"] = "true"
        cases.append(string_live)
        wrong_route_code = _winner()
        cases.append(wrong_route_code)
        for index, malformed in enumerate(cases):
            with self.subTest(case=malformed), patch(
                "live402.facilitator.verify", return_value=_verified()
            ), patch(
                "live402.route.run_probe",
                return_value=(503 if index == len(cases) - 1 else 200, malformed),
            ), patch(
                "live402.facilitator.settle"
            ) as settle, patch("live402.history.mark_batch_settled") as mark, patch(
                "live402.route._attach_pq_trust"
            ) as attach:
                code, body, extra = self._execute()
            self.assertEqual(code, 503)
            self.assertFalse(body["live"])
            self.assertFalse(body["billing"]["settled"])
            self.assertIsNone(extra)
            settle.assert_not_called()
            mark.assert_not_called()
            attach.assert_not_called()

    def test_invalid_body_after_verify_can_retry_intentionally(self):
        headers = _headers(_payload("66"))
        with patch("live402.facilitator.verify", return_value=_verified()) as verify, patch(
            "live402.route.run_probe", return_value=(200, _winner())
        ) as probe, patch("live402.facilitator.settle", return_value=_settled()) as settle, patch(
            "live402.route._attach_pq_trust", side_effect=lambda _c, result, _b: result
        ):
            first = handle_route({}, headers, RESOURCE)
            second = handle_route({"need": "weather"}, headers, RESOURCE)
        self.assertEqual(first[0], 400)
        self.assertEqual(second[0], 200)
        self.assertEqual(verify.call_count, 2)
        probe.assert_called_once()
        settle.assert_called_once()

    def test_settlement_failure_never_promotes_history_or_pq_or_claims_settled(self):
        failed = facilitator.FacilitatorResult(ok=False, error="facilitator_unavailable")
        with patch("live402.facilitator.verify", return_value=_verified()), patch(
            "live402.route.run_probe", return_value=(200, _winner())
        ), patch("live402.facilitator.settle", return_value=failed), patch(
            "live402.history.mark_batch_settled"
        ) as mark, patch("live402.route._attach_pq_trust") as attach:
            code, body, _extra = self._execute()
        self.assertEqual(code, 402)
        self.assertNotEqual((body.get("billing") or {}).get("settled"), True)
        mark.assert_not_called()
        attach.assert_not_called()

    def test_transparency_failure_after_settlement_reports_paid_once(self):
        def unavailable(_code, result, _body):
            result = dict(result)
            result["pq_trust"] = {
                "transparency": {"status": "unavailable", "state": "unavailable"}
            }
            return result

        with patch("live402.facilitator.verify", return_value=_verified()), patch(
            "live402.route.run_probe", return_value=(200, _winner())
        ), patch("live402.facilitator.settle", return_value=_settled()) as settle, patch(
            "live402.route._attach_pq_trust", side_effect=unavailable
        ):
            code, body, extra = self._execute(
                {"need": "weather", "require_transparency": True}
            )
        self.assertEqual(code, 503)
        self.assertTrue(body["billing"]["settlement_attempted"])
        self.assertTrue(body["billing"]["settled"])
        self.assertIn("PAYMENT-RESPONSE", extra)
        settle.assert_called_once()


class FreeMissReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        replay.reset()
        self.headers = _headers(_payload("77"))

    def tearDown(self):
        replay.reset()
        if self.previous is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self.previous
        self.tmp.cleanup()

    def _run(self, reason="no_candidates"):
        with patch("live402.facilitator.verify", return_value=_verified()) as verify, patch(
            "live402.route.run_probe", return_value=(503, _miss(reason))
        ) as probe, patch("live402.facilitator.settle") as settle:
            out = handle_route({"need": "weather"}, self.headers, RESOURCE)
        return out, verify.call_count, probe.call_count, settle.call_count

    def _fingerprint(self):
        parsed = payment.extract_payment_payload(self.headers)
        accept = payment.match_accept(parsed, payment.payment_required(RESOURCE))
        return replay.canonical_fingerprint(parsed, accept)

    def test_sequential_and_restart_replay_identical_without_work(self):
        first, verifies, probes, settles = self._run()
        self.assertEqual((verifies, probes, settles), (1, 1, 0))
        fp = self._fingerprint()
        self.assertEqual(replay.ledger_state(fp), replay.STATE_NOT_SETTLED)
        replay.reset_memory()
        second, verifies, probes, settles = self._run()
        self.assertEqual((verifies, probes, settles), (0, 0, 0))
        self.assertEqual(first, second)

    def test_concurrent_duplicate_runs_one_verify_and_probe(self):
        started = threading.Event()
        release = threading.Event()
        verify_count = []
        probe_count = []

        def verify(*_args, **_kwargs):
            verify_count.append(1)
            return _verified()

        def probe(*_args, **_kwargs):
            probe_count.append(1)
            started.set()
            release.wait(timeout=2)
            return 503, _miss()

        results = []
        with patch("live402.facilitator.verify", side_effect=verify), patch(
            "live402.route.run_probe", side_effect=probe
        ), patch("live402.facilitator.settle") as settle:
            workers = [
                threading.Thread(
                    target=lambda: results.append(
                        handle_route({"need": "weather"}, self.headers, RESOURCE)
                    )
                )
                for _ in range(2)
            ]
            workers[0].start()
            self.assertTrue(started.wait(timeout=2))
            workers[1].start()
            release.set()
            for worker in workers:
                worker.join(timeout=3)
        self.assertEqual(len(verify_count), 1)
        self.assertEqual(len(probe_count), 1)
        settle.assert_not_called()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_every_typed_miss_finishes_durable_not_settled(self):
        for index, reason in enumerate(TYPED_MISSES, start=16):
            with self.subTest(reason=reason):
                replay.reset()
                self.headers = _headers(_payload(f"{index:02x}"))
                result, verifies, probes, settles = self._run(reason)
                self.assertEqual(result[0], 503)
                self.assertEqual((verifies, probes, settles), (1, 1, 0))
                self.assertEqual(
                    replay.ledger_state(self._fingerprint()),
                    replay.STATE_NOT_SETTLED,
                )

    def test_no_authorization_material_in_response_log_or_replay_row(self):
        secret = "0x" + ("ab" * 65)
        stream = StringIO()
        with patch("sys.stderr", stream):
            result, _v, _p, _s = self._run()
        conn = sqlite3.connect(os.environ["LIVE402_REPLAY_DB"])
        try:
            raw = conn.execute("SELECT outcome_json FROM settle_ledger").fetchone()[0]
        finally:
            conn.close()
        exposed = json.dumps(result, sort_keys=True) + stream.getvalue() + raw
        self.assertNotIn(secret, exposed)
        self.assertNotIn(self.headers["PAYMENT-SIGNATURE"], exposed)
        self.assertNotIn("authorization", result[1])


class CompatibilityAndAbuseControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        replay.reset()

    def tearDown(self):
        replay.reset()
        if self.previous is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self.previous
        self.tmp.cleanup()

    def test_legacy_503_without_billing_remains_settled(self):
        fp = "ab" * 32
        self.assertEqual(replay.begin(fp)[0], "run")
        replay.finish(fp, (503, {"live": False}, None), cache=True)
        self.assertEqual(replay.ledger_state(fp), replay.STATE_SETTLED)

    def test_invalid_billing_shape_cannot_claim_not_settled(self):
        fp = "cd" * 32
        self.assertEqual(replay.begin(fp)[0], "run")
        replay.finish(fp, (503, {"billing": {"settled": False}}, None), cache=True)
        self.assertEqual(replay.ledger_state(fp), replay.STATE_SETTLED)

    def test_http_200_cannot_claim_free_terminal_outcome(self):
        fp = "ef" * 32
        self.assertEqual(replay.begin(fp)[0], "run")
        billing = {
            "model": payment.ROUTING_BILLING_MODEL,
            "condition": payment.ROUTING_SETTLEMENT_CONDITION,
            "asset": "USDC",
            "amount_atomic": payment.AMOUNT_ATOMIC,
            "display_amount": payment.AMOUNT_USD,
            "rail": "base",
            "settlement_attempted": False,
            "settled": False,
        }
        replay.finish(
            fp, (200, {"live": True, "billing": billing}, None), cache=True
        )
        self.assertEqual(replay.ledger_state(fp), replay.STATE_SETTLED)

    def test_default_route_limit_is_twelve_and_override_remains(self):
        with patch.dict(os.environ, {"LIVE402_FIXTURE": ""}, clear=False):
            os.environ.pop("LIVE402_ROUTE_RPM", None)
            self.assertEqual(server.route_rpm(), 12)
            os.environ["LIVE402_ROUTE_RPM"] = "7"
            self.assertEqual(server.route_rpm(), 7)


if __name__ == "__main__":
    unittest.main()
