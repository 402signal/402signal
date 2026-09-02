"""PR1 C: explicit structured constraints fail closed. No silent weaken."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import policy, select
from live402.select import ConstraintError


class ExplicitConstraintTests(unittest.TestCase):
    def test_wrong_type_raises(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_amount_atomic": "10000"})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_price_usd": "0.01"})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"require_invocable": 1})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"require_invocable": "true"})

    def test_bool_is_not_int(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_latency_ms": True})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"min_observations": False})

    def test_negative_and_nan_and_inf(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_amount_atomic": -1})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_price_usd": -0.01})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_price_usd": float("nan")})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_total_cost_usd": float("inf")})

    def test_probability_bounds(self):
        select.validate_explicit_constraints({"min_observed_success": 0})
        select.validate_explicit_constraints({"min_observed_success": 1})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"min_observed_success": 1.1})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"min_reputation_confidence": -0.1})

    def test_unsupported_objective_and_prefer_network(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"objective": "bestest"})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"prefer_network": "ethereum"})
        select.validate_explicit_constraints({"objective": "cheapest", "prefer_network": "base"})

    def test_invalid_networks_never_become_all(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"networks": []})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"networks": ["ethereum"]})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"networks": ["solana", "ethereum"]})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"networks": "ethereum"})
        select.validate_explicit_constraints({"networks": ["solana"]})
        select.validate_explicit_constraints({"networks": "base,solana"})
        parsed = select.parse_constraints({"networks": ["ethereum"]})
        self.assertEqual(parsed["rails"], frozenset())
        self.assertIsNotNone(parsed["rails"])

    def test_search_depth_and_probe_cap(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"search_depth": "unlimited"})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_candidates_to_probe": 0})
        select.validate_explicit_constraints({"search_depth": "thorough", "max_candidates_to_probe": 3})

    def test_missing_keys_ok(self):
        select.validate_explicit_constraints({})
        select.validate_explicit_constraints({"need": "weather"})

    def test_nested_constraints_container_is_never_ignored(self):
        for value in (
            {"max_price_usd": 0.01},
            {},
            None,
            "max_price_usd=0.01",
        ):
            with self.assertRaisesRegex(
                ConstraintError, "constraints must be specified as top-level fields"
            ):
                select.validate_explicit_constraints(
                    {"need": "weather", "constraints": value}
                )

        # Mixing both shapes is an error too; no ambiguous precedence.
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints(
                {
                    "need": "weather",
                    "max_price_usd": 0.01,
                    "constraints": {"max_price_usd": 0.02},
                }
            )

    def test_null_present_is_400(self):
        for key in select.EXPLICIT_CONSTRAINT_KEYS:
            with self.assertRaises(ConstraintError, msg=key):
                select.validate_explicit_constraints({key: None})

    def test_absent_is_not_null(self):
        select.validate_explicit_constraints({"need": "weather", "url": "https://x.example"})

    def test_huge_integer_overflow(self):
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_amount_atomic": 2**100})
        with self.assertRaises(ConstraintError):
            select.validate_explicit_constraints({"max_latency_ms": 2**63})

    def test_nl_unresolved_is_not_guessed(self):
        compiled = policy.compile_policy("weather with high reputation")
        self.assertTrue(compiled["unresolved_constraints"])
        self.assertNotIn("min_reputation_score", compiled["interpreted_constraints"])
        select.validate_explicit_constraints({"need": "weather with high reputation"})


class RouteRejectsMalformedConstraints(unittest.TestCase):
    def test_run_probe_400s(self):
        from live402 import route

        code, body = route.run_probe({"url": "https://example.com/x", "networks": ["ethereum"]})
        self.assertEqual(code, 400)
        self.assertEqual(body.get("miss_reason"), "invalid_need")
        code, body = route.run_probe({"need": "weather", "objective": "nope"})
        self.assertEqual(code, 400)
        code, body = route.run_probe({"need": "weather", "max_amount_atomic": True})
        self.assertEqual(code, 400)
        code, body = route.run_probe(
            {"need": "weather", "constraints": {"max_price_usd": 0.01}}
        )
        self.assertEqual(code, 400)
        self.assertEqual(
            body.get("error"), "constraints must be specified as top-level fields"
        )


if __name__ == "__main__":
    unittest.main()
