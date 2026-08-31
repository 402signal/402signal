"""Probe history persistence, freshness, readiness, pulse peek. No network."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, fixtures, history, payment, probe, pulse


def _complete_envelope(pay_to, amount="10000", rail="base"):
    if rail == "solana":
        network, asset = payment.SOLANA_MAINNET, payment.USDC_SOLANA_MINT
    elif rail == "algorand":
        network, asset = payment.ALGORAND_MAINNET, payment.USDC_ALGORAND_ASA
    else:
        network, asset = payment.BASE_CAIP2, payment.USDC_BASE
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "asset": asset,
                "amount": amount,
                "payTo": pay_to,
            }
        ],
    }


def _snap(live=True, payTo="0xabc", **extra):
    row = {
        "live": bool(live),
        "status": 402 if live else None,
        "latency_ms": extra.pop("latency_ms", 10),
        "has_402_challenge": bool(live),
        "payTo": payTo if live else None,
        "probed_at": probe.now_iso(),
    }
    if not live:
        row["miss_reason"] = extra.pop("miss_reason", "no_402_envelope")
        row["payTo"] = None
    row.update(extra)
    if live and row.get("payTo") and not row.get("envelope") and not row.get("accepts"):
        amount = row.get("amount") or "10000"
        rail = row.get("rail") or "base"
        row["envelope"] = _complete_envelope(row["payTo"], amount=amount, rail=rail)
        row.setdefault("amount", amount)
        row.setdefault("asset", payment.usdc_asset_for_rail(rail) or payment.USDC_BASE)
        row.setdefault("rail", rail)
    return row


def _probe_result(pay_to="0xabc", schema=False, catalog_pay=None, url="https://wx.example/forecast"):
    snap = {
        "live": True,
        "status": 402,
        "latency_ms": 12,
        "has_402_challenge": True,
        "payTo": pay_to,
        "probed_at": probe.now_iso(),
    }
    result = probe.health_from_probe(url, snap)
    item = {
        "url": url,
        "accepts": [
            {
                "payTo": catalog_pay or pay_to,
                "amount": "10000",
                "network": "base",
                "asset": payment.USDC_BASE,
            }
        ],
    }
    env = None
    if pay_to:
        env = _complete_envelope(pay_to)
        result["envelope"] = env
        result["amount"] = "10000"
        result["asset"] = payment.USDC_BASE
        result["rail"] = "base"
    if schema:
        item["inputSchema"] = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
    result = probe.attach_catalog_fields(result, item)
    result = probe.attach_invocable_target(result, item, env)
    return probe._finalize_probe(result)


class HistoryDbTests(unittest.TestCase):
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

    def test_two_probes_same_url_success_rate(self):
        url = "https://hist.example/wx"
        history.record_probe(url, _snap(live=True, payTo="0xabc", latency_ms=11))
        history.record_probe(url, _snap(live=False, miss_reason="no_402_envelope", latency_ms=22))
        summ = history.summary(url)
        self.assertEqual(summ["n_24h"], 2)
        self.assertEqual(summ["ok_24h"], 1)
        self.assertEqual(summ["success_24h"], 0.5)
        self.assertEqual(summ["n_7d"], 2)
        self.assertEqual(summ["success_7d"], 0.5)
        self.assertIsNotNone(summ["p50_latency_ms"])
        self.assertIsNotNone(summ["p95_latency_ms"])

    def test_preview_observation_not_yet_vs_observed_hides_thin_rate(self):
        seen = "https://w.example/base-weather"
        unseen = "https://a.example/algo-weather"
        history.record_probe(
            seen,
            _snap(live=True, payTo="0xabc", latency_ms=17, invocable=1, payable=1),
        )
        for _ in range(3):
            history.record_probe(seen, _snap(live=True, payTo="0xabc", latency_ms=17))
        obs = history.preview_observations([seen, unseen])
        self.assertEqual(obs[unseen]["status"], "not_yet_observed")
        self.assertEqual(obs[seen]["status"], "observed")
        self.assertTrue(obs[seen].get("payable"))
        self.assertEqual(obs[seen].get("last_latency_ms"), 17)
        self.assertGreaterEqual(obs[seen].get("n_7d"), 3)
        self.assertLess(obs[seen].get("n_7d"), 10)
        self.assertNotIn("success_7d", obs[seen])

        items = [
            {
                "url": seen,
                "description": "weather",
                "_rail": "base",
                "accepts": [{"network": "eip155:8453", "amount": "10000"}],
            },
            {
                "url": unseen,
                "description": "weather",
                "_rail": "algorand",
                "accepts": [{"network": "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", "amount": "10000"}],
            },
        ]
        with patch("live402.catalog.query_for_need", return_value={"items": items}):
            body = pulse.preview_need("weather")
        self.assertTrue(body.get("not_probed"))
        self.assertNotIn("candidates_probed", body)
        self.assertEqual(body.get("discovery_matches"), 2)
        self.assertEqual(body.get("displayed"), 2)
        by_url = {h["url"]: h for h in body["hits"]}
        self.assertEqual(by_url[seen]["observation"]["status"], "observed")
        self.assertEqual(by_url[unseen]["observation"]["status"], "not_yet_observed")
        self.assertNotIn("success_7d", by_url[seen]["observation"])

    def test_preview_discovery_matches_truncated_without_invented_total(self):
        items = []
        for i in range(12):
            items.append(
                {
                    "url": "https://wx%d.example/weather" % i,
                    "description": "weather forecast",
                    "_rail": "base",
                    "accepts": [{"network": "eip155:8453", "amount": "10000"}],
                }
            )
        with patch("live402.catalog.query_for_need", return_value={"items": items}):
            body = pulse.preview_need("weather")
        self.assertTrue(body.get("not_probed"))
        self.assertEqual(body.get("discovery_matches"), 12)
        self.assertEqual(body.get("displayed"), 8)
        self.assertTrue(body.get("truncated"))
        self.assertNotIn("total", body)
        self.assertEqual(len(body["hits"]), 8)
        with patch(
            "live402.catalog.query_for_need",
            return_value={"items": items, "pagination": {"total": 99}},
        ):
            with_total = pulse.preview_need("weather")
        self.assertEqual(with_total.get("total"), 99)
        self.assertFalse(body.get("discovery_exhaustive"))
        self.assertEqual(body.get("discovery_via"), {})

    def test_preview_exposes_safe_discovery_via(self):
        self.assertTrue(fixtures.fixture_mode())
        body = pulse.preview_need("weather")
        self.assertTrue(body.get("not_probed"))
        via = body.get("discovery_via") or {}
        self.assertEqual(via.get("base"), "fixture")
        self.assertEqual(via.get("solana"), "fixture")
        self.assertEqual(via.get("algorand"), "fixture")
        self.assertTrue(body.get("discovery_exhaustive"))
        blob = str(via)
        self.assertNotIn("upstream_total", blob)
        self.assertNotIn("fetch_failed", blob)

    def test_n0_success_is_none_not_zero(self):
        summ = history.summary("https://never-seen.example/x")
        self.assertEqual(summ["n_7d"], 0)
        self.assertEqual(summ["n_24h"], 0)
        self.assertIsNone(summ["success_7d"])
        self.assertIsNone(summ["success_24h"])
        self.assertIsNot(summ["success_7d"], 0.0)
        self.assertIsNone(summ["p50_latency_ms"])
        self.assertIsNone(summ["p95_latency_ms"])

    def test_payto_flip_sets_changed_at(self):
        url = "https://hist.example/flip"
        t0 = int(time.time()) - 5
        history.record_probe(url, _snap(live=True, payTo="0xaaa", ts=t0))
        history.record_probe(url, _snap(live=True, payTo="0xbbb", ts=t0 + 2))
        summ = history.summary(url)
        self.assertEqual(summ["last_payTo"], "0xbbb")
        self.assertEqual(summ["payTo_changed_at"], t0 + 2)

    def test_base_payto_case_is_not_a_flip(self):
        url = "https://hist.example/base-case"
        mixed = "0xb18fc2275f36dae99eb215caeff03b431f887d16"
        t0 = int(time.time()) - 5
        history.record_probe(url, _snap(live=True, payTo=mixed, rail="base", ts=t0))
        meta = history.record_probe(url, _snap(live=True, payTo=mixed.upper(), rail="base", ts=t0 + 1))
        self.assertFalse(meta.get("payTo_flipped"))
        self.assertIsNone(history.summary(url).get("payTo_changed_at"))

    def test_solana_payto_case_is_a_flip(self):
        url = "https://hist.example/sol-case"
        addr = payment.DEFAULT_PAYTO_SOLANA
        t0 = int(time.time()) - 5
        history.record_probe(url, _snap(live=True, payTo=addr, rail="solana", ts=t0))
        meta = history.record_probe(
            url, _snap(live=True, payTo=addr.lower(), rail="solana", ts=t0 + 1)
        )
        self.assertTrue(meta.get("payTo_flipped"))
        self.assertEqual(history.summary(url).get("payTo_changed_at"), t0 + 1)

    def test_price_flip_requires_same_asset(self):
        url = "https://hist.example/price-asset"
        t0 = int(time.time()) - 5
        history.record_probe(
            url,
            _snap(
                live=True,
                payTo="0xabc",
                rail="base",
                amount="10000",
                asset=payment.USDC_BASE,
                ts=t0,
            ),
        )
        same = history.record_probe(
            url,
            _snap(
                live=True,
                payTo="0xabc",
                rail="base",
                amount="10000",
                asset=payment.USDC_BASE,
                ts=t0 + 1,
            ),
        )
        self.assertFalse(same.get("price_flipped"))
        flipped = history.record_probe(
            url,
            _snap(
                live=True,
                payTo="0xabc",
                rail="base",
                amount="20000",
                asset=payment.USDC_BASE,
                ts=t0 + 2,
            ),
        )
        self.assertTrue(flipped.get("price_flipped"))

    def test_record_probe_never_raises_unwritable(self):
        os.environ["LIVE402_HISTORY_DB"] = "/proc/1/live402-history.sqlite"
        history.reset()
        try:
            history.record_probe("https://x.example/a", _snap(live=True, payTo="0xabc"))
        except Exception as exc:
            self.fail("record_probe raised %r" % exc)
        with patch("sqlite3.connect", side_effect=PermissionError("unwritable")):
            try:
                history.record_probe("https://x.example/b", _snap(live=True, payTo="0xabc"))
            except Exception as exc:
                self.fail("record_probe raised %r" % exc)

    def test_probe_result_verified_and_readiness(self):
        payable = _probe_result(pay_to="0xabc", schema=False)
        self.assertEqual(payable.get("verified_seconds_ago"), 0)
        self.assertEqual(payable.get("verified_at"), payable.get("probed_at"))
        self.assertIn(payable.get("readiness"), ("payable", "recently_verified"))
        self.assertNotEqual(payable.get("readiness"), "healthy")
        self.assertIn(payable.get("readiness_healthy"), (None, "unknown"))
        self.assertTrue(payable.get("live"))
        inv = _probe_result(pay_to="0xabc", schema=True)
        self.assertEqual(inv.get("verified_seconds_ago"), 0)
        self.assertEqual(inv.get("readiness"), "invocable")
        self.assertTrue(inv.get("invocable"))
        missing = _probe_result(pay_to=None, schema=True)
        self.assertEqual(missing.get("readiness"), "discovered")
        hist = inv.get("history") or {}
        for key in ("success_24h", "success_7d", "n_24h", "n_7d", "p50_latency_ms", "p95_latency_ms"):
            self.assertIn(key, hist)

    def test_payto_changed_sets_risk(self):
        result = _probe_result(pay_to="0xnew", catalog_pay="0xold")
        self.assertTrue(result.get("payTo_changed"))
        self.assertEqual(result.get("risk"), ["payTo_changed"])

    def test_history_flip_sets_risk(self):
        url = "https://wx.example/forecast"
        history.record_probe(url, _snap(live=True, payTo="0xold"))
        result = _probe_result(pay_to="0xnew", catalog_pay="0xnew", url=url)
        self.assertTrue(result.get("payTo_changed"))
        self.assertEqual(result.get("risk"), ["payTo_changed"])

    def test_fixture_probe_records_verified_seconds_ago(self):
        result = probe.probe_url("https://fixture.402signal.local/weather")
        self.assertIn("verified_seconds_ago", result)
        self.assertEqual(result.get("verified_seconds_ago"), 0)
        self.assertIn(result.get("readiness"), ("discovered", "payable", "invocable", "recently_verified"))
        self.assertNotEqual(result.get("readiness"), "healthy")

    def test_db_wal_shm_are_0600(self):
        history.record_probe("https://hist.example/mode", _snap(live=True, payTo="0xabc"))
        path = history.db_path()
        self.assertTrue(os.path.exists(path))
        for pth in (path, path + "-wal", path + "-shm"):
            if os.path.exists(pth):
                self.assertEqual(os.stat(pth).st_mode & 0o777, 0o600, pth)

    def _probe_cols(self, url):
        conn = sqlite3.connect(history.db_path())
        try:
            return conn.execute(
                "SELECT amount, schema_present, payTo FROM probes WHERE url = ? ORDER BY id DESC LIMIT 1",
                (url,),
            ).fetchone()
        finally:
            conn.close()

    def _obs_types(self, url):
        conn = sqlite3.connect(history.db_path())
        try:
            return conn.execute(
                "SELECT source_type, field, value, status FROM observations WHERE url = ? ORDER BY id",
                (url,),
            ).fetchall()
        finally:
            conn.close()

    def test_record_probe_writes_observed_not_claimed(self):
        url = "https://hist.example/obs-only"
        history.record_probe(url, _snap(live=True, payTo="0xabc", amount="10000", status=402))
        latest = history.latest_observations(url)
        self.assertTrue(latest["observed"])
        self.assertFalse(latest["claimed"])
        for field, row in latest["observed"].items():
            self.assertEqual(row["source_type"], "402signal_observed")
            self.assertEqual(row["provenance"], "402signal_observed")
            self.assertNotEqual(row["source_type"], "catalog_claimed")
            self.assertNotEqual(row["source_type"], "legacy_mixed")
        self.assertEqual(latest["observed"]["payTo"]["value"], "0xabc")
        types = {r[0] for r in self._obs_types(url)}
        self.assertEqual(types, {"402signal_observed"})
        self.assertIn(("402signal_observed", "payTo", "0xabc", "observed"), self._obs_types(url))

    def test_claimed_dict_does_not_change_url_state(self):
        url = "https://hist.example/claim-state"
        history.record_probe(
            url,
            _snap(
                live=True,
                payTo="0xobs",
                amount="10000",
                claimed={"payTo": "0xcat", "amount": "1", "source": "cdp"},
            ),
        )
        summ = history.summary(url)
        self.assertEqual(summ["last_payTo"], "0xobs")
        latest = history.latest_observations(url)
        self.assertEqual(latest["claimed"]["payTo"]["value"], "0xcat")
        self.assertEqual(latest["claimed"]["payTo"]["source_type"], "catalog_claimed")
        self.assertEqual(latest["observed"]["payTo"]["value"], "0xobs")
        self.assertEqual(latest["observed"]["payTo"]["source_type"], "402signal_observed")
        row = self._probe_cols(url)
        self.assertEqual(row[2], "0xobs")
        self.assertEqual(row[0], "10000")

    def test_later_claim_does_not_overwrite_observed(self):
        url = "https://hist.example/later-claim"
        history.record_probe(url, _snap(live=True, payTo="0xobs", amount="10000"))
        history.record_claim(url, {"payTo": "0xother", "amount": "999"}, source="cdp")
        latest = history.latest_observations(url)
        self.assertEqual(latest["observed"]["payTo"]["value"], "0xobs")
        self.assertEqual(latest["observed"]["payTo"]["source_type"], "402signal_observed")
        self.assertEqual(latest["claimed"]["payTo"]["value"], "0xother")
        self.assertEqual(latest["claimed"]["payTo"]["source_type"], "catalog_claimed")
        self.assertEqual(latest["observed"]["amount"]["value"], "10000")
        summ = history.summary(url)
        self.assertEqual(summ["last_payTo"], "0xobs")
        row = self._probe_cols(url)
        self.assertEqual(row[2], "0xobs")
        self.assertEqual(row[0], "10000")

    def test_attach_to_result_claimed_observed_unknown_is_none(self):
        url = "https://hist.example/attach-sides"
        snap = _snap(live=True, payTo="0xobs", amount="10000", latency_ms=15, status=402)
        snap["claimed"] = {"payTo": "0xcat"}
        history.record_probe(url, snap)
        result = {
            "url": url,
            "live": True,
            "payTo": "0xobs",
            "probed_at": "2026-08-30T00:00:00Z",
        }
        out = history.attach_to_result(result)
        self.assertIn("claimed", out)
        self.assertIn("observed", out)
        self.assertEqual(out["claimed"]["payTo"], "0xcat")
        self.assertIsNone(out["claimed"]["amount"])
        self.assertIsNone(out["claimed"]["schema_present"])
        self.assertIsNone(out["claimed"]["facilitator"])
        self.assertIsNot(out["claimed"]["amount"], "10000")
        self.assertEqual(out["observed"]["payTo"], "0xobs")
        self.assertEqual(out["observed"]["amount"], "10000")
        self.assertEqual(out["observed"]["http_status"], 402)
        self.assertEqual(out["observed"]["latency_ms"], 15)
        self.assertIsNone(out["observed"]["schema_present"])
        self.assertEqual(out["verified_at"], out["probed_at"])
        self.assertNotIn("verified", str(out["claimed"]).lower() or "")
        self.assertTrue(out.get("payTo_changed"))
        self.assertEqual(out.get("risk"), ["payTo_changed"])

    def test_catalog_fallback_not_stored_as_observed(self):
        url = "https://hist.example/catalog-only"
        snap = {
            "live": True,
            "payTo": "0xabc",
            "status": 402,
            "latency_ms": 10,
            "target": {
                "amountAtomic": "99999",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
            "schema_source": "catalog",
            "invocable": True,
        }
        history.record_probe(url, snap)
        latest = history.latest_observations(url)
        self.assertNotIn("amount", latest["observed"])
        self.assertNotIn("schema_present", latest["observed"])
        self.assertNotIn("invocable", latest["observed"])
        self.assertEqual(latest["observed"]["payTo"]["value"], "0xabc")
        self.assertEqual(latest["observed"]["http_status"]["value"], "402")
        self.assertFalse(latest["claimed"])
        row = self._probe_cols(url)
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        types = {r[0] for r in self._obs_types(url)}
        self.assertEqual(types, {"402signal_observed"})
        for _stype, field, value, _status in self._obs_types(url):
            self.assertNotEqual(field, "amount")
            self.assertNotEqual(value, "99999")

    def test_envelope_amount_and_schema_are_observed(self):
        url = "https://hist.example/envelope"
        snap = _snap(live=True, payTo="0xabc")
        snap["envelope"] = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount": "10000",
                    "payTo": "0xabc",
                }
            ],
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        }
        history.record_probe(url, snap)
        latest = history.latest_observations(url)
        self.assertEqual(latest["observed"]["amount"]["value"], "10000")
        self.assertEqual(latest["observed"]["schema_present"]["value"], "1")
        self.assertEqual(latest["observed"]["invocable"]["value"], "1")
        row = self._probe_cols(url)
        self.assertEqual(row[0], "10000")
        self.assertEqual(row[1], 1)

    def test_attach_catalog_fields_records_claimed_not_as_observed(self):
        url = "https://wx.example/forecast"
        result = {
            "live": True,
            "url": url,
            "status": 402,
            "latency_ms": 12,
            "has_402_challenge": True,
            "payTo": "0xobs",
            "probed_at": probe.now_iso(),
            "envelope": {"accepts": [{"amount": "10000", "payTo": "0xobs"}]},
        }
        item = {
            "_rail": "solana",
            "accepts": [
                {
                    "payTo": "0xclaim",
                    "amount": "5000",
                    "extra": {"facilitator": "https://facilitator.payai.network"},
                }
            ],
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
        result = probe.attach_catalog_fields(result, item)
        self.assertEqual(result["payTo"], "0xobs")
        self.assertEqual(result["claimed"]["payTo"], "0xclaim")
        self.assertEqual(result["claimed"]["amount"], "5000")
        result = probe._finalize_probe(result)
        latest = history.latest_observations(url)
        self.assertEqual(latest["observed"]["payTo"]["value"], "0xobs")
        self.assertEqual(latest["observed"]["payTo"]["source_type"], "402signal_observed")
        self.assertEqual(latest["claimed"]["payTo"]["value"], "0xclaim")
        self.assertEqual(latest["claimed"]["payTo"]["source_type"], "catalog_claimed")
        self.assertEqual(latest["claimed"]["amount"]["value"], "5000")
        self.assertNotEqual(latest["observed"].get("amount", {}).get("value"), "5000")
        self.assertTrue(result.get("payTo_changed"))
        types = {r[0] for r in self._obs_types(url)}
        self.assertEqual(types, {"402signal_observed", "catalog_claimed"})
        self.assertNotIn("legacy_mixed", types)

    def test_thin_envelope_bazaar_source_is_not_observed_schema(self):
        url = "https://hist.example/thin-bazaar"
        snap = _snap(live=True, payTo="0xabc")
        snap["schema_source"] = "bazaar"
        snap["envelope"] = {"accepts": [{"payTo": "0xabc", "amount": "10000"}]}
        snap["invocable"] = True
        history.record_probe(url, snap)
        latest = history.latest_observations(url)
        self.assertNotIn("schema_present", latest["observed"])
        self.assertNotIn("invocable", latest["observed"])
        row = self._probe_cols(url)
        self.assertIsNone(row[1])
        types = {r[0] for r in self._obs_types(url)}
        self.assertEqual(types, {"402signal_observed"})
        fields = {r[1] for r in self._obs_types(url)}
        self.assertNotIn("schema_present", fields)
        self.assertNotIn("invocable", fields)


class PulsePeekTests(unittest.TestCase):
    def setUp(self):
        catalog.reset_index()
        pulse.reset_cache()

    def tearDown(self):
        catalog.reset_index()
        pulse.reset_cache()

    def test_peek_index_does_not_refresh(self):
        catalog.reset_index()
        with patch("live402.catalog.refresh") as refresh, patch(
            "live402.catalog.query_for_need"
        ) as query:
            self.assertIsNone(catalog.peek_index())
            refresh.assert_not_called()
            query.assert_not_called()

    def test_get_index_does_not_crawl(self):
        catalog.reset_index()
        with patch("live402.catalog.refresh") as refresh, patch(
            "live402.probe._fetch_catalog_payload"
        ) as fetch:
            idx = catalog.get_index()
            refresh.assert_not_called()
            fetch.assert_not_called()
            self.assertEqual(idx.get("items"), [])

    def test_pulse_does_not_call_refresh_or_query(self):
        catalog.reset_index()
        pulse.reset_cache()
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.get_index"
        ) as get_idx, patch("live402.catalog.refresh") as refresh, patch(
            "live402.catalog.query_for_need"
        ) as query:
            payload = pulse._collect()
            get_idx.assert_not_called()
            refresh.assert_not_called()
            query.assert_not_called()
            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("index_status"), "upstream")
            self.assertIn("chains", payload)
            for chain in ("base", "solana", "algorand"):
                self.assertIn(chain, payload["chains"])
                self.assertIsNone(payload["chains"][chain].get("count"))
                self.assertEqual(payload["chains"][chain]["source"].get("catalog"), "upstream")

    def test_pulse_does_not_invent_catalog_totals(self):
        pulse.reset_cache()
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.query_for_need"
        ) as query, patch("live402.probe._fetch_catalog_payload") as fetch:
            payload = pulse._collect()
            query.assert_not_called()
            fetch.assert_not_called()
        self.assertEqual(payload.get("index_status"), "upstream")
        for chain in ("base", "solana", "algorand"):
            self.assertIsNone(payload["chains"][chain].get("count"))
            self.assertNotEqual(payload["chains"][chain].get("count"), 14376)
            self.assertNotEqual(payload["chains"][chain].get("count"), 14000)
        self.assertIn("observed", payload)
        self.assertEqual(payload["observed"].get("source"), "402signal_observed")
        self.assertNotIn("healthy", payload["observed"])
        self.assertNotIn("success_7d", payload["observed"])
        self.assertNotIn("executable_now_rate", payload["observed"])

    def test_get_pulse_fast_without_crawl(self):
        pulse.reset_cache()
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.get_index"
        ) as get_idx, patch("live402.catalog.refresh") as refresh, patch(
            "live402.catalog.query_for_need"
        ) as query:
            t0 = time.monotonic()
            payload = pulse.get_pulse()
            elapsed = time.monotonic() - t0
            get_idx.assert_not_called()
            refresh.assert_not_called()
            query.assert_not_called()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(payload.get("index_status"), "upstream")

    def test_concurrent_get_pulse_one_collect(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_collect():
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return {
                "ok": True,
                "updated_at": "2026-08-30T00:00:00Z",
                "cached_s": 15,
                "index_status": "upstream",
                "chains": {},
                "samples": [],
                "observed": {"n_7d": 0, "reliability": "unknown", "source": "402signal_observed"},
            }

        with patch("live402.pulse._collect", side_effect=slow_collect):
            t1 = threading.Thread(target=pulse.get_pulse)
            t1.start()
            self.assertTrue(started.wait(2))
            t0 = time.monotonic()
            second = pulse.get_pulse()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.get("index_status"), "upstream")
            release.set()
            t1.join(5)
        self.assertEqual(len(calls), 1)

    def test_pulse_upstream_is_not_a_local_mirror(self):
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.peek_index", return_value=None
        ), patch("live402.catalog.get_index") as get_idx, patch(
            "live402.catalog.refresh"
        ) as refresh, patch("live402.catalog.query_for_need") as query:
            payload = pulse._collect()
            get_idx.assert_not_called()
            refresh.assert_not_called()
            query.assert_not_called()
        self.assertEqual(payload.get("index_status"), "upstream")
        self.assertNotEqual(payload.get("index_status"), "ready")
        self.assertNotEqual(payload.get("index_status"), "pending")
        self.assertNotEqual(payload.get("index_status"), "refreshing")
        for chain in ("base", "solana", "algorand"):
            src = payload["chains"][chain]["source"]
            self.assertTrue(src.get("ok"))
            self.assertEqual(src.get("catalog"), "upstream")
            self.assertIsNone(payload["chains"][chain]["count"])


class RailsSingleFlightTests(unittest.TestCase):
    def setUp(self):
        from live402 import rails

        rails.reset_cache()
        catalog.reset_index()

    def tearDown(self):
        from live402 import rails

        rails.reset_cache()
        catalog.reset_index()

    def test_concurrent_get_rails_one_collect(self):
        from live402 import rails

        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_collect():
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return {"ok": True, "rails": [], "updated_at": "t"}

        with patch("live402.rails.collect", side_effect=slow_collect):
            t1 = threading.Thread(target=rails.get_rails)
            t1.start()
            self.assertTrue(started.wait(2))
            out = {}

            def waiter():
                out["p"] = rails.get_rails()

            t2 = threading.Thread(target=waiter)
            t2.start()
            time.sleep(0.1)
            self.assertEqual(len(calls), 1)
            self.assertTrue(t2.is_alive())
            release.set()
            t1.join(5)
            t2.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out.get("p"), {"ok": True, "rails": [], "updated_at": "t"})

    def test_get_rails_last_good_fast_without_catalog_crawl(self):
        from live402 import rails

        primed = {"ok": True, "rails": [{"network": "base", "up": True}], "updated_at": "t0"}
        with patch("live402.rails.collect", return_value=primed):
            first = rails.get_rails()
        self.assertEqual(first["updated_at"], "t0")

        with patch.object(catalog, "fetch_rail") as fetch_rail, patch(
            "live402.rails.collect"
        ) as collect_mock:
            catalog.refresh()
            t0 = time.monotonic()
            again = rails.get_rails()
            elapsed = time.monotonic() - t0
            fetch_rail.assert_not_called()
            collect_mock.assert_not_called()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(again["updated_at"], "t0")
        self.assertFalse(catalog.refresh_in_progress())


class RankPayToChangedTests(unittest.TestCase):
    def test_payto_changed_not_winner_if_stable_live_exists(self):
        items = [
            {
                "url": "https://changed.example/weather",
                "description": "weather forecast",
                "accepts": [{"network": "base", "payTo": "0xold"}],
            },
            {
                "url": "https://stable.example/weather",
                "description": "weather forecast",
                "accepts": [{"network": "base", "payTo": "0xabc"}],
            },
        ]

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            _ = catalog_item, deadline
            pay_to = "0xnew" if "changed" in url else "0xabc"
            row = {
                "live": True,
                "url": url,
                "payTo": pay_to,
                "payTo_changed": "changed" in url,
                "invocable": False,
                "probed_at": probe.now_iso(),
                "readiness": "payable",
                "status": 402,
                "has_402_challenge": True,
                "rail": "base",
                "amount": "10000",
                "asset": payment.USDC_BASE,
                "accepts": [
                    {
                        "network": payment.BASE_CAIP2,
                        "asset": payment.USDC_BASE,
                        "amount": "10000",
                        "payTo": pay_to,
                    }
                ],
            }
            if "changed" in url:
                row["risk"] = ["payTo_changed"]
            return row

        with patch("live402.probe.fetch_discovery", return_value=items), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            result = probe.route_need("weather")
        self.assertEqual(result.get("url"), "https://stable.example/weather")
        self.assertNotEqual(result.get("risk"), ["payTo_changed"])
        self.assertTrue(result.get("live"))


class HistoryShortlistTests(unittest.TestCase):
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

    def _item(self, url, description, capability, rail="base"):
        return {
            "url": url,
            "description": description,
            "capability": capability,
            "_rail": rail,
            "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
        }

    def test_stale_success_cannot_leapfrog_better_match(self):
        strong = self._item(
            "https://strong.example/weather-forecast",
            "hourly weather forecast climate temperature",
            "travel.weather",
        )
        weak = self._item(
            "https://weak.example/misc",
            "weather",
            "search.web",
        )
        stale_ts = int(time.time()) - 3 * 86400
        history.record_probe(
            weak["url"],
            _snap(live=True, payTo="0xabc", ts=stale_ts),
        )
        ranked = probe.rank_resources("weather forecast", [weak, strong])
        boosted = probe._history_boost_shortlist(
            ranked, need="weather forecast", prefer_network=None
        )
        self.assertEqual(probe._resource_url(boosted[0]), strong["url"])
        strong_score = probe.score_need("weather forecast", strong)
        weak_score = probe.score_need("weather forecast", weak)
        self.assertGreaterEqual(strong_score - weak_score, probe.HISTORY_CLOSE_SCORE)

    def test_fresh_history_breaks_close_relevance_ties(self):
        a = self._item("https://a.example/weather", "weather forecast", "travel.weather")
        b = self._item("https://b.example/weather", "weather forecast", "travel.weather")
        self.assertEqual(
            probe.score_need("weather forecast", a),
            probe.score_need("weather forecast", b),
        )
        fresh_ts = int(time.time()) - 60
        history.record_probe(b["url"], _snap(live=True, payTo="0xabc", ts=fresh_ts))
        ranked = probe.rank_resources("weather forecast", [a, b])
        boosted = probe._history_boost_shortlist(
            ranked, need="weather forecast", prefer_network=None
        )
        self.assertEqual(probe._resource_url(boosted[0]), b["url"])

    def test_mature_history_beats_weak_when_relevance_similar(self):
        mature = self._item(
            "https://mature.example/weather", "weather forecast", "travel.weather"
        )
        weak = self._item(
            "https://weak.example/weather", "weather forecast", "travel.weather"
        )
        now = int(time.time()) - 2 * 3600
        for i in range(12):
            history.record_probe(
                mature["url"],
                _snap(live=True, payTo="0xabc", ts=now - i),
            )
        for i in range(4):
            history.record_probe(
                weak["url"],
                _snap(live=True, payTo="0xabc", ts=now - i),
            )
        ranked = probe.rank_resources("weather forecast", [weak, mature])
        boosted = probe._history_boost_shortlist(
            ranked, need="weather forecast", prefer_network=None
        )
        self.assertEqual(probe._resource_url(boosted[0]), mature["url"])


if __name__ == "__main__":
    unittest.main()
