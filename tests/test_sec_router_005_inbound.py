"""SEC-ROUTER-005 / A-07: inbound accept matching is strict.

Require client network, amount, and payTo. No omit-to-first-same-rail.
Inbound rail is rail_of_observed_network. Testnet is HTTP 402.
v1 amounts use maxAmountRequired. No live spend.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import discover, payment
from live402.route import handle_route
from tests.test_pay_replay import (
    _counting_facilitator,
    _headers_for,
    _payload,
    _weather_body,
)

ROUTE = discover.ROUTE
ROOT = Path(__file__).resolve().parents[1]
OTHER_BASE_PAYTO = "0x" + ("ab" * 20)


def _required(resource_url=ROUTE):
    return payment.payment_required(resource_url)


def _two_base_required():
    required = _required()
    required = dict(required)
    first = dict((required.get("accepts") or [])[0])
    other = dict(first)
    other["payTo"] = OTHER_BASE_PAYTO
    other["amount"] = "20000"
    rest = list((required.get("accepts") or [])[1:])
    required["accepts"] = [other, first] + rest
    return required


class InboundFieldRequiredTests(unittest.TestCase):
    def test_complete_v2_caip_matches(self):
        accept = payment.match_accept(_payload("ok"), _required())
        self.assertIsInstance(accept, dict)
        self.assertEqual(accept.get("network"), payment.BASE_CAIP2)
        self.assertEqual(str(accept.get("amount")), payment.AMOUNT_ATOMIC)

    def test_omit_network_does_not_first_rail_win(self):
        body = _payload("nn")
        body["accepted"] = dict(body["accepted"])
        body["accepted"].pop("network", None)
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_omit_amount_does_not_first_rail_win(self):
        body = _payload("na")
        body["accepted"] = dict(body["accepted"])
        body["accepted"].pop("amount", None)
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_omit_payto_does_not_first_rail_win(self):
        body = _payload("np")
        body["accepted"] = dict(body["accepted"])
        body["accepted"].pop("payTo", None)
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_v2_short_alias_is_not_observed_network(self):
        body = _payload("al")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["network"] = "base"
        self.assertIsNone(payment.rail_of_observed_network("base", 2))
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_missing_or_nonliteral_version_fails_closed(self):
        for bad in (None, "2", 2.0, True, 3):
            body = _payload("vv")
            if bad is None:
                body.pop("x402Version", None)
            else:
                body["x402Version"] = bad
            self.assertIsNone(payment.match_accept(body, _required()), bad)

    def test_conflicting_nested_version_fails_closed(self):
        body = _payload("cv")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["x402Version"] = 1
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_scheme_is_required_and_exact(self):
        for bad in (None, "up-to", "Exact", b"exact", True):
            body = _payload("sc")
            body["accepted"] = dict(body["accepted"])
            if bad is None:
                body["accepted"].pop("scheme", None)
            else:
                body["accepted"]["scheme"] = bad
            self.assertIsNone(payment.match_accept(body, _required()), bad)

    def test_bad_scheme_never_reaches_facilitator(self):
        body = _payload("sf")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["scheme"] = "up-to"
        verify_calls = []
        settle_calls = []
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            code, _result, _extra = handle_route(
                _weather_body(), _headers_for(body), ROUTE
            )
        self.assertEqual(code, 402)
        self.assertEqual(verify_calls, [])
        self.assertEqual(settle_calls, [])

    def test_asset_identity_is_required(self):
        body = _payload("ai")
        body["accepted"] = dict(body["accepted"])
        body["accepted"].pop("asset", None)
        body["accepted"].pop("currency", None)
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_facilitator_payload_rewrites_legacy_duplicates(self):
        body = _payload("fd")
        body["scheme"] = "up-to"
        body["network"] = "base"
        body["amount"] = "1"
        body["asset"] = "wrong"
        body["payTo"] = OTHER_BASE_PAYTO
        req = payment.official_requirements(_required()["accepts"][0])
        out = payment.normalize_payload_for_facilitator(body, req)
        for key in ("scheme", "network", "amount", "asset", "payTo"):
            self.assertEqual(out[key], req[key])
            self.assertEqual(out["accepted"][key], req[key])


class TwoSameRailAcceptTests(unittest.TestCase):
    def test_matches_second_same_rail_not_first(self):
        required = _two_base_required()
        first, second = required["accepts"][0], required["accepts"][1]
        self.assertEqual(payment.rail_of_observed_network(first.get("network"), 2), "base")
        self.assertEqual(payment.rail_of_observed_network(second.get("network"), 2), "base")
        self.assertEqual(first.get("payTo"), OTHER_BASE_PAYTO)
        self.assertEqual(second.get("payTo"), payment.DEFAULT_PAYTO)
        accept = payment.match_accept(_payload("sr"), required)
        self.assertIsInstance(accept, dict)
        self.assertEqual(accept.get("payTo"), payment.DEFAULT_PAYTO)
        self.assertEqual(str(accept.get("amount")), payment.AMOUNT_ATOMIC)
        self.assertIs(accept, second)

    def test_wrong_same_rail_amount_does_not_match_first(self):
        required = _two_base_required()
        body = _payload("wa")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["amount"] = "99999"
        self.assertIsNone(payment.match_accept(body, required))


class V1MaxAmountRequiredTests(unittest.TestCase):
    def _v1(self, nonce="v1", **fields):
        body = {
            "x402Version": 1,
            "resource": ROUTE,
            "scheme": "exact",
            "network": "base",
            "asset": payment.USDC_BASE,
            "maxAmountRequired": payment.AMOUNT_ATOMIC,
            "payTo": payment.DEFAULT_PAYTO,
        }
        body.update(fields)
        body["_nonce"] = nonce
        return body

    def test_v1_max_amount_required_matches(self):
        accept = payment.match_accept(self._v1(), _required())
        self.assertIsInstance(accept, dict)
        self.assertEqual(str(accept.get("amount")), payment.AMOUNT_ATOMIC)

    def test_v1_wrong_max_amount_required_fails(self):
        self.assertIsNone(
            payment.match_accept(self._v1(maxAmountRequired="20000"), _required())
        )

    def test_v1_omit_max_amount_required_fails(self):
        body = self._v1()
        body.pop("maxAmountRequired")
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_v1_amount_without_max_amount_required_fails(self):
        body = self._v1()
        body.pop("maxAmountRequired")
        body["amount"] = payment.AMOUNT_ATOMIC
        self.assertIsNone(payment.match_accept(body, _required()))

    def test_v1_conflicting_amount_fields_fail(self):
        self.assertIsNone(
            payment.match_accept(
                self._v1(amount="20000", maxAmountRequired=payment.AMOUNT_ATOMIC),
                _required(),
            )
        )


class InboundTestnet402Tests(unittest.TestCase):
    def test_match_accept_rejects_base_sepolia(self):
        body = _payload("tn")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["network"] = "eip155:84532"
        self.assertIsNone(payment.match_accept(body, _required()))
        self.assertTrue(payment.looks_like_testnet_network("eip155:84532"))
        self.assertEqual(
            payment.inbound_match_error(body),
            payment.INBOUND_TESTNET_ERROR,
        )

    def test_route_testnet_is_clear_402(self):
        body = _payload("t2")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["network"] = "eip155:84532"
        verify_calls = []
        settle_calls = []
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            code, result, _extra = handle_route(
                _weather_body(),
                _headers_for(body),
                ROUTE,
            )
        self.assertEqual(code, 402)
        self.assertEqual(result.get("error"), payment.INBOUND_TESTNET_ERROR)
        self.assertEqual(len(verify_calls), 0)
        self.assertEqual(len(settle_calls), 0)

    def test_named_sepolia_alias_is_402(self):
        body = _payload("t3")
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["network"] = "base-sepolia"
        self.assertIsNone(payment.match_accept(body, _required()))
        self.assertEqual(
            payment.inbound_match_error(body),
            payment.INBOUND_TESTNET_ERROR,
        )


class DocsSelectMatchedAcceptTests(unittest.TestCase):
    def test_discover_and_llms_do_not_say_sign_accepts_0(self):
        discover_src = (ROOT / "live402" / "discover.py").read_text(encoding="utf-8")
        self.assertNotIn("sign accepts[0]", discover_src.lower())
        self.assertNotIn("Sign accepts[0]", discover_src)
        self.assertIn("select the matched/observed accept", discover.LLMS_TXT)
        spec = discover.openapi_spec()
        curl = ((spec.get("x-examples") or {}).get("curl") or "")
        self.assertIn("Select the matched/observed accept", curl)
        self.assertNotIn("Sign accepts[0]", curl)


if __name__ == "__main__":
    unittest.main()
