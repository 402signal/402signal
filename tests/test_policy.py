"""PR15 natural-language policy: compile, unresolved, engine uses structured only."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import policy, select


class CompilePolicyTests(unittest.TestCase):
    def test_weather_under_cent_and_300ms(self):
        out = policy.compile_policy("weather under $0.01 and 300ms")
        interpreted = out["interpreted_constraints"]
        self.assertAlmostEqual(interpreted["max_price_usd"], 0.01)
        self.assertEqual(interpreted["max_probe_latency_ms"], 300)
        self.assertEqual(interpreted["max_latency_ms"], 300)
        self.assertNotIn("max_service_latency_ms", interpreted)
        self.assertNotIn("networks", interpreted)
        self.assertEqual(out["unresolved_constraints"], [])

    def test_service_latency_only_when_named(self):
        out = policy.compile_policy("weather under $0.02 and service 400ms")
        self.assertEqual(out["interpreted_constraints"]["max_service_latency_ms"], 400)
        self.assertNotIn("max_probe_latency_ms", out["interpreted_constraints"])

    def test_high_reputation_unresolved_settlement_with_number_compiles(self):
        out = policy.compile_policy(
            "weather under $0.01 with high reputation and settlement under 2s"
        )
        self.assertAlmostEqual(out["interpreted_constraints"]["max_price_usd"], 0.01)
        self.assertEqual(out["interpreted_constraints"]["max_settlement_latency_ms"], 2000)
        names = {row["name"] for row in out["unresolved_constraints"]}
        self.assertIn("min_reputation_score", names)
        self.assertNotIn("min_reputation_score", out["interpreted_constraints"])

    def test_settlement_without_number_is_unresolved(self):
        out = policy.compile_policy("weather with fast settlement")
        names = {row["name"] for row in out["unresolved_constraints"]}
        self.assertIn("max_settlement_latency_ms", names)
        self.assertNotIn("max_settlement_latency_ms", out["interpreted_constraints"])

    def test_established_usage_compiles_to_min_observations(self):
        out = policy.compile_policy("weather with established usage")
        self.assertEqual(out["interpreted_constraints"]["min_observations"], 10)
        self.assertNotIn("min_reputation_score", out["interpreted_constraints"])
        strong = policy.compile_policy("weather with strong observed evidence")
        self.assertEqual(strong["interpreted_constraints"]["min_observations"], 10)

    def test_total_cost_with_number_compiles(self):
        out = policy.compile_policy("weather with total cost under $0.02")
        self.assertAlmostEqual(out["interpreted_constraints"]["max_total_cost_usd"], 0.02)
        vague = policy.compile_policy("weather with low total cost")
        names = {row["name"] for row in vague["unresolved_constraints"]}
        self.assertIn("max_total_cost_usd", names)

    def test_vague_cheap_fast_unresolved(self):
        out = policy.compile_policy("cheap fast weather")
        self.assertEqual(out["interpreted_constraints"], {})
        names = {row["name"] for row in out["unresolved_constraints"]}
        self.assertIn("max_price_usd", names)
        self.assertIn("max_probe_latency_ms", names)

    def test_explicit_network_and_invocable(self):
        out = policy.compile_policy("weather on solana, must be invocable, at least 10 observations")
        cons = out["interpreted_constraints"]
        self.assertEqual(cons["networks"], ["solana"])
        self.assertTrue(cons["require_invocable"])
        self.assertEqual(cons["min_observations"], 10)
        self.assertEqual(out["unresolved_constraints"], [])


class EngineUsesStructuredOnlyTests(unittest.TestCase):
    def test_merge_structured_wins_over_nl(self):
        body = {
            "need": "weather under $0.01 and 300ms",
            "max_price_usd": 0.05,
            "max_probe_latency_ms": 80,
        }
        cons = policy.merge_constraints(body)
        self.assertAlmostEqual(cons["max_price_usd"], 0.05)
        self.assertEqual(cons["max_probe_latency_ms"], 80)

    def test_nl_feeds_structured_engine(self):
        body = {"need": "weather under $0.01 and 300ms"}
        cons = policy.merge_constraints(body)
        self.assertAlmostEqual(cons["max_price_usd"], 0.01)
        self.assertEqual(cons["max_probe_latency_ms"], 300)
        cheap = {
            "live": True,
            "invocable": True,
            "url": "https://cheap.example/x",
            "rail": "base",
            "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
            "latency_ms": 40,
            "amount": 10000,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "accepts": [
                {
                    "network": "eip155:8453",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                    "amount": 10000,
                }
            ],
            "history": {"n_7d": 0, "p50_latency_ms": None},
        }
        dear = dict(cheap)
        dear = dict(cheap)
        dear["url"] = "https://dear.example/x"
        dear["amount"] = 50000
        dear["accepts"] = [
            {
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                "amount": 50000,
            }
        ]
        self.assertTrue(select.passes_constraints(cheap, cons))
        self.assertFalse(select.passes_constraints(dear, cons))

    def test_unresolved_nl_does_not_fail_closed_unless_structured(self):
        compiled = policy.compile_policy("weather with high reputation")
        self.assertTrue(compiled["unresolved_constraints"])
        cons = policy.merge_constraints({"need": "weather with high reputation"})
        self.assertEqual(cons["unmeasured"], ())
        hit = {
            "live": True,
            "invocable": True,
            "url": "https://ok.example/x",
            "rail": "base",
            "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
            "latency_ms": 10,
            "amount": 10000,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "accepts": [
                {
                    "network": "eip155:8453",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                    "amount": 10000,
                }
            ],
        }
        self.assertTrue(select.passes_constraints(hit, cons))
        structured = policy.merge_constraints({"need": "weather", "min_reputation_score": 0.9})
        self.assertEqual(structured["unmeasured"], ())
        self.assertFalse(select.passes_constraints(hit, structured))


if __name__ == "__main__":
    unittest.main()
