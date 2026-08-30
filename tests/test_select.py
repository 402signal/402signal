"""Best-of-N selection: objectives, constraints, no rail bias. No network."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import probe, select


def _hist(success_7d=None, n_7d=0, success_24h=None, n_24h=0):
    return {
        "success_7d": success_7d,
        "n_7d": n_7d,
        "success_24h": success_24h,
        "n_24h": n_24h,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
    }


def _hit(
    url="https://a.example/x",
    rail="base",
    live=True,
    pay_to="0xabc",
    amount=10000,
    latency=10,
    invocable=True,
    history=None,
    **extra,
):
    row = {
        "url": url,
        "rail": rail,
        "live": live,
        "payTo": pay_to,
        "invocable": invocable,
        "latency_ms": latency,
        "amount": amount,
        "readiness": extra.pop(
            "readiness",
            "invocable" if invocable else ("payable" if live and pay_to else "discovered"),
        ),
        "history": history if history is not None else _hist(),
    }
    row.update(extra)
    return row


class ParseTests(unittest.TestCase):
    def test_default_objective_is_best(self):
        self.assertEqual(select.parse_objective(None), "best")
        self.assertEqual(select.parse_objective(""), "best")
        self.assertEqual(select.parse_objective("unknown"), "best")
        self.assertEqual(select.parse_objective("BEST"), "best")
        self.assertEqual(select.parse_objective("cheapest"), "cheapest")
        self.assertEqual(select.parse_objective("fastest"), "fastest")
        self.assertEqual(select.parse_objective("most_reliable"), "most_reliable")
        self.assertEqual(select.OBJECTIVES, ("best", "cheapest", "fastest", "most_reliable"))

    def test_parse_constraints_invalid_is_unconstrained(self):
        empty = select.parse_constraints({})
        self.assertIsNone(empty["max_amount_atomic"])
        self.assertIsNone(empty["max_latency_ms"])
        self.assertFalse(empty["require_invocable"])
        self.assertIsNone(empty["rails"])
        bad = select.parse_constraints(
            {"max_amount_atomic": -1, "max_latency_ms": "nope", "networks": []}
        )
        self.assertIsNone(bad["max_amount_atomic"])
        self.assertIsNone(bad["max_latency_ms"])
        self.assertIsNone(bad["rails"])
        ok = select.parse_constraints(
            {
                "max_amount_atomic": 10000,
                "max_latency_ms": "50",
                "require_invocable": True,
                "networks": ["solana", "ethereum", "base"],
            }
        )
        self.assertEqual(ok["max_amount_atomic"], 10000)
        self.assertEqual(ok["max_latency_ms"], 50)
        self.assertTrue(ok["require_invocable"])
        self.assertEqual(ok["rails"], frozenset({"solana", "base"}))


class ObjectivePickTests(unittest.TestCase):
    def test_cheapest_picks_lower_amount(self):
        cheap = _hit(url="https://cheap.example/x", amount=1000, latency=40)
        dear = _hit(url="https://dear.example/x", amount=9000, latency=5)
        winner = select.pick_winner([dear, cheap], "cheapest", None)
        self.assertIs(winner, cheap)

    def test_fastest_picks_lower_latency(self):
        slow = _hit(url="https://slow.example/x", amount=1000, latency=80)
        fast = _hit(url="https://fast.example/x", amount=9000, latency=4)
        winner = select.pick_winner([slow, fast], "fastest", None)
        self.assertIs(winner, fast)

    def test_most_reliable_picks_higher_success_7d(self):
        low = _hit(
            url="https://low.example/x",
            history=_hist(success_7d=0.4, n_7d=10),
        )
        high = _hit(
            url="https://high.example/x",
            history=_hist(success_7d=0.9, n_7d=10),
        )
        winner = select.pick_winner([low, high], "most_reliable", None)
        self.assertIs(winner, high)

    def test_unknown_reliability_does_not_beat_known_half(self):
        unknown = _hit(url="https://unknown.example/x", history=_hist())
        known = _hit(
            url="https://known.example/x",
            history=_hist(success_7d=0.5, n_7d=3),
        )
        self.assertIsNone(select.reliability(unknown))
        self.assertEqual(select.reliability(known), 0.5)
        winner = select.pick_winner([unknown, known], "most_reliable", None)
        self.assertIs(winner, known)

    def test_no_rail_bias_first_equal_live_wins(self):
        orders = (
            ("base", "solana", "algorand"),
            ("solana", "algorand", "base"),
            ("algorand", "base", "solana"),
        )
        for rails in orders:
            hits = [
                _hit(
                    url="https://%s.example/x" % rail,
                    rail=rail,
                    amount=10000,
                    latency=10,
                    invocable=True,
                    history=_hist(),
                )
                for rail in rails
            ]
            winner = select.pick_winner(hits, "best", None)
            self.assertIs(winner, hits[0], msg="first rail=%s" % rails[0])
            self.assertEqual(winner["rail"], rails[0])


class ConstraintTests(unittest.TestCase):
    def test_unknown_amount_fails_max_amount_atomic(self):
        unknown = _hit(url="https://unk.example/x", amount=None)
        known = _hit(url="https://ok.example/x", amount=5000)
        unknown.pop("amount")
        cons = {"max_amount_atomic": 10000, "max_latency_ms": None, "require_invocable": False, "rails": None}
        self.assertIsNone(select.amount_atomic(unknown))
        self.assertFalse(select.passes_constraints(unknown, cons))
        self.assertTrue(select.passes_constraints(known, cons))
        self.assertIs(select.pick_winner([unknown, known], "best", cons), known)
        self.assertIsNone(select.pick_winner([unknown], "best", cons))

    def test_require_invocable_drops_payable_without_schema(self):
        payable = _hit(
            url="https://pay.example/x",
            invocable=False,
            readiness="payable",
        )
        invocable = _hit(url="https://inv.example/x", invocable=True)
        cons = {"require_invocable": True}
        self.assertFalse(select.passes_constraints(payable, cons))
        self.assertTrue(select.passes_constraints(invocable, cons))
        self.assertIs(select.pick_winner([payable, invocable], "best", cons), invocable)

    def test_rails_solana_drops_base_and_algorand(self):
        base = _hit(url="https://base.example/x", rail="base")
        sol = _hit(url="https://sol.example/x", rail="solana")
        algo = _hit(url="https://algo.example/x", rail="algorand")
        cons = select.parse_constraints({"networks": ["solana"]})
        self.assertEqual(cons["rails"], frozenset({"solana"}))
        self.assertFalse(select.passes_constraints(base, cons))
        self.assertFalse(select.passes_constraints(algo, cons))
        self.assertTrue(select.passes_constraints(sol, cons))
        self.assertIs(select.pick_winner([base, algo, sol], "best", cons), sol)
        self.assertIs(
            select.pick_winner(
                [base, algo, sol],
                "best",
                {"rails": frozenset({"solana"})},
            ),
            sol,
        )

    def test_empty_after_filter_returns_none(self):
        dead = _hit(live=False, pay_to=None, invocable=False)
        no_pay = _hit(live=True, pay_to=None, invocable=False)
        other_rail = _hit(rail="base")
        self.assertIsNone(select.pick_winner([], "best", None))
        self.assertIsNone(select.pick_winner([dead, no_pay], "best", None))
        self.assertIsNone(
            select.pick_winner([other_rail], "best", {"rails": frozenset({"solana"})})
        )

    def test_payto_changed_is_still_selectable(self):
        flipped = _hit(url="https://flip.example/x", payTo_changed=True, risk=["payTo_changed"])
        self.assertTrue(select.passes_constraints(flipped, {}))
        self.assertIs(select.pick_winner([flipped], "best", None), flipped)


class ComparisonTests(unittest.TestCase):
    def test_comparison_unknown_rate_is_none_not_zero(self):
        a = _hit(url="https://a.example/x", rail="base", history=_hist())
        b = _hit(
            url="https://b.example/x",
            rail="solana",
            history=_hist(success_7d=0.5, n_7d=3),
        )
        winner = select.pick_winner([a, b], "most_reliable", None)
        rows = select.comparison([a, b], winner)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["reliability"])
        self.assertIsNot(rows[0]["reliability"], 0.0)
        self.assertEqual(rows[1]["reliability"], 0.5)
        self.assertTrue(rows[1]["selected"])
        self.assertFalse(rows[0]["selected"])
        for key in (
            "url",
            "rail",
            "amount_atomic",
            "latency_ms",
            "reliability",
            "readiness",
            "live",
            "invocable",
            "selected",
        ):
            self.assertIn(key, rows[0])


class RouteNeedSelectTests(unittest.TestCase):
    """Best-of-N wiring through route_need. Mocked probes, no network."""

    def _item(self, url, description="weather forecast", network="base", amount="10000", pay_to="0xabc"):
        return {
            "url": url,
            "description": description,
            "accepts": [{"network": network, "payTo": pay_to, "amount": amount}],
        }

    def _live(self, url, amount=10000, latency=10, pay_to="0xabc", changed=False, rail="base"):
        row = {
            "live": True,
            "url": url,
            "payTo": pay_to,
            "amount": amount,
            "latency_ms": latency,
            "invocable": False,
            "payTo_changed": bool(changed),
            "probed_at": probe.now_iso(),
            "readiness": "payable",
            "rail": rail,
            "status": 402,
            "has_402_challenge": True,
        }
        if changed:
            row["risk"] = ["payTo_changed"]
        return row

    def _dead(self, url, miss="no_402_envelope"):
        return {
            "live": False,
            "url": url,
            "payTo": None,
            "invocable": False,
            "miss_reason": miss,
            "probed_at": probe.now_iso(),
            "status": None,
            "has_402_challenge": False,
        }

    def _route(self, items, fake_probe, need="weather", **kwargs):
        with patch("live402.probe.fetch_discovery", return_value=items), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            return probe.route_need(need, **kwargs)

    def test_cheapest_second_candidate_wins(self):
        dear = self._item("https://dear.example/weather", amount="9000")
        cheap = self._item("https://cheap.example/weather", amount="1000")
        by_url = {
            dear["url"]: self._live(dear["url"], amount=9000, latency=5),
            cheap["url"]: self._live(cheap["url"], amount=1000, latency=40),
        }

        def fake_probe(url, catalog_item=None, deadline=None):
            _ = catalog_item, deadline
            return dict(by_url[url])

        result = self._route([dear, cheap], fake_probe, objective="cheapest")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), cheap["url"])
        self.assertEqual(result.get("objective"), "cheapest")
        self.assertEqual(result.get("tried"), 2)
        compared = result.get("compared") or []
        self.assertEqual(len(compared), 2)
        selected = [row for row in compared if row.get("selected")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].get("url"), cheap["url"])
        self.assertIsNone(compared[0].get("reliability"))
        self.assertIsNot(compared[0].get("reliability"), 0.0)

    def test_fastest_wins_lower_latency(self):
        slow = self._item("https://slow.example/weather")
        fast = self._item("https://fast.example/weather")
        by_url = {
            slow["url"]: self._live(slow["url"], amount=1000, latency=80),
            fast["url"]: self._live(fast["url"], amount=9000, latency=4),
        }

        def fake_probe(url, catalog_item=None, deadline=None):
            _ = catalog_item, deadline
            return dict(by_url[url])

        result = self._route([slow, fast], fake_probe, objective="fastest")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), fast["url"])
        self.assertEqual(result.get("objective"), "fastest")

    def test_max_amount_atomic_only_expensive_is_constraints_unmet(self):
        dear = self._item("https://dear-only.example/weather", amount="9000")
        dead = self._item("https://dead.example/weather", amount="1000")
        by_url = {
            dear["url"]: self._live(dear["url"], amount=9000, latency=10),
            dead["url"]: self._dead(dead["url"]),
        }

        def fake_probe(url, catalog_item=None, deadline=None):
            _ = catalog_item, deadline
            return dict(by_url[url])

        cons = select.parse_constraints({"max_amount_atomic": 5000})
        result = self._route([dear, dead], fake_probe, constraints=cons)
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        self.assertEqual(result.get("objective"), "best")
        self.assertEqual(result.get("tried"), 2)
        compared = result.get("compared") or []
        self.assertEqual(len(compared), 2)
        self.assertFalse(any(row.get("selected") for row in compared))

    def test_default_objective_first_equal_live_wins(self):
        first = self._item("https://first.example/weather", network="solana")
        second = self._item("https://second.example/weather", network="base")
        by_url = {
            first["url"]: self._live(first["url"], amount=10000, latency=10, rail="solana"),
            second["url"]: self._live(second["url"], amount=10000, latency=10, rail="base"),
        }

        def fake_probe(url, catalog_item=None, deadline=None):
            _ = catalog_item, deadline
            return dict(by_url[url])

        result = self._route([first, second], fake_probe)
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), first["url"])
        self.assertEqual(result.get("objective"), "best")
        self.assertEqual(result.get("rail"), "solana")
        self.assertEqual(result.get("tried"), 2)


if __name__ == "__main__":
    unittest.main()
