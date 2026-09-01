"""PR13 routing correctness: observed vs claimed, selected_payment, payable, changes."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import history, payment, probe, select


CATALOG_BASE_PAYTO = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"
OBS_BASE_PAYTO = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SOL_PAYTO = payment.DEFAULT_PAYTO_SOLANA
CDP = "https://api.cdp.coinbase.com/platform/v2/x402"
PAYAI = "https://facilitator.payai.network"


def _catalog_base_and_solana(url):
    return {
        "url": url,
        "description": "weather on two rails",
        "_rail": "base",
        "accepts": [
            {
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": "20000",
                "payTo": CATALOG_BASE_PAYTO,
                "extra": {
                    "facilitator": CDP,
                    "displayAmount": "$0.02",
                },
            },
            {
                "network": payment.SOLANA_MAINNET,
                "asset": payment.USDC_SOLANA_MINT,
                "amount": "1000",
                "payTo": SOL_PAYTO,
                "extra": {
                    "facilitator": PAYAI,
                    "displayAmount": "$0.001",
                },
            },
        ],
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }


def _live_base_only(url, catalog_item=None, pay_to=OBS_BASE_PAYTO, amount="20000"):
    """HTTP 402 envelope is Base $0.02 only. Catalog may claim extra rails."""
    envelope = {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "extra": {
                    "facilitator": CDP,
                    "displayAmount": "$0.02",
                },
            }
        ],
    }
    result = {
        "url": url,
        "live": True,
        "status": 402,
        "has_402_challenge": True,
        "payTo": pay_to,
        "latency_ms": 10,
        "envelope": envelope,
        "rail": "base",
        "amount": amount,
        "asset": payment.USDC_BASE,
        "history": {
            "success_7d": None,
            "n_7d": 0,
            "success_24h": None,
            "n_24h": 0,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
        },
    }
    item = catalog_item or {"url": url}
    result = probe.attach_catalog_fields(result, item)
    result = probe.attach_invocable_target(result, item, envelope)
    return result


class ObservedVsClaimedTests(unittest.TestCase):
    def test_catalog_solana_is_not_observed(self):
        url = "https://wx.example/claimed-sol"
        item = _catalog_base_and_solana(url)
        hit = _live_base_only(url, item)
        claimed_opts = (hit.get("claimed") or {}).get("payment_options") or []
        claimed_rails = {o.get("rail") for o in claimed_opts}
        self.assertEqual(claimed_rails, {"base", "solana"})
        observed = payment.payment_options_from_result(hit)
        self.assertEqual([o.get("rail") for o in observed], ["base"])
        self.assertEqual(observed[0]["amount_atomic"], 20000)
        target_rails = {
            payment.rail_of_network(a.get("network"))
            for a in (hit.get("target") or {}).get("accepts") or []
        }
        self.assertEqual(target_rails, {"base"})
        self.assertNotIn("solana", target_rails)

    def test_networks_solana_does_not_select_catalog_only_rail(self):
        url = "https://wx.example/no-sol-select"
        hit = _live_base_only(url, _catalog_base_and_solana(url))
        cons = select.parse_constraints({"networks": ["solana"]})
        self.assertFalse(select.passes_constraints(hit, cons))
        self.assertIsNone(select.pick_winner([hit], "cheapest", cons))
        self.assertIsNone(select.pick_winner([hit], "best", cons))

    def test_cheapest_does_not_pick_catalog_solana_millicent(self):
        url = "https://wx.example/no-cheap-sol"
        claimed = _live_base_only(url, _catalog_base_and_solana(url))
        other = _live_base_only(
            "https://wx.example/other-base",
            {"url": "https://wx.example/other-base"},
            pay_to="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            amount="20000",
        )
        winner = select.pick_winner([claimed, other], "cheapest", None)
        self.assertIsNotNone(winner)
        selected = select.pick_selected_payment(winner, "cheapest", None)
        self.assertEqual(selected["rail"], "base")
        self.assertEqual(selected["amount_atomic"], 20000)
        self.assertNotEqual(selected["rail"], "solana")
        self.assertNotEqual(selected["amount_atomic"], 1000)
        usd = select._best_usd(claimed)
        self.assertAlmostEqual(usd, 0.02)

    def test_max_price_usd_does_not_pass_via_solana_claim(self):
        url = "https://wx.example/max-price"
        hit = _live_base_only(url, _catalog_base_and_solana(url))
        cons = select.parse_constraints({"max_price_usd": 0.01})
        self.assertFalse(select.passes_constraints(hit, cons))
        self.assertIsNone(select.pick_winner([hit], "cheapest", cons))
        self.assertIsNone(select.pick_selected_payment(hit, "cheapest", cons))


class SelectedPaymentTests(unittest.TestCase):
    def test_selected_payment_is_the_observed_option(self):
        url = "https://wx.example/selected"
        hit = _live_base_only(url, _catalog_base_and_solana(url))
        selected = select.pick_selected_payment(hit, "cheapest", None)
        self.assertIsNotNone(selected)
        identity = {k: selected[k] for k in (
            "rail", "network", "asset", "amount_atomic",
            "display_amount", "normalized_usd", "payTo", "facilitator",
        )}
        self.assertEqual(
            identity,
            {
                "rail": "base",
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount_atomic": 20000,
                "display_amount": "$0.02",
                "normalized_usd": 0.02,
                "payTo": OBS_BASE_PAYTO,
                "facilitator": CDP,
            },
        )
        eco = selected.get("economics")
        self.assertIsInstance(eco, dict)
        self.assertEqual(eco.get("rail"), "base")
        allowed_prov = {"402signal_observed", "protocol_reference", "unknown"}
        for field in (
            "merchant_price_usd",
            "chain_fee_usd",
            "facilitator_fee_usd",
            "total_cost_usd",
            "settlement_latency_ms",
            "finality_ms",
            "settlement_or_finality_ms",
        ):
            self.assertIn((eco.get(field) or {}).get("provenance"), allowed_prov, msg=field)
        self.assertEqual((eco.get("merchant_price_usd") or {}).get("provenance"), "402signal_observed")
        self.assertEqual((eco.get("merchant_price_usd") or {}).get("value"), 0.02)
        # Economics must not swap the observed winner for the cheaper catalog Solana bait.
        self.assertNotEqual(selected["amount_atomic"], 1000)
        self.assertNotEqual(selected["payTo"], SOL_PAYTO)
        probe._align_target_with_selected(hit, selected)
        target = hit["target"]
        self.assertEqual(target["amountAtomic"], "20000")
        self.assertEqual(target["displayAmount"], "$0.02")
        self.assertEqual(target["facilitator"], CDP)
        self.assertNotEqual(target["amountAtomic"], "1000")
        compared = select.comparison([hit], hit, "cheapest", None)
        self.assertTrue(compared[0]["selected"])
        self.assertEqual(compared[0]["selected_payment"], selected)
        self.assertEqual(compared[0]["rail"], "base")
        self.assertEqual(compared[0]["amount_atomic"], 20000)

    def test_cheapest_among_two_observed_options_picks_lower_usd(self):
        envelope = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount": "20000",
                    "payTo": OBS_BASE_PAYTO,
                    "extra": {"facilitator": CDP, "displayAmount": "$0.02"},
                },
                {
                    "scheme": "exact",
                    "network": payment.SOLANA_MAINNET,
                    "asset": payment.USDC_SOLANA_MINT,
                    "amount": "1000",
                    "payTo": SOL_PAYTO,
                    "extra": {"facilitator": PAYAI, "displayAmount": "$0.001"},
                },
            ],
        }
        result = {
            "url": "https://wx.example/both-live",
            "live": True,
            "status": 402,
            "has_402_challenge": True,
            "payTo": OBS_BASE_PAYTO,
            "latency_ms": 8,
            "envelope": envelope,
            "rail": "base",
        }
        result = probe.attach_invocable_target(result, None, envelope)
        selected = select.pick_selected_payment(result, "cheapest", None)
        self.assertEqual(selected["rail"], "solana")
        self.assertEqual(selected["amount_atomic"], 1000)
        self.assertAlmostEqual(selected["normalized_usd"], 0.001)
        self.assertEqual(selected["facilitator"], PAYAI)
        probe._align_target_with_selected(result, selected)
        self.assertEqual(result["target"]["amountAtomic"], "1000")
        self.assertEqual(result["target"]["facilitator"], PAYAI)


class StrictPayableTests(unittest.TestCase):
    def test_live_payto_alone_is_not_payable(self):
        result = {
            "live": True,
            "payTo": OBS_BASE_PAYTO,
            "status": 402,
            "has_402_challenge": True,
        }
        result = probe.attach_invocable_target(result, None, None)
        self.assertTrue(result.get("challenge_observed"))
        self.assertFalse(result.get("payable"))
        self.assertFalse(result.get("invocable"))
        self.assertFalse(select._is_payable(result))
        self.assertFalse(history._observed_payable(result))

    def test_catalog_fields_do_not_complete_an_incomplete_envelope(self):
        item = _catalog_base_and_solana("https://wx.example/incomplete")
        envelope = {"x402Version": 2, "accepts": [{"network": payment.BASE_CAIP2}]}
        result = {
            "url": "https://wx.example/incomplete",
            "live": True,
            "status": 402,
            "has_402_challenge": True,
            "payTo": OBS_BASE_PAYTO,
            "envelope": envelope,
        }
        result = probe.attach_catalog_fields(result, item)
        result = probe.attach_invocable_target(result, item, envelope)
        self.assertTrue(result.get("challenge_observed"))
        self.assertFalse(result.get("payable"))
        self.assertFalse(result.get("invocable"))
        claimed = result.get("claimed") or {}
        self.assertTrue(claimed.get("payment_options"))

    def test_complete_observed_option_is_payable(self):
        url = "https://wx.example/complete"
        hit = _live_base_only(url, _catalog_base_and_solana(url))
        self.assertTrue(hit.get("challenge_observed"))
        self.assertTrue(hit.get("payable"))
        self.assertTrue(hit.get("invocable"))
        self.assertTrue(select._is_payable(hit))


class PersistChangeRehydrateTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("LIVE402_HISTORY_DB")
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_HISTORY_DB"] = self._path
        history.reset()

    def tearDown(self):
        history.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev
        for p in (self._path, self._path + "-wal", self._path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_persist_route_batch_returns_meta_and_rehydrates_changes(self):
        url = "https://wx.example/flip-obs"
        t0 = int(time.time()) - 10
        first = _live_base_only(url, _catalog_base_and_solana(url), pay_to="0x1111111111111111111111111111111111111111")
        first["ts"] = t0
        first["batch_id"] = "a" * 32
        history.persist_route_batch(first["batch_id"], [first])

        later = _live_base_only(url, _catalog_base_and_solana(url), pay_to=CATALOG_BASE_PAYTO)
        later["ts"] = t0 + 5
        later["batch_id"] = "b" * 32
        metas = history.persist_route_batch(later["batch_id"], [later])
        self.assertIn(url, metas)
        self.assertTrue(metas[url].get("payTo_flipped"))
        self.assertTrue(later.get("payTo_changed"))

        body = dict(later)
        body.pop("payTo_changed", None)
        out = history.attach_to_result(body, metas[url])
        self.assertTrue(out.get("payTo_changed"))
        self.assertEqual(out.get("risk"), ["payTo_changed"])
        changes = out.get("changes") or {}
        self.assertIn("payTo_changed_at", changes)
        claimed_opts = (out.get("claimed") or {}).get("payment_options") or []
        self.assertTrue(claimed_opts)

    def test_payto_changed_even_when_live_now_matches_catalog(self):
        url = "https://wx.example/match-catalog-after-flip"
        item = _catalog_base_and_solana(url)
        previous = _live_base_only(url, item, pay_to=OBS_BASE_PAYTO)
        previous["ts"] = int(time.time()) - 8
        previous["batch_id"] = "c" * 32
        history.persist_route_batch(previous["batch_id"], [previous])

        current = _live_base_only(url, item, pay_to=CATALOG_BASE_PAYTO)
        current["ts"] = int(time.time()) - 1
        current["batch_id"] = "d" * 32
        metas = history.persist_route_batch(current["batch_id"], [current])
        out = history.attach_to_result(current, metas[url])
        self.assertTrue(out.get("payTo_changed"))
        self.assertEqual(out.get("risk"), ["payTo_changed"])
        claimed_pay = (out.get("claimed") or {}).get("payTo")
        self.assertTrue(
            payment.payto_equal(claimed_pay or CATALOG_BASE_PAYTO, CATALOG_BASE_PAYTO, "base")
        )


if __name__ == "__main__":
    unittest.main()
