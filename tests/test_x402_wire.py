"""Version-aware x402 wire, no synthesis, rail addresses, asset vs USD."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, probe, select


VALID_BASE = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"
VALID_BASE_UPPER = "0x" + "A" * 40


def _v2(acc, version=2):
    return {"x402Version": version, "accepts": [acc]}


def _base_acc(**overrides):
    acc = {
        "scheme": "exact",
        "network": payment.BASE_CAIP2,
        "asset": payment.USDC_BASE,
        "amount": "10000",
        "payTo": VALID_BASE,
        "maxTimeoutSeconds": 60,
    }
    acc.update(overrides)
    return acc


class ObservedNetworkAliasTests(unittest.TestCase):
    def test_v2_rejects_short_aliases_and_case(self):
        self.assertIsNone(payment.rail_of_observed_network("base", 2))
        self.assertIsNone(payment.rail_of_observed_network("solana", 2))
        self.assertIsNone(payment.rail_of_observed_network("algorand", 2))
        self.assertIsNone(payment.rail_of_observed_network(payment.BASE_CAIP2.upper(), 2))
        self.assertIsNone(payment.rail_of_observed_network("eip155:8453 ", 2))
        self.assertEqual(payment.rail_of_observed_network(payment.BASE_CAIP2, 2), "base")
        self.assertEqual(payment.rail_of_network("base"), "base")

    def test_v1_allows_exact_short_aliases(self):
        self.assertEqual(payment.rail_of_observed_network("base", 1), "base")
        self.assertIsNone(payment.rail_of_observed_network("BASE", 1))
        self.assertIsNone(payment.rail_of_observed_network("Base", 1))


class VersionAwareAcceptTests(unittest.TestCase):
    def test_v2_literal_version_only(self):
        acc = _base_acc()
        self.assertIsNotNone(payment.validate_observed_accept(acc, _v2(acc, 2)))
        self.assertIsNone(payment.validate_observed_accept(acc, {"x402Version": "2", "accepts": [acc]}))
        self.assertIsNone(payment.validate_observed_accept(acc, {"x402Version": 2.0, "accepts": [acc]}))
        self.assertIsNone(payment.validate_observed_accept(acc, {"x402Version": True, "accepts": [acc]}))

    def test_v2_requires_scheme_timeout_amount_string(self):
        acc = _base_acc()
        acc.pop("scheme")
        self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)))
        acc = _base_acc()
        acc.pop("maxTimeoutSeconds")
        self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)))
        acc = _base_acc(amount=10000)
        self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)))

    def test_v2_rejects_short_network_alias(self):
        acc = _base_acc(network="base")
        self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)))

    def test_amount_rejects_non_canonical(self):
        for amt in (10000, 1.0, "1e4", "+10000", " 10000", "010000", True, "NaN", "Inf", "1" * 40):
            acc = _base_acc(amount=amt)
            self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)), amt)

    def test_v1_uses_max_amount_required_not_amount(self):
        acc = {
            "scheme": "exact",
            "network": "base",
            "asset": payment.USDC_BASE,
            "maxAmountRequired": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
            "x402Version": 1,
        }
        self.assertIsNotNone(payment.validate_observed_accept(acc, {"x402Version": 1, "accepts": [acc]}))
        swapped = dict(acc)
        swapped.pop("maxAmountRequired")
        swapped["amount"] = "10000"
        self.assertIsNone(payment.validate_observed_accept(swapped, {"x402Version": 1, "accepts": [swapped]}))

    def test_v2_does_not_use_max_amount_required_as_amount(self):
        acc = _base_acc()
        acc.pop("amount")
        acc["maxAmountRequired"] = "10000"
        self.assertIsNone(payment.validate_observed_accept(acc, _v2(acc)))

    def test_seller_json_nan_is_not_envelope(self):
        env, miss = probe.parse_envelope(
            402,
            {},
            b'{"x402Version": 2, "accepts": [{"payTo": "0xabc", "amount": NaN}]}',
        )
        self.assertIsNone(env)
        self.assertEqual(miss, "no_402_envelope")


class NoSynthesisTests(unittest.TestCase):
    def test_all_accepts_malformed_does_not_synthesize(self):
        envelope = {
            "x402Version": 2,
            "accepts": [{"network": "base", "amount": "10000"}],
        }
        result = {
            "live": True,
            "envelope": envelope,
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": VALID_BASE,
            "scheme": "exact",
            "maxTimeoutSeconds": 60,
        }
        result = probe.attach_invocable_target(result, None, envelope)
        opts = payment.payment_options_from_result(result)
        self.assertEqual(opts, [])
        self.assertTrue(result.get("challenge_observed"))
        self.assertFalse(result.get("payable"))
        self.assertIsNone(select.pick_selected_payment(result, "cheapest", None))


class RailAddressTests(unittest.TestCase):
    def test_base_all_lower_all_upper(self):
        self.assertTrue(payment.valid_payto_for_rail(VALID_BASE, "base"))
        self.assertTrue(payment.valid_payto_for_rail(VALID_BASE_UPPER, "base"))
        self.assertFalse(payment.valid_payto_for_rail("0xabc", "base"))
        self.assertFalse(payment.valid_payto_for_rail("0x" + "g" * 40, "base"))

    def test_algorand_checksum_not_charset(self):
        good = payment.DEFAULT_PAYTO_ALGORAND
        self.assertTrue(payment.valid_payto_for_rail(good, "algorand"))
        bad = good[:-1] + ("A" if good[-1] != "A" else "B")
        self.assertEqual(len(bad), 58)
        self.assertFalse(payment.valid_payto_for_rail(bad, "algorand"))
        self.assertFalse(payment.valid_payto_for_rail("A" * 58, "algorand"))

    def test_solana_exactly_32_bytes(self):
        self.assertTrue(payment.valid_payto_for_rail(payment.DEFAULT_PAYTO_SOLANA, "solana"))
        self.assertFalse(payment.valid_payto_for_rail("2", "solana"))
        self.assertFalse(payment.valid_payto_for_rail("0x" + "ab" * 16, "solana"))
        self.assertFalse(payment.valid_payto_for_rail("not-valid-base58!!!", "solana"))


class AssetUsdTests(unittest.TestCase):
    def test_bare_usdc_is_not_known_id(self):
        self.assertFalse(payment.known_usdc_asset("USDC", "base"))
        self.assertFalse(payment.known_usdc_asset("USD", payment.BASE_CAIP2))
        self.assertTrue(payment.known_usdc_asset(payment.USDC_BASE, payment.BASE_CAIP2))
        opt = payment.payment_option_from_accept(
            {
                "network": payment.BASE_CAIP2,
                "asset": "USDC",
                "amount": "10000",
                "payTo": VALID_BASE,
            }
        )
        self.assertIsNone(opt["normalized_usd"])
        self.assertIsNone(opt["decimals"])


if __name__ == "__main__":
    unittest.main()
