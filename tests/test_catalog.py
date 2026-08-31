"""Full catalog index + stronger matching. No network."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, fixtures, probe


CDP = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
PAYAI = "https://facilitator.payai.network/discovery/resources"
GOPL = "https://facilitator.goplausible.xyz/discovery/resources"


def _qs(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _item(url, description="", **extra):
    row = {
        "url": url,
        "description": description,
        "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
    }
    row.update(extra)
    return row


class CatalogIndexTests(unittest.TestCase):
    def setUp(self):
        catalog.reset_index()

    def tearDown(self):
        catalog.reset_index()

    def test_two_page_weather_on_page_1_ranks_first(self):
        weather_url = "https://wx.example/forecast"

        def fake_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname
            offset = int(_qs(url).get("offset") or 0)
            if host != "api.cdp.coinbase.com":
                return {"items": [], "pagination": {"limit": 100, "offset": offset, "total": 0}}
            if offset == 0:
                return {
                    "items": [
                        _item("https://unrelated.example/search", "web search index"),
                        _item("https://unrelated.example/docs", "billing docs"),
                    ],
                    "pagination": {"limit": 100, "offset": 0, "total": 101},
                    "x402Version": 2,
                }
            if offset == 100:
                return {
                    "items": [
                        _item(weather_url, "hourly weather forecast"),
                    ],
                    "pagination": {"limit": 100, "offset": 100, "total": 101},
                    "x402Version": 2,
                }
            return {"items": [], "pagination": {"limit": 100, "offset": offset, "total": 101}}

        with patch("live402.probe._fetch_catalog_payload", side_effect=fake_payload):
            rail = catalog.fetch_rail("base", CDP)
            idx = catalog.get_index()
        urls = [probe._resource_url(i) for i in rail["items"]]
        self.assertIn(weather_url, urls)
        idx_urls = [probe._resource_url(i) for i in idx["items"]]
        self.assertIn(weather_url, idx_urls)
        ranked = probe.rank_resources("weather", idx["items"])
        self.assertTrue(ranked)
        self.assertEqual(probe._resource_url(ranked[0]), weather_url)

    def test_missing_pagination_short_page_stops_no_invented_total(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            return {"items": [_item("https://a.example/one", "alpha"), _item("https://a.example/two", "beta")]}

        with patch("live402.probe._fetch_catalog_payload", side_effect=fake_payload):
            result = catalog.fetch_rail("base", CDP)
        pag = catalog.parse_pagination(
            {"items": [_item("https://a.example/one"), _item("https://a.example/two")]},
            requested_limit=100,
        )
        self.assertIsNone(pag["total"])
        self.assertTrue(pag["last"])
        self.assertFalse(pag["has_pagination"])
        self.assertIsNone(result["total"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(_qs(calls[0]).get("offset"), "0")

    def test_catalog_url_allowed_fail_closed_page_url_evil_not_fetched(self):
        self.assertFalse(probe.catalog_url_allowed("https://evil.example/discovery/resources"))
        self.assertFalse(
            probe.catalog_url_allowed(
                "http://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
            )
        )
        self.assertIsNone(catalog.page_url("https://evil.example/discovery/resources", 100, 0))
        self.assertIsNone(
            catalog.page_url(
                "http://api.cdp.coinbase.com/platform/v2/x402/discovery/resources", 100, 0
            )
        )
        with patch("live402.probe._fetch_catalog_payload") as fetch:
            result = catalog.fetch_rail("evil", "https://evil.example/discovery/resources")
            fetch.assert_not_called()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["error"], "not_allowlisted")

    def test_page_url_only_limit_offset(self):
        url = catalog.page_url(CDP + "?page=3&cursor=abc&limit=20", 100, 200)
        self.assertIsNotNone(url)
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs.get("limit"), ["100"])
        self.assertEqual(qs.get("offset"), ["200"])
        self.assertNotIn("page", qs)
        self.assertNotIn("cursor", qs)

    def test_slim_item_drops_huge_schema_blob(self):
        huge = {
            "type": "object",
            "properties": {("k%d" % i): {"type": "string", "description": "x" * 80} for i in range(400)},
        }
        item = {
            "resource": "https://payai.example/tool",
            "tags": ["search"],
            "inputSchema": huge,
            "outputSchema": huge,
            "extensions": {
                "bazaar": {
                    "schema": huge,
                    "info": {
                        "input": {
                            "method": "POST",
                            "toolName": "web_search",
                            "type": "http",
                            "inputSchema": huge,
                        }
                    },
                }
            },
        }
        slim = catalog.slim_item(item, "solana")
        blob = json.dumps(slim)
        self.assertNotIn("inputSchema", slim)
        self.assertNotIn("outputSchema", slim)
        self.assertLess(len(blob), 8000)
        self.assertTrue(slim["_input_schema_present"])
        self.assertTrue(slim["_output_schema_present"])
        bazaar = ((slim.get("extensions") or {}).get("bazaar") or {})
        self.assertNotIn("schema", bazaar)
        inp = ((bazaar.get("info") or {}).get("input") or {})
        self.assertEqual(inp.get("method"), "POST")
        self.assertEqual(inp.get("toolName"), "web_search")
        self.assertNotIn("inputSchema", inp)

    def test_classify_capability_weather_unknown_and_generic_url(self):
        cap, src = catalog.classify_capability(
            {"description": "hourly weather forecast for a city"}
        )
        self.assertEqual(cap, "travel.weather")
        self.assertEqual(src, "description")
        cap, src = catalog.classify_capability({"description": "", "tags": [], "url": "https://zz.example/x"})
        self.assertEqual(cap, "unknown")
        self.assertEqual(src, "unknown")
        cap, src = catalog.classify_capability(
            {"url": "https://api.example.com/v1/data", "description": ""}
        )
        self.assertEqual(cap, "unknown")

    def test_score_need_capability_schema_beats_loose_token(self):
        weather = {
            "url": "https://wx.example/svc",
            "description": "returns climate data",
            "capability": "travel.weather",
            "capability_source": "description",
            "_input_schema_present": True,
            "_output_schema_present": True,
            "_rail": "base",
            "accepts": [{"network": "base", "payTo": "0xabc"}],
        }
        overlap = {
            "url": "https://docs.example/weather-unrelated-blog",
            "description": "unrelated billing invoice about shipment weather delays",
            "capability": "payments.checkout",
            "capability_source": "description",
            "_input_schema_present": False,
            "_rail": "base",
            "accepts": [{"network": "base", "payTo": "0xabc"}],
        }
        self.assertGreater(
            probe.score_need("weather", weather), probe.score_need("weather", overlap)
        )
        ranked = probe.rank_resources("weather", [overlap, weather])
        self.assertEqual(probe._resource_url(ranked[0]), weather["url"])

    def test_traction_reads_quality_and_settle_count(self):
        self.assertEqual(probe._traction({"quality": {"l30DaysTotalCalls": 42}}), "42")
        self.assertEqual(probe._traction({"settleCount": 7}), "7")
        self.assertEqual(probe._traction({"x402Requests": 3}), "3")
        self.assertEqual(probe._traction({"resourceUrl": "https://go.example/x"}), "unknown")
        gopl = catalog.slim_item(
            {"resourceUrl": "https://go.example/weather", "settleCount": 11, "description": "weather"},
            "algorand",
        )
        self.assertEqual(probe._resource_url(gopl), "https://go.example/weather")
        self.assertEqual(probe._traction(gopl), "11")
        payai = catalog.slim_item(
            {
                "resource": "https://payai.example/search",
                "tags": ["search"],
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "metadata": {"requestCount": 5},
            },
            "solana",
        )
        self.assertEqual(probe._resource_url(payai), "https://payai.example/search")
        self.assertTrue(payai["_input_schema_present"])
        self.assertEqual(probe._traction(payai), "5")

    def test_fetch_discovery_fixture_mode(self):
        self.assertTrue(fixtures.fixture_mode())
        rows = probe.fetch_discovery()
        urls = [probe._resource_url(r) for r in rows]
        self.assertIn("https://fixture.402signal.local/weather", urls)

    def test_cdp_clamp_advances_by_returned_pagination_limit(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            offset = int(_qs(url).get("offset") or 0)
            limit = int(_qs(url).get("limit") or 0)
            self.assertEqual(limit, 1)
            if offset == 0:
                items = [
                    _item("https://cdp.example/item/%d" % i, "listing %d" % i) for i in range(20)
                ]
                return {
                    "items": items,
                    "pagination": {"limit": 20, "offset": 0, "total": 40},
                    "x402Version": 2,
                }
            if offset == 20:
                items = [
                    _item("https://cdp.example/item/%d" % (20 + i), "listing %d" % (20 + i))
                    for i in range(20)
                ]
                return {
                    "items": items,
                    "pagination": {"limit": 20, "offset": 20, "total": 40},
                    "x402Version": 2,
                }
            self.fail("unexpected offset %s" % offset)
            return {"items": []}

        with patch.object(catalog, "PAGE_SIZE", 1), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            result = catalog.fetch_rail("base", CDP)
        offsets = [_qs(u).get("offset") for u in calls]
        self.assertEqual(offsets, ["0", "20"])
        self.assertNotIn("1", offsets)
        self.assertEqual(len(result["items"]), 40)
        for url in calls:
            qs = parse_qs(urlparse(url).query)
            self.assertNotIn("page", qs)
            self.assertNotIn("cursor", qs)

    def test_dedup_across_rails_keeps_also_on(self):
        shared = "https://both.example/weather"

        def fake_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname
            if host == "api.cdp.coinbase.com":
                return {
                    "items": [_item(shared, "weather on base")],
                    "pagination": {"limit": 100, "offset": 0, "total": 1},
                }
            if host == "facilitator.payai.network":
                return {
                    "items": [_item(shared, "weather on solana")],
                    "pagination": {"limit": 100, "offset": 0, "total": 1},
                }
            return {"items": [], "pagination": {"limit": 100, "offset": 0, "total": 0}}

        with patch("live402.probe._fetch_catalog_payload", side_effect=fake_payload):
            idx = catalog.get_index()
        self.assertEqual(len(idx["by_rail"]["base"]), 1)
        self.assertEqual(len(idx["by_rail"]["solana"]), 1)
        merged = [i for i in idx["items"] if probe._resource_url(i) == shared]
        self.assertEqual(len(merged), 1)
        self.assertIn("solana", merged[0].get("also_on") or [])
        self.assertIn("base", merged[0].get("rails") or [])
        self.assertIn("solana", merged[0].get("rails") or [])

    def test_overlapping_refresh_and_get_index_one_walk(self):
        started = threading.Event()
        release = threading.Event()
        rails = []

        def fake_fetch_rail(rail, base):
            rails.append(rail)
            started.set()
            self.assertTrue(release.wait(5))
            return {"items": [], "error": None, "total": 0, "truncated": False}

        with patch.object(catalog, "fetch_rail", side_effect=fake_fetch_rail):
            t1 = threading.Thread(target=catalog.refresh)
            t1.start()
            self.assertTrue(started.wait(2))
            peeked = catalog.peek_index()
            self.assertIsNotNone(peeked)
            self.assertTrue(peeked.get("in_progress"))
            self.assertFalse(peeked.get("complete"))
            self.assertTrue(catalog.refresh_in_progress())

            out = {}

            def call_get():
                out["idx"] = catalog.get_index()

            t2 = threading.Thread(target=call_get)
            t2.start()
            time.sleep(0.05)
            release.set()
            t1.join(5)
            t2.join(5)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())

        self.assertEqual(rails, ["base", "solana", "algorand"])
        self.assertIsNotNone(out.get("idx"))
        self.assertFalse(out["idx"].get("in_progress"))
        self.assertFalse(catalog.refresh_in_progress())

    def test_peek_during_crawl_is_in_progress_not_empty_complete(self):
        started = threading.Event()
        release = threading.Event()

        def fake_fetch_rail(rail, base):
            started.set()
            self.assertTrue(release.wait(5))
            return {"items": [], "error": None, "total": 0, "truncated": False}

        with patch.object(catalog, "fetch_rail", side_effect=fake_fetch_rail):
            t = threading.Thread(target=catalog.refresh)
            t.start()
            self.assertTrue(started.wait(2))
            peeked = catalog.peek_index()
            release.set()
            t.join(5)

        self.assertTrue(peeked.get("in_progress"))
        self.assertFalse(peeked.get("complete"))
        self.assertNotEqual(peeked.get("complete"), True)

    def test_reset_index_clears_in_progress(self):
        started = threading.Event()
        release = threading.Event()

        def fake_fetch_rail(rail, base):
            started.set()
            self.assertTrue(release.wait(5))
            return {"items": [], "error": None, "total": 0, "truncated": False}

        with patch.object(catalog, "fetch_rail", side_effect=fake_fetch_rail):
            t = threading.Thread(target=catalog.refresh)
            t.start()
            self.assertTrue(started.wait(2))
            self.assertTrue(catalog.refresh_in_progress())
            catalog.reset_index()
            self.assertFalse(catalog.refresh_in_progress())
            self.assertIsNone(catalog.peek_index())
            release.set()
            t.join(5)
        catalog.reset_index()
        self.assertIsNone(catalog.peek_index())
        self.assertFalse(catalog.refresh_in_progress())

    def test_caps_unchanged_from_live_size_model(self):
        self.assertEqual(catalog.MAX_ITEMS, 30_000)
        self.assertEqual(catalog.MAX_PAGES, 400)
        self.assertEqual(catalog.PAGE_READ_LIMIT, 1_048_576)

    def test_merge_items_reuses_by_rail_dicts(self):
        item = catalog.slim_item(_item("https://a.example/x", "alpha"), "base")
        by_rail = {"base": [item], "solana": [], "algorand": []}
        merged = catalog._merge_items(by_rail)
        self.assertEqual(len(merged), 1)
        self.assertIs(merged[0], item)
        self.assertEqual(merged[0]["rails"], ["base"])

    def test_refresh_drops_prev_index_before_merge(self):
        def fake_payload(url, timeout, read_limit=None):
            return {
                "items": [_item("https://a.example/one", "alpha")],
                "pagination": {"limit": 100, "offset": 0, "total": 1},
            }

        with patch("live402.probe._fetch_catalog_payload", side_effect=fake_payload):
            first = catalog.refresh()
        prev = first
        self.assertIs(catalog.peek_index(), prev)

        seen = {}
        real_merge = catalog._merge_items

        def wrapped(by_rail):
            seen["index_is_prev"] = catalog._index is prev
            seen["index"] = catalog._index
            return real_merge(by_rail)

        with patch("live402.probe._fetch_catalog_payload", side_effect=fake_payload), patch.object(
            catalog, "_merge_items", side_effect=wrapped
        ):
            catalog.refresh()
        self.assertFalse(seen["index_is_prev"])
        self.assertIsNone(seen["index"])
        published = catalog.peek_index()
        self.assertIsNotNone(published)
        self.assertIs(published["items"][0], published["by_rail"]["base"][0])

    def test_rail_error_keeps_previous_items(self):
        def ok_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname
            if host == "api.cdp.coinbase.com":
                return {
                    "items": [_item("https://base.example/a", "base a")],
                    "pagination": {"limit": 100, "offset": 0, "total": 1},
                }
            if host == "facilitator.payai.network":
                return {
                    "items": [_item("https://sol.example/b", "sol b")],
                    "pagination": {"limit": 100, "offset": 0, "total": 1},
                }
            return {"items": [], "pagination": {"limit": 100, "offset": 0, "total": 0}}

        with patch("live402.probe._fetch_catalog_payload", side_effect=ok_payload):
            first = catalog.refresh()
        self.assertEqual(len(first["by_rail"]["base"]), 1)

        def then_fail_base(url, timeout, read_limit=None):
            host = urlparse(url).hostname
            if host == "api.cdp.coinbase.com":
                raise RuntimeError("boom")
            return ok_payload(url, timeout, read_limit)

        with patch("live402.probe._fetch_catalog_payload", side_effect=then_fail_base):
            second = catalog.refresh()
        self.assertEqual(second["errors"].get("base"), "fetch_failed")
        self.assertEqual(len(second["by_rail"]["base"]), 1)
        self.assertEqual(probe._resource_url(second["by_rail"]["base"][0]), "https://base.example/a")
        self.assertEqual(len(second["by_rail"]["solana"]), 1)


if __name__ == "__main__":
    unittest.main()
