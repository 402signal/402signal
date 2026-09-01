"""PR1 D: observed x402 challenge is not automatically payable."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, probe, select


VALID_BASE = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"


def _env(accepts, version=2):
    return {"x402Version": version, "accepts": accepts}


class RailOfNetworkTests(unittest.TestCase):
    def test_exact_ids_only(self):
        self.assertEqual(payment.rail_of_network("eip155:8453"), "base")
        self.assertEqual(payment.rail_of_network(payment.SOLANA_MAINNET), "solana")
        self.assertEqual(payment.rail_of_network(payment.ALGORAND_MAINNET), "algorand")
        self.assertEqual(payment.rail_of_network("base"), "base")
        self.assertIsNone(payment.rail_of_network("eip155:84532"))
        self.assertIsNone(payment.rail_of_network("eip155:8453:extra"))
        self.assertIsNone(payment.rail_of_network("not-solana-but-contains-solana"))
        self.assertIsNone(payment.rail_of_network("mysolana"))
        self.assertIsNone(payment.rail_of_network("the-algorand-chain"))
        self.assertIsNone(payment.rail_of_network("algorand:testnet"))
        self.assertIsNone(payment.rail_of_network("solana:devnet"))


class ObservedAcceptValidatorTests(unittest.TestCase):
    def test_complete_base_accept_is_selectable(self):
        acc = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        opt = payment.validate_observed_accept(acc, _env([acc]))
        self.assertIsNotNone(opt)
        self.assertTrue(payment.is_complete_payment_option(opt, _env([acc])))

    def test_parseable_malformed_is_not_payable(self):
        envelope = _env(
            [
                {
                    "scheme": "exact",
                    "network": payment.BASE_CAIP2,
                    "payTo": VALID_BASE,
                }
            ]
        )
        result = {
            "live": True,
            "status": 402,
            "envelope": envelope,
            "payTo": VALID_BASE,
        }
        result = probe.attach_invocable_target(result, None, envelope)
        self.assertTrue(result.get("challenge_observed"))
        self.assertFalse(result.get("payable"))
        self.assertIsNone(select.pick_selected_payment(result, "cheapest", None))

    def test_wrong_rail_payto(self):
        acc = {
            "scheme": "exact",
            "network": payment.SOLANA_MAINNET,
            "asset": payment.USDC_SOLANA_MINT,
            "amount": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))

    def test_short_base_payto(self):
        acc = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": "0xabc",
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))

    def test_unsupported_scheme_and_version(self):
        acc = {
            "scheme": "upto",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))
        acc2 = dict(acc)
        acc2["scheme"] = "exact"
        self.assertIsNone(payment.validate_observed_accept(acc2, _env([acc2], version=99)))

    def test_negative_and_conflicting_amount(self):
        acc = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "-1",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))
        acc2 = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "maxAmountRequired": "20000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc2, _env([acc2])))

    def test_timeout_bounds(self):
        acc = {
            "scheme": "exact",
            "network": payment.BASE_CAIP2,
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 0,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))
        acc["maxTimeoutSeconds"] = 86401
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))
        acc["maxTimeoutSeconds"] = 60
        self.assertIsNotNone(payment.validate_observed_accept(acc, _env([acc])))

    def test_unknown_network_prefix(self):
        acc = {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": payment.USDC_BASE,
            "amount": "10000",
            "payTo": VALID_BASE,
            "maxTimeoutSeconds": 60,
        }
        self.assertIsNone(payment.validate_observed_accept(acc, _env([acc])))
        self.assertIsNone(payment.rail_of_network("eip155:84532"))


if __name__ == "__main__":
    unittest.main()
