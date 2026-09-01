"""Asset-aware payment options, cheapest fail-closed, rail-aware payTo."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, payment, probe, select


UNKNOWN_BASE = "0x1111111111111111111111111111111111111111"
UNKNOWN_SOL = "UnkTok111111111111111111111111111111111111"
UNKNOWN_B = "0x2222222222222222222222222222222222222222"


def _payto_for_network(network):
    rail = payment.rail_of_network(network) or "base"
    if rail == "solana":
        return payment.DEFAULT_PAYTO_SOLANA
    if rail == "algorand":
        return payment.DEFAULT_PAYTO_ALGORAND
    return "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"


def _opt(network, asset, amount, pay_to=None, extra=None):
    if pay_to is None:
        pay_to = _payto_for_network(network)
    acc = {"network": network, "asset": asset, "amount": amount, "payTo": pay_to}
    if extra:
        acc["extra"] = extra
    return payment.payment_option_from_accept(acc)


def _hit_accept(url, network, asset, amount, rail=None, pay_to=None, **extra):
    rail = rail or payment.rail_of_network(network) or "base"
    if pay_to is None:
        pay_to = _payto_for_network(network)
    amt = amount if isinstance(amount, str) else str(amount)
    acc = {
        "scheme": "exact",
        "network": network,
        "asset": asset,
        "amount": amt,
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
    }
    row = {
        "url": url,
        "rail": rail,
        "live": True,
        "payTo": pay_to,
        "invocable": True,
        "latency_ms": extra.pop("latency", 10),
        "amount": amt,
        "asset": asset,
        "envelope": {"x402Version": 2, "accepts": [acc]},
        "accepts": [acc],
        "target": {"accepts": [acc]},
        "history": extra.pop(
            "history",
            {
                "success_7d": None,
                "n_7d": 0,
                "success_24h": None,
                "n_24h": 0,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
            },
        ),
    }
    row.update(extra)
    return row


class PaymentOptionTests(unittest.TestCase):
    def test_base_and_solana_usdc_same_usd(self):
        base = _opt(payment.BASE_CAIP2, payment.USDC_BASE, 10000)
        sol = _opt(payment.SOLANA_MAINNET, payment.USDC_SOLANA_MINT, 10000)
        self.assertEqual(base["decimals"], 6)
        self.assertEqual(sol["decimals"], 6)
        self.assertAlmostEqual(base["normalized_usd"], 0.01)
        self.assertAlmostEqual(sol["normalized_usd"], 0.01)
        self.assertEqual(base["display_amount"], "$0.01")
        self.assertEqual(sol["display_amount"], "$0.01")
        self.assertTrue(payment.prices_equivalent(base, sol))

    def test_unknown_million_atomic_is_not_a_dollar(self):
        opt = _opt(payment.BASE_CAIP2, UNKNOWN_BASE, 1_000_000)
        self.assertIsNone(opt["normalized_usd"])
        self.assertIsNone(opt["decimals"])
        self.assertNotEqual(opt["display_amount"], "$1.00")
        self.assertNotIn("$", opt["display_amount"] or "")
        shown = probe._display_amount(1_000_000, {}, UNKNOWN_BASE, payment.BASE_CAIP2)
        self.assertNotEqual(shown, "$1.00")
        self.assertIsNone(probe._display_amount(1_000_000, {}))
        target = probe.build_target(
            {
                "accepts": [
                    {
                        "network": payment.BASE_CAIP2,
                        "asset": UNKNOWN_BASE,
                        "amount": "1000000",
                        "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                    }
                ]
            }
        )
        self.assertNotEqual(target.get("displayAmount"), "$1.00")
        if target.get("displayAmount"):
            self.assertNotIn("$1.00", target["displayAmount"])

    def test_unknown_prefers_seller_display_string(self):
        opt = _opt(
            payment.BASE_CAIP2,
            UNKNOWN_BASE,
            1_000_000,
            extra={"displayAmount": "1000000 CUSTOM"},
        )
        self.assertEqual(opt["display_amount"], "1000000 CUSTOM")
        self.assertIsNone(opt["normalized_usd"])

    def test_algorand_usdc_known(self):
        opt = _opt(payment.ALGORAND_MAINNET, payment.USDC_ALGORAND_ASA, 10000)
        self.assertAlmostEqual(opt["normalized_usd"], 0.01)
        self.assertEqual(opt["decimals"], 6)


class CheapestAssetTests(unittest.TestCase):
    def test_base_usdc_vs_solana_usdc_same_dollar_compares(self):
        dear = _hit_accept(
            "https://base.example/x",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            20000,
            rail="base",
        )
        cheap = _hit_accept(
            "https://sol.example/x",
            payment.SOLANA_MAINNET,
            payment.USDC_SOLANA_MINT,
            10000,
            rail="solana",
        )
        winner = select.pick_winner([dear, cheap], "cheapest", None)
        self.assertIs(winner, cheap)
        tied = _hit_accept(
            "https://sol-tied.example/x",
            payment.SOLANA_MAINNET,
            payment.USDC_SOLANA_MINT,
            20000,
            rail="solana",
        )
        winner_tie = select.pick_winner([dear, tied], "cheapest", None)
        self.assertIs(winner_tie, dear)

    def test_unknown_one_atomic_is_not_cheaper_than_usdc_cent(self):
        usdc = _hit_accept(
            "https://usdc.example/x",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            10000,
            rail="base",
        )
        unknown = _hit_accept(
            "https://unk.example/x",
            payment.BASE_CAIP2,
            UNKNOWN_BASE,
            1,
            rail="base",
        )
        winner = select.pick_winner([unknown, usdc], "cheapest", None)
        self.assertIs(winner, usdc)
        self.assertIsNot(winner, unknown)

    def test_two_unknown_tokens_no_cheapest_winner(self):
        a = _hit_accept(
            "https://a.example/x",
            payment.BASE_CAIP2,
            UNKNOWN_BASE,
            1,
            rail="base",
        )
        b = _hit_accept(
            "https://b.example/x",
            payment.BASE_CAIP2,
            UNKNOWN_B,
            1,
            rail="base",
        )
        self.assertIsNone(select.pick_winner([a, b], "cheapest", None))
        self.assertFalse(select.enough_evidence([a, b], "cheapest", None))

    def test_cheapest_does_not_treat_incomparable_assets_as_identical(self):
        usdc = _hit_accept(
            "https://usdc.example/x",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            10000,
            rail="base",
        )
        other = _hit_accept(
            "https://other.example/x",
            payment.SOLANA_MAINNET,
            UNKNOWN_SOL,
            10000,
            rail="solana",
        )
        winner = select.pick_winner([usdc, other], "cheapest", None)
        self.assertIs(winner, usdc)
        self.assertNotEqual(select._cmp_amount_asc(usdc, other), -1)
        self.assertEqual(select._cmp_amount_asc(usdc, other), 0)

    def test_max_amount_atomic_drops_unknown_and_cross_asset(self):
        usdc = _hit_accept(
            "https://usdc.example/x",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            5000,
            rail="base",
        )
        unknown = _hit_accept(
            "https://unk.example/x",
            payment.BASE_CAIP2,
            UNKNOWN_BASE,
            1,
            rail="base",
        )
        cons = select.parse_constraints({"max_amount_atomic": 10000})
        self.assertTrue(select.passes_constraints(usdc, cons))
        self.assertFalse(select.passes_constraints(unknown, cons))
        self.assertIs(select.pick_winner([unknown, usdc], "best", cons), usdc)

    def test_max_price_usd_uses_normalized_usd(self):
        cheap = _hit_accept(
            "https://cheap.example/x",
            payment.SOLANA_MAINNET,
            payment.USDC_SOLANA_MINT,
            10000,
            rail="solana",
        )
        dear = _hit_accept(
            "https://dear.example/x",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            20000,
            rail="base",
        )
        cons = select.parse_constraints({"max_price_usd": 0.01})
        self.assertTrue(select.passes_constraints(cheap, cons))
        self.assertFalse(select.passes_constraints(dear, cons))

    def test_network_constraint_uses_payment_options_not_canonical_rail(self):
        both = {
            "url": "https://both.example/x",
            "rail": "base",
            "live": True,
            "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
            "invocable": True,
            "latency_ms": 10,
            "amount": 10000,
            "accepts": [
                {
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount": 20000,
                    "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                },
                {
                    "network": payment.SOLANA_MAINNET,
                    "asset": payment.USDC_SOLANA_MINT,
                    "amount": 10000,
                    "payTo": payment.DEFAULT_PAYTO_SOLANA,
                },
            ],
        }
        cons = select.parse_constraints({"networks": ["solana"]})
        self.assertTrue(select.passes_constraints(both, cons))
        winner = select.pick_winner([both], "cheapest", cons)
        self.assertIs(winner, both)
        usd = select._best_usd(both, cons)
        self.assertAlmostEqual(usd, 0.01)


class PayToEqualTests(unittest.TestCase):
    def test_base_case_insensitive(self):
        mixed = "0xb18fc2275f36dae99eb215caeff03b431f887d16"
        upper = mixed.upper()
        self.assertTrue(payment.payto_equal(mixed, upper, "base"))
        self.assertTrue(payment.payto_equal(mixed, upper, payment.BASE_CAIP2))
        self.assertFalse(payment.payto_equal(mixed, "0x0000000000000000000000000000000000000001", "base"))

    def test_solana_case_sensitive(self):
        addr = payment.DEFAULT_PAYTO_SOLANA
        self.assertTrue(payment.payto_equal(addr, addr, "solana"))
        self.assertFalse(payment.payto_equal(addr, addr.lower(), "solana"))
        self.assertFalse(payment.payto_equal(addr, addr.upper(), "solana"))

    def test_algorand_case_insensitive_canonical_upper(self):
        addr = payment.DEFAULT_PAYTO_ALGORAND
        self.assertEqual(payment.payto_canonical(addr.lower(), "algorand"), addr.upper())
        self.assertTrue(payment.payto_equal(addr, addr.lower(), "algorand"))
        self.assertTrue(payment.payto_equal(addr.lower(), addr.upper(), payment.ALGORAND_MAINNET))
        other = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        self.assertFalse(payment.payto_equal(addr, other, "algorand"))

    def test_mixed_rail_negatives(self):
        evm = payment.DEFAULT_PAYTO
        sol = payment.DEFAULT_PAYTO_SOLANA
        algo = payment.DEFAULT_PAYTO_ALGORAND
        self.assertFalse(payment.payto_equal(evm, sol, "base"))
        self.assertFalse(payment.payto_equal(sol, algo, "solana"))
        self.assertFalse(payment.payto_equal(algo, evm, "algorand"))
        self.assertFalse(payment.payto_equal(evm.upper(), evm, "solana"))
        self.assertTrue(payment.payto_equal(evm.upper(), evm, "base"))
        self.assertFalse(payment.payto_equal(sol, evm, "solana"))
        self.assertFalse(payment.payto_equal(sol.lower(), evm, "solana"))


class MergePaymentOptionsTests(unittest.TestCase):
    def test_url_on_two_rails_keeps_both_payment_options(self):
        url = "https://both.example/weather"
        base_item = catalog.slim_item(
            {
                "url": url,
                "description": "weather on base",
                "accepts": [
                    {
                        "network": payment.BASE_CAIP2,
                        "asset": payment.USDC_BASE,
                        "amount": "20000",
                        "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                        "extra": {"displayAmount": "$0.02"},
                    }
                ],
            },
            "base",
        )
        sol_item = catalog.slim_item(
            {
                "url": url,
                "description": "weather on solana",
                "accepts": [
                    {
                        "network": payment.SOLANA_MAINNET,
                        "asset": payment.USDC_SOLANA_MINT,
                        "amount": "10000",
                        "payTo": payment.DEFAULT_PAYTO_SOLANA,
                        "extra": {"displayAmount": "$0.01"},
                    }
                ],
            },
            "solana",
        )
        self.assertEqual(base_item["accepts"][0].get("asset"), payment.USDC_BASE)
        merged = catalog._merge_items(
            {"base": [base_item], "solana": [sol_item], "algorand": []}
        )
        self.assertEqual(len(merged), 1)
        accepts = merged[0].get("accepts") or []
        assets = {a.get("asset") for a in accepts}
        nets = {payment.rail_of_network(a.get("network") or "") for a in accepts}
        self.assertIn(payment.USDC_BASE, assets)
        self.assertIn(payment.USDC_SOLANA_MINT, assets)
        self.assertEqual(nets, {"base", "solana"})
        opts = payment.payment_options_from_accepts(accepts)
        self.assertEqual(len(opts), 2)
        usd = sorted(o["normalized_usd"] for o in opts)
        self.assertEqual(usd, [0.01, 0.02])
        claimed = probe.attach_catalog_fields({"live": False, "payTo": None}, merged[0])
        claimed_opts = (claimed.get("claimed") or {}).get("payment_options") or []
        claimed_assets = {o.get("asset") for o in claimed_opts}
        self.assertIn(payment.USDC_BASE, claimed_assets)
        self.assertIn(payment.USDC_SOLANA_MINT, claimed_assets)
        target = probe.build_target(merged[0])
        target_assets = {a.get("asset") for a in target.get("accepts") or []}
        self.assertEqual(target_assets, set())


class PulsePriceLabelTests(unittest.TestCase):
    def test_unknown_token_price_is_not_fake_dollar(self):
        from live402 import pulse as pulse_mod

        label, usd = pulse_mod._price_from_accept(
            {"network": payment.BASE_CAIP2, "asset": UNKNOWN_BASE, "amount": "1000000"}
        )
        self.assertIsNone(usd)
        self.assertNotEqual(label, "$1.00")

    def test_known_usdc_still_cents(self):
        from live402 import pulse as pulse_mod

        label, usd = pulse_mod._price_from_accept(
            {
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": "10000",
            }
        )
        self.assertEqual(label, "$0.01")
        self.assertAlmostEqual(usd, 0.01)


if __name__ == "__main__":
    unittest.main()
