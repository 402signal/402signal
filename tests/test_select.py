"""Best-of-N selection: objectives, constraints, no rail bias. No network."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import history, payment, probe, select
from tests.v2accept import attach_v2, v2_accept


def _usdc_for_rail(rail: str) -> str:
    return payment.usdc_asset_for_rail(rail) or payment.USDC_BASE


def _payto_for_rail(rail: str) -> str:
    if rail == "solana":
        return payment.DEFAULT_PAYTO_SOLANA
    if rail == "algorand":
        return payment.DEFAULT_PAYTO_ALGORAND
    return "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"


def _network_for_rail(rail: str) -> str:
    if rail == "solana":
        return payment.SOLANA_MAINNET
    if rail == "algorand":
        return payment.ALGORAND_MAINNET
    return payment.BASE_CAIP2


def _hist(success_7d=None, n_7d=0, success_24h=None, n_24h=0):
    return {
        "success_7d": success_7d,
        "n_7d": n_7d,
        "success_24h": success_24h,
        "n_24h": n_24h,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
    }


_UNSET = object()


def _hit(
    url="https://a.example/x",
    rail="base",
    live=True,
    pay_to=_UNSET,
    amount=10000,
    latency=10,
    invocable=True,
    history=None,
    **extra,
):
    if pay_to is _UNSET:
        pay_to = _payto_for_rail(rail)
    asset = extra.pop("asset", None)
    accepts = extra.pop("accepts", None)
    if asset is None and accepts is None:
        asset = _usdc_for_rail(rail)
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
    if asset is not None:
        row["asset"] = asset
    if accepts is None and (amount is not None or asset):
        accepts = [
            v2_accept(
                _network_for_rail(rail),
                asset or _usdc_for_rail(rail),
                amount,
                pay_to,
            )
        ]
    if accepts is not None:
        row["accepts"] = accepts
    row.update(extra)
    return attach_v2(row)


class ParseTests(unittest.TestCase):
    def test_default_objective_is_best(self):
        self.assertEqual(select.parse_objective(None), "best")
        self.assertEqual(select.parse_objective(""), "best")
        self.assertEqual(select.parse_objective("unknown"), "best")
        self.assertEqual(select.parse_objective("BEST"), "best")
        self.assertEqual(select.parse_objective("cheapest"), "cheapest")
        self.assertEqual(select.parse_objective("fastest"), "fastest")
        self.assertEqual(select.parse_objective("most_reliable"), "most_reliable")
        self.assertEqual(
            select.OBJECTIVES,
            (
                "best",
                "cheapest",
                "fastest",
                "most_reliable",
                "lowest_total_cost",
                "fastest_settlement",
            ),
        )

    def test_parse_constraints_invalid_is_unconstrained(self):
        empty = select.parse_constraints({})
        self.assertIsNone(empty["max_amount_atomic"])
        self.assertIsNone(empty["max_price_usd"])
        self.assertIsNone(empty["max_latency_ms"])
        self.assertIsNone(empty["max_probe_latency_ms"])
        self.assertIsNone(empty["max_service_latency_ms"])
        self.assertIsNone(empty["min_observations"])
        self.assertFalse(empty["require_invocable"])
        self.assertIsNone(empty["rails"])
        self.assertEqual(empty["unmeasured"], ())
        bad = select.parse_constraints(
            {"max_amount_atomic": -1, "max_latency_ms": "nope", "networks": [], "max_price_usd": -1}
        )
        self.assertIsNone(bad["max_amount_atomic"])
        self.assertIsNone(bad["max_price_usd"])
        self.assertIsNone(bad["max_latency_ms"])
        self.assertEqual(bad["rails"], frozenset())
        ok = select.parse_constraints(
            {
                "max_amount_atomic": 10000,
                "max_price_usd": "0.05",
                "max_latency_ms": "50",
                "require_invocable": True,
                "networks": ["solana", "ethereum", "base"],
            }
        )
        self.assertEqual(ok["max_amount_atomic"], 10000)
        self.assertAlmostEqual(ok["max_price_usd"], 0.05)
        self.assertEqual(ok["max_latency_ms"], 50)
        self.assertEqual(ok["max_probe_latency_ms"], 50)
        self.assertTrue(ok["require_invocable"])
        self.assertEqual(ok["rails"], frozenset({"solana", "base"}))

    def test_max_latency_ms_maps_to_probe_latency(self):
        cons = select.parse_constraints({"max_latency_ms": 300})
        self.assertEqual(cons["max_latency_ms"], 300)
        self.assertEqual(cons["max_probe_latency_ms"], 300)
        self.assertIsNone(cons["max_service_latency_ms"])
        explicit = select.parse_constraints(
            {"max_latency_ms": 900, "max_probe_latency_ms": 120}
        )
        self.assertEqual(explicit["max_probe_latency_ms"], 120)
        self.assertEqual(explicit["max_latency_ms"], 900)


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

    def test_thin_perfect_does_not_beat_mature_almost_perfect_on_best(self):
        thin = _hit(
            url="https://thin.example/x",
            history=_hist(success_7d=1.0, n_7d=3),
            latency=10,
            amount=10000,
        )
        mature = _hit(
            url="https://mature.example/x",
            history=_hist(success_7d=0.997, n_7d=400),
            latency=10,
            amount=10000,
        )
        self.assertEqual(select.reliability(thin), 1.0)
        self.assertEqual(select.mature_reliability(thin), None)
        self.assertEqual(select.weak_reliability(thin), 1.0)
        self.assertAlmostEqual(select.mature_reliability(mature), 0.997)
        self.assertIsNone(select.weak_reliability(mature))
        winner = select.pick_winner([thin, mature], "best", None)
        self.assertIs(winner, mature)
        winner_rel = select.pick_winner([thin, mature], "most_reliable", None)
        self.assertIs(winner_rel, mature)

    def test_weak_reliability_is_last_tie_break_on_best(self):
        weak_high = _hit(
            url="https://weak-high.example/x",
            history=_hist(success_7d=1.0, n_7d=4),
            latency=10,
            amount=10000,
        )
        weak_low = _hit(
            url="https://weak-low.example/x",
            history=_hist(success_7d=0.5, n_7d=4),
            latency=10,
            amount=10000,
        )
        winner = select.pick_winner([weak_low, weak_high], "best", None)
        self.assertIs(winner, weak_high)
        unknown = _hit(
            url="https://unk.example/x",
            history=_hist(),
            latency=10,
            amount=10000,
        )
        winner_unk = select.pick_winner([unknown, weak_high], "best", None)
        self.assertIs(winner_unk, weak_high)

    def test_enough_evidence_best_vs_comparison(self):
        one = _hit(url="https://one.example/x")
        two = _hit(url="https://two.example/x")
        flipped = _hit(url="https://flip.example/x", payTo_changed=True)
        self.assertTrue(select.enough_evidence([one], "best", None))
        self.assertFalse(select.enough_evidence([flipped], "best", None))
        self.assertTrue(select.enough_evidence([one], "cheapest", None))
        self.assertTrue(select.enough_evidence([one, two], "cheapest", None))
        self.assertTrue(select.enough_evidence([one], "fastest", None))
        self.assertTrue(select.enough_evidence([one], "most_reliable", None))
        self.assertFalse(select.enough_evidence([], "best", None))

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

    def test_prefer_network_is_not_a_hard_filter(self):
        cons = select.parse_constraints({"prefer_network": "solana"})
        self.assertIsNone(cons["rails"])
        base = _hit(url="https://base-only.example/x", rail="base")
        algo = _hit(url="https://algo-only.example/x", rail="algorand")
        self.assertTrue(select.passes_constraints(base, cons))
        self.assertTrue(select.passes_constraints(algo, cons))
        self.assertIs(select.pick_winner([base, algo], "best", cons), base)
        merged = select.parse_constraints({"prefer_network": "solana", "networks": ["base"]})
        self.assertEqual(merged["rails"], frozenset({"base"}))
        self.assertFalse(select.passes_constraints(algo, merged))
        self.assertTrue(select.passes_constraints(base, merged))

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

    def test_payto_changed_is_not_selectable_without_opt_in(self):
        flipped = _hit(url="https://flip.example/x", payTo_pending=True, payTo_changed=True, risk=["payTo_changed"])
        self.assertTrue(select.passes_constraints(flipped, {}))
        self.assertIsNone(select.pick_winner([flipped], "best", None))
        self.assertIs(
            select.pick_winner([flipped], "best", {"accept_payTo_change": True}),
            flipped,
        )
        claimed_mismatch = _hit(url="https://claim.example/x", payTo_changed=True)
        self.assertIs(select.pick_winner([claimed_mismatch], "best", None), claimed_mismatch)

    def test_all_changed_window_empty_unless_opt_in(self):
        a = _hit(url="https://a.example/x", payTo_pending=True)
        b = _hit(url="https://b.example/x", payTo_pending=True)
        stable = _hit(url="https://stable.example/x")
        self.assertIsNone(select.pick_winner([a, b], "best", None))
        self.assertIs(select.pick_winner([a, b, stable], "best", None), stable)
        self.assertIs(select.pick_winner([a, b], "best", {"accept_payTo_change": True}), a)

    def test_unknown_probe_latency_fails_closed(self):
        unknown = _hit(url="https://unk-lat.example/x", latency=None)
        unknown.pop("latency_ms", None)
        known = _hit(url="https://ok-lat.example/x", latency=40)
        cons = select.parse_constraints({"max_probe_latency_ms": 100})
        self.assertIsNone(select.latency_ms(unknown))
        self.assertFalse(select.passes_constraints(unknown, cons))
        self.assertTrue(select.passes_constraints(known, cons))

    def test_service_latency_does_not_use_probe_rtt(self):
        fast_probe = _hit(
            url="https://fast-probe.example/x",
            latency=5,
            history=_hist(),
        )
        fast_probe["history"]["p50_latency_ms"] = None
        measured = _hit(
            url="https://svc.example/x",
            latency=80,
            history=_hist(n_7d=10, success_7d=0.9),
        )
        measured["history"]["p50_latency_ms"] = 40
        cons = select.parse_constraints({"max_service_latency_ms": 50})
        self.assertIsNone(select.service_latency_ms(fast_probe))
        self.assertEqual(select.latency_ms(fast_probe), 5)
        self.assertFalse(select.passes_constraints(fast_probe, cons))
        self.assertTrue(select.passes_constraints(measured, cons))
        too_slow = _hit(
            url="https://slow-svc.example/x",
            latency=5,
            history=_hist(n_7d=10, success_7d=0.9),
        )
        too_slow["history"]["p50_latency_ms"] = 90
        self.assertFalse(select.passes_constraints(too_slow, cons))

    def test_min_observations_unknown_fails_closed(self):
        none = _hit(url="https://none-obs.example/x", history=None)
        none["history"] = None
        zero = _hit(url="https://zero-obs.example/x", history=_hist())
        enough = _hit(
            url="https://enough-obs.example/x",
            history=_hist(n_7d=12, success_7d=0.8),
        )
        cons = select.parse_constraints({"min_observations": 10})
        self.assertFalse(select.passes_constraints(none, cons))
        self.assertFalse(select.passes_constraints(zero, cons))
        self.assertTrue(select.passes_constraints(enough, cons))

    def test_unknown_reputation_and_success_fail_closed(self):
        unknown = _hit(url="https://rep-unk.example/x", history=_hist())
        known = _hit(
            url="https://rep-ok.example/x",
            history=_hist(n_7d=40, success_7d=0.99),
        )
        cons_success = select.parse_constraints({"min_observed_success": 0.8})
        self.assertEqual(cons_success["unmeasured"], ())
        self.assertFalse(select.passes_constraints(unknown, cons_success))
        self.assertTrue(select.passes_constraints(known, cons_success))
        cons_rep = select.parse_constraints({"min_reputation_score": 0.2})
        self.assertEqual(cons_rep["unmeasured"], ())
        self.assertTrue(select.passes_constraints(known, cons_rep))
        cons_settle = select.parse_constraints({"max_settlement_latency_ms": 5000})
        self.assertEqual(cons_settle["unmeasured"], ())
        # Solana finality is unknown; Base has a protocol_reference figure.
        sol = _hit(url="https://sol-settle.example/x", rail="solana", history=_hist(n_7d=40, success_7d=0.99))
        base = _hit(url="https://base-settle.example/x", rail="base", history=_hist(n_7d=40, success_7d=0.99))
        self.assertFalse(select.passes_constraints(sol, cons_settle))
        self.assertTrue(select.passes_constraints(base, cons_settle))
        cons_tight = select.parse_constraints({"max_settlement_latency_ms": 1000})
        self.assertFalse(select.passes_constraints(base, cons_tight))


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
        self.assertIsNone(rows[0]["success_7d"])
        self.assertIsNot(rows[0]["success_7d"], 0.0)
        self.assertEqual(rows[0]["n_7d"], 0)
        self.assertEqual(rows[1]["success_7d"], 0.5)
        self.assertEqual(rows[1]["n_7d"], 3)
        self.assertTrue(rows[1]["selected"])
        self.assertFalse(rows[0]["selected"])
        for key in (
            "url",
            "rail",
            "amount_atomic",
            "latency_ms",
            "success_7d",
            "n_7d",
            "readiness",
            "live",
            "invocable",
            "selected",
        ):
            self.assertIn(key, rows[0])
        self.assertNotIn("reliability", rows[0])

    def test_comparison_exposes_n_so_thin_perfect_is_not_400_of_400(self):
        thin = _hit(
            url="https://thin.example/x",
            history=_hist(success_7d=1.0, n_7d=3),
            latency=10,
            amount=10000,
        )
        mature = _hit(
            url="https://mature.example/x",
            history=_hist(success_7d=0.995, n_7d=400),
            latency=10,
            amount=10000,
        )
        winner = select.pick_winner([thin, mature], "best", None)
        self.assertIs(winner, mature)
        winner_rel = select.pick_winner([thin, mature], "most_reliable", None)
        self.assertIs(winner_rel, mature)
        rows = select.comparison([thin, mature], winner)
        by_url = {row["url"]: row for row in rows}
        self.assertEqual(by_url[thin["url"]]["success_7d"], 1.0)
        self.assertEqual(by_url[thin["url"]]["n_7d"], 3)
        self.assertAlmostEqual(by_url[mature["url"]]["success_7d"], 0.995)
        self.assertEqual(by_url[mature["url"]]["n_7d"], 400)
        self.assertTrue(by_url[mature["url"]]["selected"])
        self.assertFalse(by_url[thin["url"]]["selected"])
        two = _hit(
            url="https://two.example/x",
            history=_hist(success_7d=1.0, n_7d=2),
        )
        thin_row = select.comparison([two], two)[0]
        self.assertIsNone(thin_row["success_7d"])
        self.assertEqual(thin_row["n_7d"], 2)


class RouteNeedSelectTests(unittest.TestCase):
    """Best-of-N wiring through route_need. Mocked probes, no network."""

    def setUp(self):
        self._prev_db = os.environ.get("LIVE402_HISTORY_DB")
        fd, self._db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_HISTORY_DB"] = self._db
        history.reset()

    def tearDown(self):
        history.reset()
        if self._prev_db is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev_db
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass

    def _seed_history(self, url, n_ok=0, n_fail=0):
        snap_ok = {
            "live": True,
            "status": 402,
            "latency_ms": 10,
            "has_402_challenge": True,
            "payTo": "0xabc",
            "probed_at": probe.now_iso(),
        }
        snap_fail = {
            "live": False,
            "status": None,
            "latency_ms": 10,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": "no_402_envelope",
            "probed_at": probe.now_iso(),
        }
        for _ in range(n_ok):
            history.record_probe(url, dict(snap_ok))
        for _ in range(n_fail):
            history.record_probe(url, dict(snap_fail))

    def _item(self, url, description="weather forecast", network="base", amount="10000", pay_to=None):
        if pay_to is None:
            pay_to = _payto_for_rail(network if network in {"base", "solana", "algorand"} else "base")
        return {
            "url": url,
            "description": description,
            "accepts": [
                v2_accept(
                    _network_for_rail(network if network in {"base", "solana", "algorand"} else "base")
                    if network in {"base", "solana", "algorand"}
                    else network,
                    _usdc_for_rail(network),
                    amount,
                    pay_to,
                )
            ],
        }

    def _live(self, url, amount=10000, latency=10, pay_to=None, changed=False, rail="base"):
        if pay_to is None:
            pay_to = _payto_for_rail(rail)
        row = {
            "live": True,
            "url": url,
            "payTo": pay_to,
            "amount": amount,
            "asset": _usdc_for_rail(rail),
            "latency_ms": latency,
            "invocable": False,
            "payTo_changed": bool(changed),
            "probed_at": probe.now_iso(),
            "readiness": "payable",
            "rail": rail,
            "status": 402,
            "has_402_challenge": True,
            "accepts": [
                v2_accept(
                    _network_for_rail(rail),
                    _usdc_for_rail(rail),
                    amount,
                    pay_to,
                )
            ],
        }
        if changed:
            row["risk"] = ["payTo_changed"]
        return attach_v2(row)

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

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return dict(by_url[url])

        result = self._route([dear, cheap], fake_probe, objective="cheapest")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), cheap["url"])
        self.assertEqual(result.get("objective"), "cheapest")
        self.assertGreaterEqual(result.get("tried"), 2)
        self.assertGreaterEqual(result.get("candidates_probed") or 0, 2)
        self.assertFalse(result.get("probe_budget_exhausted"))
        compared = result.get("compared") or []
        self.assertEqual(len(compared), 2)
        selected = [row for row in compared if row.get("selected")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].get("url"), cheap["url"])
        self.assertIsNone(compared[0].get("success_7d"))
        self.assertIsNot(compared[0].get("success_7d"), 0.0)
        self.assertEqual(compared[0].get("n_7d"), 0)
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        self.assertTrue(result.get("candidate_evaluation_complete"))

    def test_fastest_wins_lower_latency(self):
        slow = self._item("https://slow.example/weather")
        fast = self._item("https://fast.example/weather")
        by_url = {
            slow["url"]: self._live(slow["url"], amount=1000, latency=80),
            fast["url"]: self._live(fast["url"], amount=9000, latency=4),
        }

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
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

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return dict(by_url[url])

        cons = select.parse_constraints({"max_amount_atomic": 5000})
        result = self._route([dear, dead], fake_probe, constraints=cons)
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        self.assertEqual(result.get("objective"), "best")
        self.assertGreaterEqual(result.get("tried"), 2)
        compared = result.get("compared") or []
        self.assertGreaterEqual(len(compared), 2)
        self.assertFalse(any(row.get("selected") for row in compared))
        self.assertEqual(result.get("stop_reason"), "constraints_unmet")
        self.assertTrue(result.get("candidate_evaluation_complete"))

    def test_default_objective_first_equal_live_wins(self):
        first = self._item("https://first.example/weather", network="solana")
        second = self._item("https://second.example/weather", network="base")
        by_url = {
            first["url"]: self._live(first["url"], amount=10000, latency=10, rail="solana"),
            second["url"]: self._live(second["url"], amount=10000, latency=10, rail="base"),
        }

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return dict(by_url[url])

        result = self._route([first, second], fake_probe)
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), first["url"])
        self.assertEqual(result.get("objective"), "best")
        self.assertEqual(result.get("rail"), "solana")
        self.assertGreaterEqual(result.get("tried"), 1)
        self.assertGreaterEqual(result.get("candidates_probed") or result.get("tried"), 1)

    def test_best_finishes_first_tranche_before_selecting(self):
        fast = self._item("https://fast.example/weather")
        slow_a = self._item("https://slow-a.example/weather")
        slow_b = self._item("https://slow-b.example/weather")
        started = []

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            started.append(url)
            if "fast" in url:
                return self._live(url, latency=5)
            time.sleep(0.12)
            return self._live(url, latency=80)

        t0 = time.monotonic()
        result = self._route([fast, slow_a, slow_b], fake_probe, objective="best")
        elapsed = time.monotonic() - t0
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), fast["url"])
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertEqual(set(started), {fast["url"], slow_a["url"], slow_b["url"]})
        self.assertFalse(result.get("probe_budget_exhausted"))
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        self.assertTrue(result.get("candidate_evaluation_complete"))

    def test_best_keeps_first_ranked_when_second_returns_first(self):
        first = self._item("https://first-slow.example/weather")
        second = self._item("https://second-fast.example/weather")

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "first-slow" in url:
                time.sleep(0.08)
            return self._live(url, amount=10000, latency=10)

        result = self._route([first, second], fake_probe)
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), first["url"])
        self.assertGreaterEqual(result.get("candidates_probed") or 0, 2)

    def test_cheapest_waits_for_in_flight_cheaper(self):
        dear = self._item("https://dear-cmp.example/weather", amount="9000")
        mid = self._item("https://mid-cmp.example/weather", amount="1000")
        extra = self._item("https://extra-cmp.example/weather", amount="500")

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "extra-cmp" in url:
                time.sleep(0.15)
                return self._live(url, amount=500, latency=10)
            if "dear-cmp" in url:
                return self._live(url, amount=9000, latency=10)
            return self._live(url, amount=1000, latency=10)

        t0 = time.monotonic()
        result = self._route([dear, mid, extra], fake_probe, objective="cheapest")
        elapsed = time.monotonic() - t0
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), extra["url"])
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertFalse(result.get("probe_budget_exhausted"))
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        self.assertTrue(result.get("candidate_evaluation_complete"))

    def test_fastest_waits_for_in_flight_faster(self):
        slow = self._item("https://slow-cmp.example/weather")
        mid = self._item("https://mid-fast.example/weather")
        extra = self._item("https://extra-fast.example/weather")

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "extra-fast" in url:
                time.sleep(0.15)
                return self._live(url, amount=9000, latency=4)
            if "slow-cmp" in url:
                return self._live(url, amount=1000, latency=80)
            return self._live(url, amount=2000, latency=40)

        result = self._route([slow, mid, extra], fake_probe, objective="fastest")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), extra["url"])
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertEqual(result.get("stop_reason"), "winner_selected")

    def test_most_reliable_waits_for_in_flight_mature_stronger(self):
        weak = self._item("https://weak-rel.example/weather")
        mid = self._item("https://mid-rel.example/weather")
        strong = self._item("https://strong-rel.example/weather")
        self._seed_history(weak["url"], n_ok=5, n_fail=5)
        self._seed_history(mid["url"], n_ok=5, n_fail=5)
        self._seed_history(strong["url"], n_ok=10, n_fail=0)

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "strong-rel" in url:
                time.sleep(0.15)
            return self._live(url, amount=10000, latency=10)

        result = self._route([weak, mid, strong], fake_probe, objective="most_reliable")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), strong["url"])
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        compared = {row["url"]: row for row in (result.get("compared") or [])}
        self.assertEqual(compared[strong["url"]]["n_7d"], 10)
        self.assertEqual(compared[strong["url"]]["success_7d"], 1.0)
        self.assertEqual(compared[weak["url"]]["n_7d"], 10)
        self.assertEqual(compared[weak["url"]]["success_7d"], 0.5)

    def test_best_waits_for_in_flight_that_beats_under_cmp_best(self):
        payable = self._item("https://payable-first.example/weather")
        also = self._item("https://also-pay.example/weather")
        invocable = self._item("https://invocable-slow.example/weather")

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "invocable-slow" in url:
                time.sleep(0.15)
                row = self._live(url, amount=10000, latency=20)
                row["invocable"] = True
                row["readiness"] = "invocable"
                return row
            return self._live(url, amount=10000, latency=5)

        result = self._route([payable, also, invocable], fake_probe, objective="best")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), invocable["url"])
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertTrue(result.get("invocable"))
        self.assertEqual(result.get("stop_reason"), "winner_selected")

    def test_equal_live_hits_preserve_original_relevance_rank(self):
        first = self._item("https://rank-a.example/weather", network="base")
        second = self._item("https://rank-b.example/weather", network="solana")
        third = self._item("https://rank-c.example/weather", network="algorand")

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            if "rank-c" in url:
                time.sleep(0.02)
                return self._live(url, amount=10000, latency=10, rail="algorand")
            if "rank-b" in url:
                return self._live(url, amount=10000, latency=10, rail="solana")
            time.sleep(0.08)
            return self._live(url, amount=10000, latency=10, rail="base")

        result = self._route([first, second, third], fake_probe, objective="best")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), first["url"])
        self.assertEqual(result.get("rail"), "base")
        self.assertEqual(result.get("candidates_probed"), 3)
        self.assertEqual(result.get("stop_reason"), "winner_selected")

    def test_concurrent_tranche_fail_closed_no_unverified_winner(self):
        items = [self._item("https://dead%d.example/weather" % i) for i in range(4)]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            time.sleep(0.02)
            return self._dead(url)

        result = self._route(items, fake_probe)
        self.assertFalse(result.get("live"))
        self.assertIsNone(result.get("url"))
        self.assertNotEqual(result.get("miss_reason"), "no_candidates")
        self.assertGreaterEqual(result.get("candidates_probed") or 0, 1)
        self.assertGreaterEqual(result.get("discovery_matches") or 0, 4)
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("stop_reason"), "candidate_set_exhausted")
        self.assertTrue(result.get("candidate_evaluation_complete"))
        self.assertNotEqual(result.get("miss_reason"), "probe_limit_reached")

    def test_probe_budget_exhausted_when_untested_remain(self):
        items = [self._item("https://n%d.example/weather" % i) for i in range(8)]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return self._dead(url)

        result = self._route(items, fake_probe, deadline=time.monotonic())
        self.assertFalse(result.get("live"))
        self.assertIsNone(result.get("url"))
        self.assertEqual(result.get("miss_reason"), "probe_budget_exhausted")
        self.assertTrue(result.get("probe_budget_exhausted"))
        self.assertEqual(result.get("stop_reason"), "probe_budget_exhausted")
        self.assertFalse(result.get("candidate_evaluation_complete"))
        self.assertGreater(result.get("candidates_considered"), result.get("candidates_probed"))
        self.assertNotEqual(result.get("miss_reason"), "no_candidates")
        self.assertGreaterEqual(result.get("discovery_matches"), 8)

    def test_probe_limit_reached_when_ranked_remain_and_budget_open(self):
        items = [self._item("https://lim%d.example/weather" % i) for i in range(14)]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return self._dead(url)

        result = self._route(items, fake_probe)
        self.assertFalse(result.get("live"))
        self.assertIsNone(result.get("url"))
        self.assertEqual(result.get("miss_reason"), "probe_limit_reached")
        self.assertEqual(result.get("stop_reason"), "probe_limit_reached")
        self.assertEqual(result.get("probe_ceiling"), probe.STANDARD_PROBE_CAP)
        self.assertEqual(result.get("candidates_probed"), result.get("probe_ceiling"))
        self.assertLess(result.get("candidates_probed"), probe.PROBE_CEILING)
        self.assertEqual(result.get("discovery_matches"), 14)
        self.assertEqual(result.get("candidates_discovered"), 14)
        self.assertGreater(result.get("candidates_considered"), result.get("candidates_probed"))
        self.assertFalse(result.get("candidate_evaluation_complete"))
        self.assertFalse(result.get("probe_budget_exhausted"))
        self.assertNotEqual(result.get("miss_reason"), "no_candidates")

    def test_sixth_candidate_reached_when_first_five_fail_without_twenty_probes(self):
        items = [self._item("https://d%d.example/weather" % i) for i in range(1, 13)]
        started = []

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            started.append(url)
            if "d6.example" in url:
                return self._live(url, amount=10000, latency=10)
            return self._dead(url)

        result = self._route(items, fake_probe)
        self.assertTrue(result.get("live"))
        self.assertIn("d6.example", result.get("url") or "")
        self.assertIn("https://d6.example/weather", started)
        self.assertEqual(result.get("candidates_probed"), 6)
        compared_urls = [row.get("url") for row in (result.get("compared") or [])]
        self.assertIn(result.get("url"), compared_urls)
        self.assertLessEqual(len(compared_urls), select.COMPARED_CAP)
        self.assertLess(result.get("candidates_probed"), 20)
        self.assertLess(result.get("candidates_probed"), probe.PROBE_CEILING)
        self.assertEqual(result.get("probe_ceiling"), probe.STANDARD_PROBE_CAP)
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        self.assertFalse(result.get("candidate_evaluation_complete"))
        self.assertEqual(result.get("candidates_discovered"), 12)
        self.assertEqual(result.get("candidates_considered"), 12)

    def test_max_candidates_to_probe_is_capped_at_server_ceiling(self):
        items = [self._item("https://cap%d.example/weather" % i) for i in range(30)]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return self._dead(url)

        result = self._route(
            items, fake_probe, max_candidates_to_probe=50, search_depth="thorough"
        )
        self.assertEqual(result.get("probe_ceiling"), probe.PROBE_CEILING)
        self.assertEqual(result.get("candidates_probed"), probe.PROBE_CEILING)
        self.assertLessEqual(result.get("candidates_probed"), 20)
        self.assertEqual(result.get("stop_reason"), "probe_limit_reached")

    def test_winner_selected_with_untested_ranked_is_not_evaluation_complete(self):
        items = [self._item("https://win%d.example/weather" % i) for i in range(8)]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            return self._live(url, amount=10000, latency=10)

        result = self._route(items, fake_probe, objective="best")
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("stop_reason"), "winner_selected")
        self.assertFalse(result.get("candidate_evaluation_complete"))
        self.assertEqual(result.get("candidates_probed"), probe.FIRST_TRANCHE)
        self.assertEqual(result.get("discovery_matches"), 8)
        self.assertFalse(result.get("probe_budget_exhausted"))
        self.assertNotEqual(result.get("stop_reason"), "probe_limit_reached")

    def test_empty_ranked_is_no_candidates_not_budget(self):
        result = self._route([], lambda *a, **k: self._dead("https://x.example/weather"))
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "no_candidates")
        self.assertFalse(result.get("probe_budget_exhausted"))
        self.assertEqual(result.get("discovery_matches"), 0)
        self.assertEqual(result.get("stop_reason"), "candidate_set_exhausted")
        self.assertTrue(result.get("candidate_evaluation_complete"))


if __name__ == "__main__":
    unittest.main()
