"""PR14 shadow catalog: disk FTS, write-through, generation sweeps, PR13 invariants."""

from __future__ import annotations

import os
import resource
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, fixtures, history, payment, probe, pulse, select, shadow


CATALOG_BASE_PAYTO = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"
OBS_BASE_PAYTO = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SOL_PAYTO = payment.DEFAULT_PAYTO_SOLANA
CDP = "https://api.cdp.coinbase.com/platform/v2/x402"
PAYAI = "https://facilitator.payai.network"


def _item(url, description="", **extra):
    row = {
        "url": url,
        "description": description,
        "serviceName": extra.pop("serviceName", None) or "Weather API",
        "accepts": extra.pop(
            "accepts",
            [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000", "asset": payment.USDC_BASE}],
        ),
        "_input_schema_present": extra.pop("_input_schema_present", True),
        "_output_schema_present": extra.pop("_output_schema_present", False),
        "capability": extra.pop("capability", "travel.weather"),
        "tags": extra.pop("tags", ["weather", "forecast"]),
        "_rail": extra.pop("_rail", "base"),
    }
    row.update({k: v for k, v in extra.items() if v is not None})
    if row.get("serviceName") is None:
        row.pop("serviceName", None)
    return row


def _catalog_base_and_solana(url):
    return {
        "url": url,
        "description": "weather on two rails",
        "_rail": "base",
        "accepts": [
            {
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": "20000",
                "payTo": CATALOG_BASE_PAYTO,
                "extra": {"facilitator": CDP, "displayAmount": "$0.02"},
            },
            {
                "network": payment.SOLANA_MAINNET,
                "asset": payment.USDC_SOLANA_MINT,
                "amount": "1000",
                "payTo": SOL_PAYTO,
                "extra": {"facilitator": PAYAI, "displayAmount": "$0.001"},
            },
        ],
        "_input_schema_present": True,
        "capability": "travel.weather",
        "tags": ["weather"],
        "serviceName": "Dual rail weather",
    }


class _IsolatedCatalog(unittest.TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._prev = os.environ.get("LIVE402_CATALOG_DB")
        os.environ["LIVE402_CATALOG_DB"] = self._path
        shadow.reset()
        catalog.reset_working_set_peak()

    def tearDown(self):
        shadow.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_CATALOG_DB", None)
        else:
            os.environ["LIVE402_CATALOG_DB"] = self._prev
        try:
            os.remove(self._path)
        except OSError:
            pass


class FtsAndWriteThroughTests(_IsolatedCatalog):
    def test_fts_finds_seeded_resource(self):
        slim = catalog.slim_item(
            _item("https://wx.example/forecast", "hourly weather forecast for cities"),
            "base",
        )
        shadow.upsert_item(slim, source="cdp")
        hits = shadow.fts_search("weather")
        urls = [probe._resource_url(h) for h in hits]
        self.assertIn("https://wx.example/forecast", urls)
        self.assertLessEqual(len(hits), shadow.FTS_LIMIT)
        self.assertTrue(shadow.fts_available())

    def test_live_search_write_through_updates_row(self):
        first = catalog.slim_item(
            _item("https://wx.example/forecast", "old weather blurb", serviceName="Old Wx"),
            "base",
        )
        shadow.upsert_item(first, source="cdp")
        clocks_before = shadow.clocks("https://wx.example/forecast")

        def fake_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname or ""
            if "payai" in host or "goplausible" in host:
                return {"resources": [], "partialResults": False}
            return {
                "resources": [
                    _item(
                        "https://wx.example/forecast",
                        "updated hourly weather forecast",
                        serviceName="New Wx",
                    )
                ],
                "partialResults": False,
                "searchMethod": "hybrid",
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather")
        urls = [probe._resource_url(i) for i in working["items"]]
        self.assertIn("https://wx.example/forecast", urls)
        stored = shadow.get_resource("https://wx.example/forecast")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("serviceName"), "New Wx")
        self.assertIn("updated hourly weather", stored.get("description") or "")
        clocks_after = shadow.clocks("https://wx.example/forecast")
        self.assertGreaterEqual(clocks_after["discovery"], clocks_before["discovery"] or 0)
        self.assertGreaterEqual(clocks_after["claim"], clocks_before["claim"] or 0)
        self.assertIsNone(clocks_after["verification"])
        self.assertNotEqual(clocks_after["discovery"], clocks_after["verification"])
        self.assertNotEqual(clocks_after["claim"], clocks_after["verification"])

    def test_three_clocks_stay_separate(self):
        slim = catalog.slim_item(_item("https://wx.example/clocks", "weather clocks"), "base")
        shadow.upsert_item(slim, source="cdp", ts=1_700_000_000)
        shadow.mark_verified("https://wx.example/clocks", ts=1_700_000_500)
        c = shadow.clocks("https://wx.example/clocks")
        self.assertEqual(c["discovery"], 1_700_000_000)
        self.assertEqual(c["claim"], 1_700_000_000)
        self.assertEqual(c["verification"], 1_700_000_500)
        self.assertEqual(len(c), 3)
        self.assertNotIn("freshness", c)


class GenerationSweepTests(_IsolatedCatalog):
    def test_generation_sweep_marks_unseen_retired_does_not_delete(self):
        keep = catalog.slim_item(_item("https://wx.example/keep", "weather keep"), "base")
        drop = catalog.slim_item(_item("https://wx.example/drop", "weather drop"), "base")
        gen1 = shadow.begin_sweep("cdp", ts=100)
        shadow.ingest_page("cdp", [keep, drop], offset=0, last=True, upstream_total=2, step=2, ts=100)
        self.assertEqual(shadow.resource_status("https://wx.example/keep"), "active")
        self.assertEqual(shadow.resource_status("https://wx.example/drop"), "active")
        self.assertEqual(shadow.resource_count(), 2)

        gen2 = shadow.begin_sweep("cdp", ts=200)
        self.assertGreater(gen2, gen1)
        shadow.ingest_page("cdp", [keep], offset=0, last=True, upstream_total=1, step=1, ts=200)
        self.assertEqual(shadow.resource_status("https://wx.example/keep"), "active")
        self.assertEqual(shadow.resource_status("https://wx.example/drop"), "retired")
        self.assertEqual(shadow.resource_count(), 2)
        self.assertEqual(shadow.resource_count("retired"), 1)
        self.assertIsNotNone(shadow.get_resource("https://wx.example/drop"))
        events = {e["event"] for e in shadow.claim_events("https://wx.example/drop")}
        self.assertIn(shadow.EVENT_RETIRED, events)
        self.assertNotIn("resource_deleted", events)

    def test_retired_resource_reappears_on_write_through(self):
        row = catalog.slim_item(_item("https://wx.example/gone", "weather gone"), "base")
        shadow.begin_sweep("cdp", ts=10)
        shadow.ingest_page("cdp", [row], offset=0, last=True, step=1, ts=10)
        shadow.begin_sweep("cdp", ts=20)
        shadow.complete_sweep("cdp", ts=20)
        self.assertEqual(shadow.resource_status("https://wx.example/gone"), "retired")
        later = catalog.slim_item(_item("https://wx.example/gone", "weather returned"), "base")
        shadow.upsert_item(later, source="cdp", ts=30)
        self.assertEqual(shadow.resource_status("https://wx.example/gone"), "active")

    def test_claim_change_events_from_hash_not_raw_payload(self):
        first = catalog.slim_item(
            _item(
                "https://wx.example/price",
                "weather",
                accepts=[{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000", "asset": payment.USDC_BASE}],
            ),
            "base",
        )
        shadow.upsert_item(first, source="cdp")
        second = catalog.slim_item(
            _item(
                "https://wx.example/price",
                "weather",
                accepts=[
                    {"network": "eip155:8453", "payTo": "0xdef", "amount": "20000", "asset": payment.USDC_BASE},
                    {
                        "network": payment.SOLANA_MAINNET,
                        "payTo": SOL_PAYTO,
                        "amount": "10000",
                        "asset": payment.USDC_SOLANA_MINT,
                    },
                ],
            ),
            "base",
        )
        shadow.upsert_item(second, source="cdp")
        kinds = {e["event"] for e in shadow.claim_events("https://wx.example/price")}
        self.assertIn(shadow.EVENT_PRICE, kinds)
        self.assertIn(shadow.EVENT_PAYTO, kinds)
        self.assertIn(shadow.EVENT_RAIL_ADDED, kinds)
        for ev in shadow.claim_events("https://wx.example/price"):
            blob = str(ev.get("detail") or "")
            self.assertNotIn("inputSchema", blob)
            self.assertLess(len(blob), 500)


class FederatedWorkingSetTests(_IsolatedCatalog):
    def test_unions_local_and_live_without_loading_world(self):
        seeded = catalog.slim_item(
            _item("https://local.example/weather", "local-only weather station"),
            "base",
        )
        shadow.upsert_item(seeded, source="cdp")
        catalog.reset_working_set_peak()

        def fake_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname or ""
            if "api.cdp.coinbase.com" in host:
                return {
                    "resources": [_item("https://live.example/weather", "live weather feed")],
                    "partialResults": False,
                    "searchMethod": "hybrid",
                }
            return {"resources": [], "partialResults": False}

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather")
        urls = {probe._resource_url(i) for i in working["items"]}
        self.assertIn("https://local.example/weather", urls)
        self.assertIn("https://live.example/weather", urls)
        self.assertLessEqual(len(working["items"]), catalog.WORKING_SET_HARD_CAP)
        self.assertLess(catalog.WORKING_SET_HARD_CAP, 1000)
        self.assertLess(catalog.working_set_peak(), 44_000)
        self.assertLessEqual(catalog.working_set_peak(), catalog.WORKING_SET_HARD_CAP)
        self.assertFalse(hasattr(catalog, "MAX_ITEMS") and catalog.MAX_ITEMS >= 30_000)
        self.assertIsNone(catalog.peek_index())
        self.assertEqual(catalog.get_index().get("items"), [])

    def test_ingest_one_page_streams_and_allowlists(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            return {
                "items": [_item("https://cdp.example/weather", "weather page")],
                "pagination": {"limit": 100, "offset": 0, "total": 1},
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            result = catalog.ingest_one_page("cdp")
        self.assertEqual(result.get("upserted"), 1)
        self.assertTrue(result.get("complete"))
        self.assertEqual(shadow.resource_status("https://cdp.example/weather"), "active")
        self.assertTrue(all("api.cdp.coinbase.com" in u for u in calls))
        self.assertFalse(any("page=" in u or "cursor=" in u for u in calls))

        with patch("live402.probe._fetch_catalog_payload") as fetch:
            bad = catalog.ingest_one_page("evil")
            fetch.assert_not_called()
        self.assertEqual(bad.get("error"), "unknown_source")


class ObservedInvariantTests(_IsolatedCatalog):
    def test_accept_claims_never_used_as_observed_payment_options(self):
        url = "https://wx.example/claimed-sol"
        item = catalog.slim_item(_catalog_base_and_solana(url), "base")
        shadow.upsert_item(item, source="cdp")
        claims = shadow.accept_claims(url)
        self.assertTrue(any(c.get("rail") == "solana" for c in claims))

        envelope = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount": "20000",
                    "payTo": OBS_BASE_PAYTO,
                    "extra": {"facilitator": CDP, "displayAmount": "$0.02"},
                }
            ],
        }
        result = {
            "url": url,
            "live": True,
            "status": 402,
            "has_402_challenge": True,
            "payTo": OBS_BASE_PAYTO,
            "latency_ms": 10,
            "envelope": envelope,
            "rail": "base",
            "amount": "20000",
            "asset": payment.USDC_BASE,
        }
        shadowed = shadow.get_resource(url)
        result = probe.attach_catalog_fields(result, shadowed)
        result = probe.attach_invocable_target(result, shadowed, envelope)
        observed = payment.payment_options_from_result(result)
        self.assertEqual([o.get("rail") for o in observed], ["base"])
        self.assertNotIn("solana", {o.get("rail") for o in observed})
        selected = select.pick_selected_payment(result, "cheapest", None)
        self.assertEqual(selected["rail"], "base")
        self.assertEqual(selected["amount_atomic"], 20000)
        self.assertEqual(selected["payTo"], OBS_BASE_PAYTO)
        claimed_rails = {o.get("rail") for o in (result.get("claimed") or {}).get("payment_options") or []}
        self.assertIn("solana", claimed_rails)

    def test_preview_still_not_probed(self):
        slim = catalog.slim_item(_item("https://wx.example/preview", "weather preview"), "base")
        shadow.upsert_item(slim, source="cdp")
        body = pulse.preview_need("weather")
        self.assertTrue(body.get("not_probed"))
        self.assertNotIn("selected_payment", body)
        for hit in body.get("hits") or []:
            self.assertNotIn("selected_payment", hit)

    def test_ssrf_allowlist_unchanged(self):
        self.assertEqual(
            set(probe.CATALOG_HOSTS),
            {
                "api.cdp.coinbase.com",
                "facilitator.payai.network",
                "facilitator.goplausible.xyz",
            },
        )
        self.assertFalse(probe.catalog_url_allowed("https://evil.example/discovery/search"))
        self.assertFalse(
            probe.catalog_url_allowed(
                "http://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
            )
        )
        self.assertIsNone(catalog.search_url("https://evil.example/discovery/search", "weather", 20, 0))
        self.assertIsNone(catalog.page_url("https://evil.example/discovery/resources", 100, 0))
        with patch("live402.probe._fetch_catalog_payload") as fetch:
            catalog.ingest_one_page("evil")
            fetch.assert_not_called()

    def test_start_refresher_does_not_walk_in_fixture_mode(self):
        self.assertTrue(fixtures.fixture_mode())
        with patch.object(catalog, "ingest_one_page") as ingest, patch.object(
            catalog, "trickle_once"
        ) as trickle, patch.object(catalog, "fetch_rail") as fetch_rail:
            catalog.start_refresher()
            ingest.assert_not_called()
            trickle.assert_not_called()
            fetch_rail.assert_not_called()
        self.assertFalse(catalog.refresh_in_progress())


class ShadowHistorySeparationTests(_IsolatedCatalog):
    def test_catalog_db_is_not_observation_db(self):
        self.assertNotEqual(shadow.db_path(), history.db_path())
        self.assertTrue(shadow.db_path().endswith(".sqlite"))
        self.assertNotIn("live402-history", shadow.db_path())
        self.assertNotEqual(shadow.VOLUME_DB, history.VOLUME_DB)

    def test_probe_sets_verification_clock_only(self):
        url = "https://wx.example/verified"
        slim = catalog.slim_item(_item(url, "weather verified"), "base")
        shadow.upsert_item(slim, source="cdp", ts=50)
        fd, hist = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        prev = os.environ.get("LIVE402_HISTORY_DB")
        os.environ["LIVE402_HISTORY_DB"] = hist
        history.reset()
        try:
            history.record_probe(
                url,
                {
                    "url": url,
                    "live": True,
                    "status": 402,
                    "payTo": OBS_BASE_PAYTO,
                    "amount": "20000",
                    "latency_ms": 9,
                    "envelope": {
                        "accepts": [
                            {
                                "network": payment.BASE_CAIP2,
                                "asset": payment.USDC_BASE,
                                "amount": "20000",
                                "payTo": OBS_BASE_PAYTO,
                            }
                        ]
                    },
                },
            )
            c = shadow.clocks(url)
            self.assertIsNotNone(c["verification"])
            self.assertGreaterEqual(c["verification"], c["claim"] or 0)
        finally:
            history.reset()
            if prev is None:
                os.environ.pop("LIVE402_HISTORY_DB", None)
            else:
                os.environ["LIVE402_HISTORY_DB"] = prev
            for p in (hist, hist + "-wal", hist + "-shm"):
                try:
                    os.remove(p)
                except OSError:
                    pass


class CatalogBenchTests(_IsolatedCatalog):
    """Measured disk / RAM / FTS. No guessed 44k figures."""

    N = 400

    def test_bounded_ingest_disk_ram_fts(self):
        n = self.N
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.perf_counter()
        page = []
        for i in range(n):
            page.append(
                catalog.slim_item(
                    _item(
                        "https://bench.example/w/%d" % i,
                        "hourly weather forecast station %d" % i,
                        serviceName="Wx %d" % i,
                        tags=["weather", "forecast", "station%d" % (i % 7)],
                    ),
                    "base",
                )
            )
            if len(page) >= 50:
                shadow.upsert_items(page, source="cdp")
                page.clear()
        if page:
            shadow.upsert_items(page, source="cdp")
            page.clear()
        ingest_s = time.perf_counter() - t0
        st = shadow.stats()
        self.assertEqual(st["resources"], n)
        bytes_n = st["bytes"]
        self.assertGreater(bytes_n, 0)
        extrapolated_44k = int(bytes_n * (44_000 / float(n)))
        self.assertLess(extrapolated_44k, 1_000_000_000)

        latencies = []
        for _ in range(20):
            t1 = time.perf_counter()
            hits = shadow.fts_search("weather")
            latencies.append((time.perf_counter() - t1) * 1000.0)
        self.assertTrue(hits)
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        self.assertLess(p50, 250.0)

        catalog.reset_working_set_peak()

        def fake_payload(url, timeout, read_limit=None):
            return {
                "resources": [_item("https://live.example/weather", "live weather")],
                "partialResults": False,
                "searchMethod": "hybrid",
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather")
        self.assertLessEqual(len(working["items"]), catalog.WORKING_SET_HARD_CAP)
        self.assertLess(catalog.working_set_peak(), 44_000)
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report = {
            "n": n,
            "sqlite_bytes": bytes_n,
            "sqlite_kb": round(bytes_n / 1024.0, 1),
            "extrapolated_44k_bytes": extrapolated_44k,
            "extrapolated_44k_mb": round(extrapolated_44k / (1024.0 * 1024.0), 2),
            "ingest_s": round(ingest_s, 3),
            "fts_p50_ms": round(p50, 3),
            "fts_max_ms": round(latencies[-1], 3),
            "working_set_peak": catalog.working_set_peak(),
            "working_set_hard_cap": catalog.WORKING_SET_HARD_CAP,
            "ru_maxrss_kb_before": rss_before,
            "ru_maxrss_kb_after": rss_after,
            "query_items": len(working["items"]),
        }
        print("CATALOG_BENCH %s" % report)
        self.assertLess(report["working_set_peak"], 44_000)
        self.assertLess(report["extrapolated_44k_mb"], 1024)


if __name__ == "__main__":
    unittest.main()
