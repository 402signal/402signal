"""PR16 rail economics + new objectives/constraints. No network."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import economics, payment, select


def _hist(success_7d=None, n_7d=0):
    return {
        "success_7d": success_7d,
        "n_7d": n_7d,
        "n_24h": 0,
        "success_24h": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
    }


def _hit(url, rail="base", amount=10000, latency=10, history=None):
    if rail == "solana":
        network, asset = payment.SOLANA_MAINNET, payment.USDC_SOLANA_MINT
        pay_to = payment.DEFAULT_PAYTO_SOLANA
    elif rail == "algorand":
        network, asset = payment.ALGORAND_MAINNET, payment.ALGORAND_MAINNET and payment.USDC_ALGORAND_ASA
        pay_to = payment.DEFAULT_PAYTO_ALGORAND
    else:
        network, asset = payment.BASE_CAIP2, payment.USDC_BASE
        pay_to = "0xabc"
    return {
        "url": url,
        "rail": rail,
        "live": True,
        "invocable": True,
        "payTo": pay_to,
        "latency_ms": latency,
        "amount": amount,
        "asset": asset,
        "history": history if history is not None else _hist(n_7d=12, success_7d=0.9),
        "accepts": [
            {
                "network": network,
                "asset": asset,
                "payTo": pay_to,
                "amount": amount,
            }
        ],
    }


class ProvenanceTests(unittest.TestCase):
    def test_same_keys_on_every_rail(self):
        keys = None
        for rail in ("base", "solana", "algorand"):
            eco = economics.for_result(_hit("https://%s.example/x" % rail, rail=rail))
            self.assertEqual(eco["rail"], rail)
            self.assertIn(eco["merchant_price_usd"]["provenance"], ("402signal_observed", "unknown"))
            self.assertEqual(eco["merchant_price_usd"]["value"], 0.01)
            self.assertEqual(eco["chain_fee_usd"]["provenance"], "unknown")
            self.assertIsNone(eco["chain_fee_usd"]["value"])
            self.assertEqual(eco["facilitator_fee_usd"]["provenance"], "unknown")
            self.assertEqual(eco["total_cost_usd"]["provenance"], "unknown")
            self.assertIsNone(eco["total_cost_usd"]["value"])
            self.assertEqual(eco["settlement_latency_ms"]["provenance"], "unknown")
            self.assertIsNone(eco["settlement_latency_ms"]["value"])
            if keys is None:
                keys = set(eco)
            else:
                self.assertEqual(set(eco), keys)

    def test_base_and_algorand_finality_are_cited(self):
        base = economics.for_result(_hit("https://base.example/x", rail="base"))
        algo = economics.for_result(_hit("https://algo.example/x", rail="algorand"))
        sol = economics.for_result(_hit("https://sol.example/x", rail="solana"))
        self.assertEqual(base["finality_ms"]["provenance"], "protocol_reference")
        self.assertEqual(base["finality_ms"]["value"], 2000)
        self.assertIn("docs.base.org", base["finality_ms"]["citation"])
        self.assertEqual(algo["finality_ms"]["provenance"], "protocol_reference")
        self.assertEqual(algo["finality_ms"]["value"], 2820)
        self.assertIn("dev.algorand.co", algo["finality_ms"]["citation"])
        self.assertEqual(sol["finality_ms"]["provenance"], "unknown")
        self.assertIsNone(sol["finality_ms"]["value"])

    def test_settlement_is_not_probe_rtt(self):
        hit = _hit("https://fast-probe.example/x", rail="base", latency=5)
        eco = economics.for_result(hit)
        self.assertNotEqual(eco["settlement_latency_ms"]["value"], 5)
        self.assertIsNone(eco["settlement_latency_ms"]["value"])
        self.assertEqual(economics.settlement_or_finality_ms(hit), 2000)
        self.assertEqual(select.latency_ms(hit), 5)


class ObjectiveTests(unittest.TestCase):
    def test_lowest_total_cost_fails_closed_when_fee_unknown(self):
        cheap = _hit("https://cheap.example/x", amount=1000)
        dear = _hit("https://dear.example/x", amount=9000)
        self.assertIsNone(economics.total_cost_usd(cheap))
        self.assertIsNone(select.pick_winner([cheap, dear], "lowest_total_cost", None))
        cons = select.parse_constraints({"max_total_cost_usd": 1.0})
        self.assertFalse(select.passes_constraints(cheap, cons))
        self.assertIsNone(select.pick_selected_payment(cheap, "lowest_total_cost", None))

    def test_fastest_settlement_not_probe_rtt(self):
        fast_probe = _hit("https://fast-probe.example/x", rail="solana", latency=1)
        slower_probe = _hit("https://slow-probe.example/x", rail="base", latency=80)
        # Solana finality unknown; Base has 2000ms protocol_reference.
        winner = select.pick_winner([fast_probe, slower_probe], "fastest_settlement", None)
        self.assertIs(winner, slower_probe)
        self.assertEqual(winner["rail"], "base")
        self.assertGreater(select.latency_ms(winner), select.latency_ms(fast_probe))
        # Probe-RTT fastest still picks Solana.
        rtt = select.pick_winner([fast_probe, slower_probe], "fastest", None)
        self.assertIs(rtt, fast_probe)

    def test_algorand_wins_only_on_visible_economics(self):
        algo = _hit("https://algo.example/x", rail="algorand", latency=50)
        sol = _hit("https://sol.example/x", rail="solana", latency=5)
        winner = select.pick_winner([sol, algo], "fastest_settlement", None)
        self.assertIs(winner, algo)
        rows = select.comparison([sol, algo], winner, "fastest_settlement", None)
        by_url = {r["url"]: r for r in rows}
        self.assertTrue(by_url[algo["url"]]["selected"])
        self.assertEqual(by_url[algo["url"]]["economics"]["finality_ms"]["value"], 2820)
        self.assertEqual(by_url[sol["url"]]["economics"]["finality_ms"]["provenance"], "unknown")
        self.assertEqual(by_url[algo["url"]]["latency_ms"], 50)

    def test_base_can_beat_algorand_on_finality(self):
        base = _hit("https://base.example/x", rail="base")
        algo = _hit("https://algo.example/x", rail="algorand")
        winner = select.pick_winner([algo, base], "fastest_settlement", None)
        self.assertIs(winner, base)
        self.assertEqual(winner["rail"], "base")

    def test_no_algo_bonus_token(self):
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[1] / "live402" / "economics.py").read_text()
        self.assertNotIn("algo_bonus", text)
        self.assertNotIn("algo_multiplier", text)


if __name__ == "__main__":
    unittest.main()
