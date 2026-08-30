"""Probe history persistence, freshness, readiness, pulse peek. No network."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, history, probe, pulse


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
        "accepts": [{"payTo": catalog_pay or pay_to, "amount": "10000", "network": "base"}],
    }
    if schema:
        item["inputSchema"] = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
    result = probe.attach_catalog_fields(result, item)
    result = probe.attach_invocable_target(result, item)
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


class PulsePeekTests(unittest.TestCase):
    def setUp(self):
        catalog.reset_index()
        pulse.reset_cache()

    def tearDown(self):
        catalog.reset_index()
        pulse.reset_cache()

    def test_peek_index_does_not_refresh(self):
        catalog.reset_index()
        with patch("live402.catalog.refresh") as refresh:
            self.assertIsNone(catalog.peek_index())
            refresh.assert_not_called()

    def test_get_index_refreshes_on_cold_start(self):
        catalog.reset_index()
        empty = {
            "items": [],
            "by_rail": {"base": [], "solana": [], "algorand": []},
            "fetched_at": 0,
            "totals": {},
            "truncated": {},
            "complete": True,
            "errors": {},
        }
        with patch("live402.catalog.refresh", return_value=empty) as refresh:
            idx = catalog.get_index()
            refresh.assert_called()
            self.assertEqual(idx, empty)

    def test_pulse_peek_does_not_call_refresh(self):
        catalog.reset_index()
        pulse.reset_cache()
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.get_index"
        ) as get_idx, patch("live402.catalog.refresh") as refresh:
            payload = pulse._collect()
            get_idx.assert_not_called()
            refresh.assert_not_called()
            self.assertTrue(payload.get("ok"))
            self.assertIn("chains", payload)
            for chain in ("base", "solana", "algorand"):
                self.assertIn(chain, payload["chains"])


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

        def fake_probe(url, catalog_item=None, deadline=None):
            _ = catalog_item, deadline
            if "changed" in url:
                return {
                    "live": True,
                    "url": url,
                    "payTo": "0xnew",
                    "payTo_changed": True,
                    "invocable": False,
                    "probed_at": probe.now_iso(),
                    "risk": ["payTo_changed"],
                    "readiness": "payable",
                }
            return {
                "live": True,
                "url": url,
                "payTo": "0xabc",
                "payTo_changed": False,
                "invocable": False,
                "probed_at": probe.now_iso(),
                "readiness": "payable",
            }

        with patch("live402.probe.fetch_discovery", return_value=items), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            result = probe.route_need("weather")
        self.assertEqual(result.get("url"), "https://stable.example/weather")
        self.assertNotEqual(result.get("risk"), ["payTo_changed"])
        self.assertTrue(result.get("live"))


if __name__ == "__main__":
    unittest.main()
