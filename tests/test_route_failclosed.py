"""P0 /route fail-closed: observed selected_payment, networks lock, compared winner."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import history, payment, policy, probe, route, select
from tests.v2accept import v2_accept


def _usdc(rail: str) -> str:
    return payment.usdc_asset_for_rail(rail) or payment.USDC_BASE


def _payto(rail: str) -> str:
    if rail == "solana":
        return payment.DEFAULT_PAYTO_SOLANA
    if rail == "algorand":
        return payment.DEFAULT_PAYTO_ALGORAND
    return "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"


def _network(rail: str) -> str:
    if rail == "solana":
        return payment.SOLANA_MAINNET
    if rail == "algorand":
        return payment.ALGORAND_MAINNET
    return payment.BASE_CAIP2


def _complete_selected(rail: str, amount=10000) -> dict:
    return {
        "rail": rail,
        "network": _network(rail),
        "asset": _usdc(rail),
        "amount_atomic": amount,
        "display_amount": None,
        "normalized_usd": None,
        "payTo": _payto(rail),
        "facilitator": None,
    }


def _live_observed(url, rail="base", amount=10000, latency=10, invocable=True, schema=True):
    pay_to = _payto(rail)
    envelope = {
        "x402Version": 2,
        "accepts": [v2_accept(_network(rail), _usdc(rail), amount, pay_to)],
    }
    row = {
        "url": url,
        "live": True,
        "status": 402,
        "has_402_challenge": True,
        "challenge_observed": True,
        "payTo": pay_to,
        "latency_ms": latency,
        "envelope": envelope,
        "rail": rail,
        "amount": amount,
        "asset": _usdc(rail),
        "history": {"success_7d": None, "n_7d": 0, "success_24h": None, "n_24h": 0},
    }
    item = {"url": url, "description": "weather forecast"}
    if schema:
        item["inputSchema"] = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
    row = probe.attach_invocable_target(row, item if schema else None, envelope)
    if not invocable:
        row["invocable"] = False
        if row.get("payable"):
            row["miss_reason"] = "no_input_schema"
    return row


def _catalog_algorand_item(url):
    return {
        "url": url,
        "description": "algorand asset lookup",
        "_rail": "algorand",
        "accepts": [
            {
                "scheme": "exact",
                "network": payment.ALGORAND_MAINNET,
                "asset": "31566704",
                "amount": "10000",
                "payTo": payment.DEFAULT_PAYTO_ALGORAND,
                "maxTimeoutSeconds": 60,
            }
        ],
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }


class ObservedNetworksFailClosedTests(unittest.TestCase):
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

    def _item(self, url, rail="base", description="weather forecast"):
        return {
            "url": url,
            "description": description,
            "_rail": rail,
            "accepts": [
                v2_accept(_network(rail), _usdc(rail), 10000, _payto(rail))
            ],
        }

    def _route(self, items, fake_probe, need="weather forecast", **kwargs):
        with patch("live402.probe.fetch_discovery", return_value=items), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            return probe.route_need(need, **kwargs)

    def _probe_by_url(self, by_url):
        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline, kwargs
            return dict(by_url[url])

        return fake_probe

    def _assert_200_winner(self, result, rail):
        self.assertTrue(result.get("live"))
        selected = result.get("selected_payment")
        self.assertIsInstance(selected, dict)
        self.assertEqual(selected.get("rail"), rail)
        self.assertEqual(payment.rail_of_network(selected.get("network")), rail)
        self.assertTrue(select.selected_payment_is_complete(selected))
        compared_urls = [row.get("url") for row in (result.get("compared") or [])]
        self.assertIn(result.get("url"), compared_urls)
        self.assertTrue(any(row.get("selected") for row in (result.get("compared") or [])))

    def test_networks_base_matching_observed_rail_succeeds(self):
        item = self._item("https://wx.example/base-weather", "base")
        hit = _live_observed(item["url"], rail="base")
        cons = select.parse_constraints({"networks": ["base"]})
        result = self._route(
            [item], self._probe_by_url({item["url"]: hit}), constraints=cons
        )
        self._assert_200_winner(result, "base")
        self.assertEqual(result.get("stop_reason"), "winner_selected")

    def test_networks_solana_matching_observed_rail_succeeds(self):
        item = self._item("https://wx.example/sol-weather", "solana")
        hit = _live_observed(item["url"], rail="solana")
        cons = select.parse_constraints({"networks": ["solana"]})
        result = self._route(
            [item], self._probe_by_url({item["url"]: hit}), constraints=cons
        )
        self._assert_200_winner(result, "solana")

    def test_networks_algorand_matching_observed_rail_succeeds(self):
        item = self._item("https://wx.example/algo-weather", "algorand")
        hit = _live_observed(item["url"], rail="algorand")
        cons = select.parse_constraints({"networks": ["algorand"]})
        result = self._route(
            [item], self._probe_by_url({item["url"]: hit}), constraints=cons
        )
        self._assert_200_winner(result, "algorand")

    def test_valid_matching_observed_rail_succeeds_without_networks(self):
        item = self._item("https://wx.example/any-weather", "base")
        hit = _live_observed(item["url"], rail="base")
        result = self._route([item], self._probe_by_url({item["url"]: hit}))
        self._assert_200_winner(result, "base")

    def test_catalog_claim_requested_rail_observed_other_is_typed_miss(self):
        url = "https://x402.twit.sh/tweets/search"
        item = _catalog_algorand_item(url)
        hit = _live_observed(url, rail="base")
        hit = probe.attach_catalog_fields(hit, item)
        hit["rail"] = "algorand"
        cons = select.parse_constraints({"networks": ["algorand"]})
        self.assertTrue(hit.get("payable"))
        self.assertFalse(select.passes_constraints(hit, cons))
        self.assertIsNone(select.pick_winner([hit], "best", cons))
        selected = select.pick_selected_payment(hit, "best", cons)
        self.assertIsNone(selected)
        claimed = (hit.get("claimed") or {}).get("payment_options") or []
        claimed_rails = {o.get("rail") for o in claimed}
        self.assertIn("algorand", claimed_rails)
        result = self._route(
            [item],
            self._probe_by_url({url: hit}),
            need="algorand asset lookup",
            constraints=cons,
        )
        self.assertFalse(result.get("live"))
        self.assertIsNone(result.get("selected_payment"))
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        self.assertNotEqual(result.get("miss_reason"), None)
        self.assertIn("algorand", str((result.get("claimed") or {}).get("payment_options") or claimed))

    def test_winner_lacks_selected_payment_is_typed_miss_not_200(self):
        live = {
            "live": True,
            "payable": True,
            "invocable": True,
            "url": "https://wx.example/null-pay",
            "selected_payment": None,
            "compared": [{"url": "https://other.example/algo", "rail": "algorand"}],
            "stop_reason": "winner_selected",
        }
        self.assertFalse(select.http200_winner_ok(live, "best", {"rails": frozenset({"algorand"})}))
        with patch("live402.probe.route_need", return_value=dict(live)):
            code, body = route.run_probe({"need": "weather", "networks": ["algorand"]})
        self.assertEqual(code, 503)
        self.assertFalse(body.get("live"))
        self.assertIsNone(body.get("selected_payment"))
        self.assertEqual(body.get("miss_reason"), "constraints_unmet")

    def test_winner_selected_payment_outside_networks_is_typed_miss(self):
        selected = _complete_selected("base")
        live = _live_observed("https://wx.example/base-as-algo", rail="base")
        live["selected_payment"] = selected
        cons = select.parse_constraints({"networks": ["algorand"]})
        self.assertFalse(select.http200_winner_ok(live, "best", cons))
        with patch("live402.probe.route_need", return_value=dict(live)):
            code, body = route.run_probe({"need": "weather", "networks": ["algorand"]})
        self.assertEqual(code, 503)
        self.assertFalse(body.get("live"))
        self.assertIsNone(body.get("selected_payment"))
        self.assertEqual(body.get("miss_reason"), "constraints_unmet")
        claimed_fill = (body.get("selected_payment") or {})
        self.assertNotEqual(claimed_fill.get("rail"), "algorand")

    def test_winner_always_in_compared_when_capped(self):
        items = [
            self._item("https://dead%d.example/weather" % i, "base")
            for i in range(5)
        ]
        winner_item = self._item("https://win6.example/weather", "base")
        items.append(winner_item)
        by_url = {}
        for item in items[:-1]:
            by_url[item["url"]] = {
                "live": False,
                "url": item["url"],
                "miss_reason": "no_402_envelope",
                "probed_at": probe.now_iso(),
            }
        by_url[winner_item["url"]] = _live_observed(winner_item["url"], rail="base")
        result = self._route(items, self._probe_by_url(by_url))
        self.assertTrue(result.get("live"))
        self.assertEqual(result.get("url"), winner_item["url"])
        self.assertEqual(result.get("candidates_probed"), 6)
        compared = result.get("compared") or []
        self.assertLessEqual(len(compared), select.COMPARED_CAP)
        self.assertIn(winner_item["url"], [row.get("url") for row in compared])
        self.assertTrue(any(row.get("selected") and row.get("url") == winner_item["url"] for row in compared))
        rows = [_live_observed("https://c%d.example/w" % i, rail="base") for i in range(6)]
        capped = select.comparison(rows, rows[-1], "best", None)
        self.assertEqual(len(capped), select.COMPARED_CAP)
        self.assertEqual(capped[-1]["url"], rows[-1]["url"])
        self.assertTrue(capped[-1]["selected"])

    def test_payable_true_invocable_false_no_input_schema_200(self):
        item = self._item("https://wx.example/payable-only", "base")
        hit = _live_observed(item["url"], rail="base", invocable=False, schema=False)
        self.assertTrue(hit.get("payable"))
        self.assertFalse(hit.get("invocable"))
        self.assertEqual(hit.get("miss_reason"), "no_input_schema")
        result = self._route([item], self._probe_by_url({item["url"]: hit}))
        self.assertTrue(result.get("live"))
        self.assertTrue(result.get("payable"))
        self.assertFalse(result.get("invocable"))
        self.assertEqual(result.get("miss_reason"), "no_input_schema")
        self.assertIsInstance(result.get("selected_payment"), dict)
        self.assertEqual(result.get("stop_reason"), "winner_selected")

    def test_require_invocable_constraints_unmet_when_none_invocable(self):
        item = self._item("https://wx.example/payable-only", "base")
        hit = _live_observed(item["url"], rail="base", invocable=False, schema=False)
        cons = select.parse_constraints({"require_invocable": True})
        result = self._route(
            [item], self._probe_by_url({item["url"]: hit}), constraints=cons
        )
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "constraints_unmet")
        self.assertIn("require_invocable", result.get("unmet_constraints") or [])
        self.assertIsNone(result.get("selected_payment"))

    def test_tiny_price_and_min_observations_unmet_stay_distinct(self):
        cheap_fail = _live_observed("https://wx.example/dear", rail="base", amount=50000)
        cheap_fail["history"] = {"n_7d": 20, "success_7d": 0.9}
        obs_fail = _live_observed("https://wx.example/new", rail="base", amount=1000)
        obs_fail["history"] = {"n_7d": 0, "success_7d": None}
        price_cons = select.parse_constraints({"max_price_usd": 0.01})
        obs_cons = select.parse_constraints({"min_observations": 10})
        price_names = select.collect_unmet_constraints([cheap_fail], price_cons)
        obs_names = select.collect_unmet_constraints([obs_fail], obs_cons)
        self.assertIn("max_price_usd", price_names)
        self.assertNotIn("min_observations", price_names)
        self.assertIn("min_observations", obs_names)
        self.assertNotIn("max_price_usd", obs_names)
        price_result = self._route(
            [self._item(cheap_fail["url"])],
            self._probe_by_url({cheap_fail["url"]: cheap_fail}),
            constraints=price_cons,
        )
        obs_result = self._route(
            [self._item(obs_fail["url"])],
            self._probe_by_url({obs_fail["url"]: obs_fail}),
            constraints=obs_cons,
        )
        self.assertEqual(price_result.get("miss_reason"), "constraints_unmet")
        self.assertEqual(obs_result.get("miss_reason"), "constraints_unmet")
        self.assertEqual(price_result.get("unmet_constraints"), ["max_price_usd"])
        self.assertEqual(obs_result.get("unmet_constraints"), ["min_observations"])

    def test_run_probe_echoes_applied_structured_constraints(self):
        item = self._item("https://wx.example/base-weather", "base")
        hit = _live_observed(item["url"], rail="base")
        with patch("live402.probe.fetch_discovery", return_value=[item]), patch(
            "live402.probe.probe_url", return_value=dict(hit)
        ):
            code, body = route.run_probe(
                {"need": "weather forecast", "networks": ["base"]}
            )
        self.assertEqual(code, 200)
        self.assertEqual((body.get("interpreted_constraints") or {}).get("networks"), ["base"])
        self.assertEqual((body.get("applied_constraints") or {}).get("networks"), ["base"])
        self.assertIn("discovered_count", body)
        self.assertIn("probed_count", body)
        self.assertIn("unprobed_count", body)
        self.assertIn("evaluation_complete", body)
        self.assertEqual(body.get("evaluation_complete"), body.get("candidate_evaluation_complete"))

    def test_catalog_selected_payment_not_filled_from_claim(self):
        url = "https://wx.example/claim-algo-obs-base"
        item = _catalog_algorand_item(url)
        hit = _live_observed(url, rail="base")
        hit = probe.attach_catalog_fields(hit, item)
        cons = select.parse_constraints({"networks": ["algorand"]})
        picked = select.pick_selected_payment(hit, "cheapest", cons)
        self.assertIsNone(picked)
        unconstrained = select.pick_selected_payment(hit, "cheapest", None)
        self.assertEqual(unconstrained["rail"], "base")
        self.assertNotEqual(unconstrained["rail"], "algorand")


class Http200WinnerOkTests(unittest.TestCase):
    def test_complete_observed_option_matches_networks(self):
        hit = _live_observed("https://ok.example/x", rail="solana")
        selected = select.pick_selected_payment(hit, "best", None)
        self.assertTrue(select.selected_payment_is_complete(selected))
        cons = select.parse_constraints({"networks": ["solana"]})
        hit["selected_payment"] = selected
        self.assertTrue(select.http200_winner_ok(hit, "best", cons))
        cons_base = select.parse_constraints({"networks": ["base"]})
        self.assertFalse(select.http200_winner_ok(hit, "best", cons_base))

    def test_missing_selected_payment_fails_closed(self):
        hit = {"live": True, "payable": True, "url": "https://x.example/x"}
        self.assertFalse(select.http200_winner_ok(hit, "best", None))


class PolicyEchoTests(unittest.TestCase):
    def test_prefer_network_echoed_but_not_a_lock(self):
        body = {"need": "weather", "prefer_network": "solana"}
        cons = policy.merge_constraints(body)
        self.assertIsNone(cons.get("rails"))
        applied = policy.public_applied_constraints(cons, body)
        self.assertEqual(applied.get("prefer_network"), "solana")
        self.assertNotIn("networks", applied)


if __name__ == "__main__":
    unittest.main()
