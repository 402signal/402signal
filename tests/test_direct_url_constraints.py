"""PR1 B: direct-url routing uses the same constraint engine."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import payment, probe, route, select


OBS_BASE = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CATALOG_SOL = payment.DEFAULT_PAYTO_SOLANA
CDP = "https://api.cdp.coinbase.com/platform/v2/x402"
PAYAI = "https://facilitator.payai.network"


def _base_accept(amount="20000", pay_to=OBS_BASE):
    return {
        "scheme": "exact",
        "network": payment.BASE_CAIP2,
        "asset": payment.USDC_BASE,
        "amount": amount,
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
        "extra": {"facilitator": CDP, "displayAmount": "$0.02"},
    }


def _catalog_claims_solana(url):
    return {
        "url": url,
        "description": "weather",
        "accepts": [
            _base_accept(),
            {
                "scheme": "exact",
                "network": payment.SOLANA_MAINNET,
                "asset": payment.USDC_SOLANA_MINT,
                "amount": "1000",
                "payTo": CATALOG_SOL,
                "maxTimeoutSeconds": 60,
                "extra": {"facilitator": PAYAI, "displayAmount": "$0.001"},
            },
        ],
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


def _observed_base_only(url, amount="20000", invocable=True, latency=10, n_7d=12, success=0.9):
    envelope = {"x402Version": 2, "accepts": [_base_accept(amount)]}
    result = {
        "url": url,
        "live": True,
        "status": 402,
        "has_402_challenge": True,
        "challenge_observed": True,
        "payTo": OBS_BASE,
        "latency_ms": latency,
        "envelope": envelope,
        "rail": "base",
        "amount": amount,
        "asset": payment.USDC_BASE,
        "history": {
            "success_7d": success,
            "n_7d": n_7d,
            "success_24h": None,
            "n_24h": 0,
            "p50_latency_ms": 20,
        },
    }
    item = _catalog_claims_solana(url)
    result = probe.attach_catalog_fields(result, item)
    result = probe.attach_invocable_target(result, item if invocable else None, envelope)
    if not invocable:
        result["invocable"] = False
    return result


class DirectUrlConstraintTests(unittest.TestCase):
    def _route(self, body, probed):
        url = body["url"]
        item = _catalog_claims_solana(url)
        with patch("live402.route.fixtures.lookup_url", return_value=item), patch(
            "live402.probe.probe_url", return_value=probed
        ):
            return route.run_probe(body)

    def test_networks_solana_vs_base_only_observation(self):
        url = "https://wx.example/direct-sol"
        probed = _observed_base_only(url)
        code, result = self._route({"url": url, "networks": ["solana"]}, probed)
        self.assertEqual(code, 503)
        self.assertFalse(result.get("live"))
        self.assertFalse(result.get("payable"))
        self.assertIsNone(result.get("selected_payment"))
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        claimed = (result.get("claimed") or {}).get("payment_options") or []
        self.assertTrue(any(o.get("rail") == "solana" for o in claimed))

    def test_max_price_usd(self):
        url = "https://wx.example/direct-price"
        probed = _observed_base_only(url, amount="20000")
        code, result = self._route({"url": url, "max_price_usd": 0.01}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        self.assertIsNone(result.get("selected_payment"))

    def test_max_amount_atomic(self):
        url = "https://wx.example/direct-amt"
        probed = _observed_base_only(url, amount="20000")
        code, result = self._route({"url": url, "max_amount_atomic": 5000}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")

    def test_require_invocable(self):
        url = "https://wx.example/direct-inv"
        probed = _observed_base_only(url, invocable=False)
        probed["invocable"] = False
        code, result = self._route({"url": url, "require_invocable": True}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")

    def test_min_reputation(self):
        url = "https://wx.example/direct-rep"
        probed = _observed_base_only(url, n_7d=0, success=None)
        code, result = self._route({"url": url, "min_reputation_score": 0.5}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")

    def test_max_total_cost(self):
        url = "https://wx.example/direct-cost"
        probed = _observed_base_only(url)
        code, result = self._route({"url": url, "max_total_cost_usd": 0.0001}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")

    def test_max_settlement_latency(self):
        url = "https://wx.example/direct-settle"
        probed = _observed_base_only(url)
        code, result = self._route({"url": url, "max_settlement_latency_ms": 1}, probed)
        self.assertEqual(code, 503)
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")

    def test_cheapest_objective_not_hardcoded_best(self):
        url = "https://wx.example/direct-cheap"
        envelope = {
            "x402Version": 2,
            "accepts": [
                _base_accept("20000"),
                {
                    "scheme": "exact",
                    "network": payment.SOLANA_MAINNET,
                    "asset": payment.USDC_SOLANA_MINT,
                    "amount": "1000",
                    "payTo": CATALOG_SOL,
                    "maxTimeoutSeconds": 60,
                    "extra": {"facilitator": PAYAI, "displayAmount": "$0.001"},
                },
            ],
        }
        probed = _observed_base_only(url)
        probed["envelope"] = envelope
        probed = probe.attach_invocable_target(probed, _catalog_claims_solana(url), envelope)
        code, result = self._route({"url": url, "objective": "cheapest"}, probed)
        self.assertEqual(code, 200)
        self.assertEqual(result.get("objective"), "cheapest")
        self.assertEqual(result["selected_payment"]["rail"], "solana")
        self.assertEqual(result["selected_payment"]["amount_atomic"], 1000)

    def test_malformed_payment_option_not_payable(self):
        url = "https://wx.example/direct-partial"
        envelope = {"x402Version": 2, "accepts": [{"network": payment.BASE_CAIP2, "payTo": "0xabc"}]}
        probed = {
            "url": url,
            "live": True,
            "status": 402,
            "has_402_challenge": True,
            "challenge_observed": True,
            "payTo": "0xabc",
            "envelope": envelope,
            "latency_ms": 8,
        }
        probed = probe.attach_invocable_target(probed, _catalog_claims_solana(url), envelope)
        code, result = self._route({"url": url}, probed)
        self.assertEqual(code, 503)
        self.assertTrue(result.get("challenge_observed") or probed.get("challenge_observed"))
        self.assertFalse(result.get("payable"))
        self.assertIsNone(result.get("selected_payment"))
        self.assertFalse(result.get("live"))

    def test_observed_disagrees_with_catalog_claim(self):
        url = "https://wx.example/direct-disagree"
        probed = _observed_base_only(url, amount="20000")
        code, result = self._route({"url": url, "objective": "cheapest"}, probed)
        self.assertEqual(code, 200)
        selected = result.get("selected_payment")
        self.assertEqual(selected["rail"], "base")
        self.assertEqual(selected["amount_atomic"], 20000)
        self.assertNotEqual(selected["amount_atomic"], 1000)
        self.assertNotEqual(selected["payTo"], CATALOG_SOL)
        claimed = (result.get("claimed") or {}).get("payment_options") or []
        self.assertTrue(any(o.get("amount_atomic") == 1000 for o in claimed))

    def test_requested_objective_never_hardcoded_best(self):
        url = "https://fixture.402signal.local/weather"
        code, result = route.run_probe({"url": url, "objective": "fastest"})
        self.assertEqual(code, 200)
        self.assertEqual(result.get("objective"), "fastest")
        self.assertIsNotNone(result.get("selected_payment"))


if __name__ == "__main__":
    unittest.main()
