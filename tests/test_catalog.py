"""Request-time catalog query + stronger matching. No network."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, fixtures, payment, probe, select


CDP = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
CDP_SEARCH = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
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

    def test_cdp_search_query_ranks_weather(self):
        weather_url = "https://wx.example/forecast"
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            parsed = urlparse(url)
            qs = _qs(url)
            self.assertNotIn("page", qs)
            self.assertNotIn("cursor", qs)
            if parsed.path.endswith("/discovery/search"):
                self.assertEqual(qs.get("query"), "weather")
                return {
                    "resources": [
                        _item("https://unrelated.example/search", "web search index"),
                        _item(weather_url, "hourly weather forecast"),
                    ],
                    "partialResults": False,
                    "searchMethod": "hybrid",
                }
            self.fail("request-time query must not walk /discovery/resources")
            return {"items": []}

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather", prefer_network="base")
        self.assertTrue(calls)
        self.assertTrue(all("/discovery/search" in u for u in calls))
        self.assertFalse(any("/discovery/resources" in u for u in calls))
        urls = [probe._resource_url(i) for i in working["items"]]
        self.assertIn(weather_url, urls)
        ranked = probe.rank_resources("weather", working["items"])
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

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            idx = catalog.query_for_need("weather")
        self.assertEqual(len(idx["by_rail"]["base"]), 1)
        self.assertEqual(len(idx["by_rail"]["solana"]), 1)
        merged = [i for i in idx["items"] if probe._resource_url(i) == shared]
        self.assertEqual(len(merged), 1)
        self.assertIn("solana", merged[0].get("also_on") or [])
        self.assertIn("base", merged[0].get("rails") or [])
        self.assertIn("solana", merged[0].get("rails") or [])

    def test_dedup_search_payloads(self):
        shared = "https://both.example/weather"

        def fake_payload(url, timeout, read_limit=None):
            host = urlparse(url).hostname
            path = urlparse(url).path
            if host == "api.cdp.coinbase.com" and path.endswith("/search"):
                return {
                    "resources": [_item(shared, "weather on base")],
                    "partialResults": False,
                    "searchMethod": "hybrid",
                }
            if host == "facilitator.payai.network" and path.endswith("/search"):
                return {
                    "items": [_item(shared, "weather on solana")],
                    "partialResults": False,
                }
            if host == "facilitator.goplausible.xyz":
                return {"items": [], "pagination": {"limit": 100, "offset": 0, "total": 0}}
            return {"items": [], "pagination": {"limit": 100, "offset": 0, "total": 0}}

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            idx = catalog.query_for_need("weather")
        merged = [i for i in idx["items"] if probe._resource_url(i) == shared]
        self.assertEqual(len(merged), 1)
        self.assertIn("solana", merged[0].get("also_on") or [])

    def test_start_refresher_does_not_walk(self):
        with patch.object(catalog, "fetch_rail") as fetch_rail, patch.object(
            catalog, "query_for_need"
        ) as query, patch.object(catalog, "refresh") as refresh:
            catalog.start_refresher()
            fetch_rail.assert_not_called()
            query.assert_not_called()
            refresh.assert_not_called()
        self.assertFalse(catalog.refresh_in_progress())
        self.assertIsNone(catalog.peek_index())

    def test_get_index_and_refresh_do_not_crawl(self):
        with patch("live402.probe._fetch_catalog_payload") as fetch, patch.object(
            catalog, "fetch_rail"
        ) as fetch_rail:
            idx = catalog.get_index()
            refreshed = catalog.refresh()
            fetch.assert_not_called()
            fetch_rail.assert_not_called()
        self.assertEqual(idx.get("items"), [])
        self.assertEqual(refreshed.get("items"), [])
        self.assertIsNone(catalog.peek_index())

    def test_query_does_not_accumulate_max_items_across_rails(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            qs = _qs(url)
            self.assertNotIn("page", qs)
            self.assertNotIn("cursor", qs)
            offset = int(qs.get("offset") or 0)
            self.assertLess(offset, catalog.QUERY_MAX_PAGES * catalog.PAGE_SIZE)
            host = urlparse(url).hostname
            path = urlparse(url).path
            n = catalog.QUERY_MAX_ITEMS
            if host == "api.cdp.coinbase.com" and path.endswith("/search"):
                items = [
                    _item("https://cdp.example/w/%d" % i, "weather %d" % i) for i in range(20)
                ]
                return {"resources": items, "partialResults": True, "searchMethod": "hybrid"}
            if path.endswith("/search"):
                return {}
            items = [
                _item("https://%s.example/w/%d" % (host, offset + i), "weather %d" % i)
                for i in range(n)
            ]
            return {
                "items": items,
                "pagination": {"limit": catalog.PAGE_SIZE, "offset": offset, "total": 28000},
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather")
        self.assertLessEqual(len(working["items"]), catalog.QUERY_MAX_ITEMS * 3)
        self.assertLessEqual(len(working["by_rail"]["base"]), catalog.QUERY_MAX_ITEMS)
        self.assertLessEqual(len(working["by_rail"]["solana"]), catalog.QUERY_MAX_ITEMS)
        self.assertLessEqual(len(working["by_rail"]["algorand"]), catalog.QUERY_MAX_ITEMS)
        self.assertFalse(any(_qs(u).get("offset") == "200" for u in calls))
        self.assertLessEqual(len(calls), 1 + 2 * (1 + catalog.QUERY_MAX_PAGES))
        self.assertTrue(any("/discovery/search" in u for u in calls))

    def test_search_url_allowlist_no_page_cursor(self):
        url = catalog.search_url(CDP_SEARCH + "?page=3&cursor=abc&limit=20", "weather", 20, 0)
        self.assertIsNotNone(url)
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs.get("query"), ["weather"])
        self.assertEqual(qs.get("limit"), ["20"])
        self.assertNotIn("page", qs)
        self.assertNotIn("cursor", qs)
        self.assertIsNone(
            catalog.search_url("https://evil.example/discovery/search", "weather", 20, 0)
        )
        self.assertIsNone(catalog.page_url("https://evil.example/discovery/resources", 100, 0))

    def test_prefer_network_still_queries_all_rails(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            host = urlparse(url).hostname or ""
            if "payai" in host:
                row = _item("https://wx.example/sol-weather", "weather")
                row["accepts"] = [{"network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "payTo": "So1", "amount": "10000"}]
            elif "goplausible" in host:
                row = _item("https://wx.example/algo-weather", "weather")
                row["accepts"] = [{"network": "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", "payTo": "AL1", "amount": "10000"}]
            else:
                row = _item("https://wx.example/base-weather", "weather")
            return {
                "resources": [row],
                "partialResults": False,
                "searchMethod": "hybrid",
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather", prefer_network="solana")
        self.assertTrue(calls)
        self.assertTrue(any("api.cdp.coinbase.com" in u for u in calls))
        self.assertTrue(any("payai" in u for u in calls))
        self.assertTrue(any("goplausible" in u for u in calls))
        rails_present = {probe._item_rail(i) for i in working["items"]}
        self.assertIn("base", rails_present)
        self.assertIn("solana", rails_present)
        self.assertIn("algorand", rails_present)
        self.assertTrue(working["by_rail"]["base"])
        self.assertTrue(working["by_rail"]["solana"])
        self.assertTrue(working["by_rail"]["algorand"])

    def test_networks_restricts_rails_queried(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            return {
                "resources": [_item("https://wx.example/sol-weather", "weather")],
                "partialResults": False,
                "searchMethod": "hybrid",
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather", networks=["solana"])
        self.assertTrue(calls)
        self.assertTrue(all("payai" in u for u in calls))
        self.assertFalse(any("api.cdp.coinbase.com" in u for u in calls))
        self.assertFalse(any("goplausible" in u for u in calls))
        self.assertEqual(working["by_rail"]["base"], [])
        self.assertEqual(working["by_rail"]["algorand"], [])
        self.assertTrue(working["by_rail"]["solana"])
        rails_present = {probe._item_rail(i) for i in working["items"]}
        self.assertEqual(rails_present, {"solana"})
        self.assertEqual(working["via"].get("solana"), "search")
        self.assertNotIn("base", working["via"])
        self.assertNotIn("algorand", working["via"])

    def test_prefer_network_fixture_keeps_other_rails(self):
        self.assertTrue(fixtures.fixture_mode())
        working = catalog.query_for_need("weather", prefer_network="solana")
        rails_present = {probe._item_rail(i) for i in working["items"]}
        self.assertIn("base", rails_present)
        self.assertIn("algorand", rails_present)

    def test_networks_fixture_drops_other_rails(self):
        self.assertTrue(fixtures.fixture_mode())
        working = catalog.query_for_need("weather", networks=["solana"])
        rails_present = {probe._item_rail(i) for i in working["items"]}
        self.assertTrue(rails_present)
        self.assertEqual(rails_present, {"solana"})
        self.assertEqual(working["by_rail"]["base"], [])
        self.assertEqual(working["by_rail"]["algorand"], [])

    def test_discovery_telemetry_search_vs_pages_and_exhaustive(self):
        calls = []

        def fake_payload(url, timeout, read_limit=None):
            calls.append(url)
            host = urlparse(url).hostname or ""
            path = urlparse(url).path
            if "payai" in host and path.endswith("/search"):
                return {"error": "nope"}
            if "payai" in host:
                return {
                    "items": [_item("https://wx.example/sol-page", "weather")],
                    "pagination": {"limit": 100, "offset": 0, "total": 1},
                }
            if "goplausible" in host and path.endswith("/search"):
                return {
                    "resources": [_item("https://wx.example/algo-search", "weather")],
                    "partialResults": False,
                    "pagination": {"limit": 20, "offset": 0, "total": 1},
                }
            return {
                "resources": [_item("https://wx.example/base-search", "weather")],
                "partialResults": False,
                "searchMethod": "hybrid",
                "pagination": {"limit": 20, "offset": 0, "total": 1},
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather")
        disc = working.get("discovery") or {}
        self.assertEqual(disc["base"]["via"], "search")
        self.assertEqual(disc["solana"]["via"], "pages")
        self.assertEqual(disc["algorand"]["via"], "search")
        self.assertEqual(disc["base"]["returned"], 1)
        self.assertEqual(disc["base"]["upstream_total"], 1)
        self.assertFalse(disc["base"]["truncated"])
        self.assertTrue(catalog.discovery_exhaustive(working))
        self.assertEqual(
            catalog.public_discovery_via(working),
            {"base": "search", "solana": "pages", "algorand": "search"},
        )

    def test_discovery_not_exhaustive_when_total_unknown_or_truncated(self):
        def fake_payload(url, timeout, read_limit=None):
            items = [_item("https://wx.example/w%d" % i, "weather") for i in range(catalog.SEARCH_LIMIT)]
            return {
                "resources": items,
                "partialResults": True,
                "searchMethod": "hybrid",
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._fetch_catalog_payload", side_effect=fake_payload
        ):
            working = catalog.query_for_need("weather", networks=["base"])
        row = (working.get("discovery") or {}).get("base") or {}
        self.assertEqual(row.get("via"), "search")
        self.assertTrue(row.get("truncated"))
        self.assertIsNone(row.get("upstream_total"))
        self.assertFalse(catalog.discovery_exhaustive(working))

    def test_merge_items_reuses_by_rail_dicts(self):
        item = catalog.slim_item(_item("https://a.example/x", "alpha"), "base")
        by_rail = {"base": [item], "solana": [], "algorand": []}
        merged = catalog._merge_items(by_rail)
        self.assertEqual(len(merged), 1)
        self.assertIs(merged[0], item)
        self.assertEqual(merged[0]["rails"], ["base"])

    def test_merge_keeps_both_rail_payment_options(self):
        from live402 import payment

        url = "https://multi.example/weather"
        base = catalog.slim_item(
            {
                "url": url,
                "description": "weather base",
                "accepts": [
                    {
                        "network": payment.BASE_CAIP2,
                        "asset": payment.USDC_BASE,
                        "amount": "20000",
                        "payTo": "0xabc",
                    }
                ],
            },
            "base",
        )
        sol = catalog.slim_item(
            {
                "url": url,
                "description": "weather solana",
                "accepts": [
                    {
                        "network": payment.SOLANA_MAINNET,
                        "asset": payment.USDC_SOLANA_MINT,
                        "amount": "10000",
                        "payTo": payment.DEFAULT_PAYTO_SOLANA,
                    }
                ],
            },
            "solana",
        )
        merged = catalog._merge_items({"base": [base], "solana": [sol], "algorand": []})
        self.assertEqual(len(merged), 1)
        assets = {a.get("asset") for a in (merged[0].get("accepts") or [])}
        self.assertEqual(assets, {payment.USDC_BASE, payment.USDC_SOLANA_MINT})
        self.assertIn("solana", merged[0].get("also_on") or [])

    def test_query_caps_are_need_scoped(self):
        self.assertEqual(catalog.QUERY_MAX_ITEMS, 100)
        self.assertEqual(catalog.QUERY_MAX_PAGES, 2)
        self.assertEqual(catalog.SEARCH_LIMIT, 20)
        self.assertEqual(catalog.PAGE_READ_LIMIT, 1_048_576)
        self.assertFalse(hasattr(catalog, "MAX_ITEMS") and catalog.MAX_ITEMS >= 30_000)
        self.assertFalse(hasattr(catalog, "INDEX_TTL"))

    def test_route_need_one_scoped_query_not_world_ingest(self):
        calls = []

        def fake_query(need, prefer_network=None, networks=None):
            calls.append((need, prefer_network, networks))
            return {
                "items": [
                    catalog.slim_item(
                        _item("https://wx.example/forecast", "hourly weather forecast"),
                        "base",
                    )
                ]
            }

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            return {
                "live": True,
                "url": url,
                "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                "invocable": False,
                "status": 402,
                "has_402_challenge": True,
                "probed_at": "2026-08-31T00:00:00Z",
                "latency_ms": 10,
                "rail": "base",
                "amount": "10000",
                "asset": payment.USDC_BASE,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": payment.BASE_CAIP2,
                        "asset": payment.USDC_BASE,
                        "amount": "10000",
                        "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                        "maxTimeoutSeconds": 60,
                    }
                ],
                "envelope": {
                    "x402Version": 2,
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": payment.BASE_CAIP2,
                            "asset": payment.USDC_BASE,
                            "amount": "10000",
                            "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                            "maxTimeoutSeconds": 60,
                        }
                    ],
                },
            }

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe.fixtures.fixture_mode", return_value=False
        ), patch("live402.catalog.query_for_need", side_effect=fake_query), patch(
            "live402.catalog.get_index"
        ) as get_idx, patch("live402.probe._fetch_catalog_payload") as fetch, patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            body = probe.route_need("weather", prefer_network="base")
        self.assertEqual(calls, [("weather", "base", None)])
        get_idx.assert_not_called()
        fetch.assert_not_called()
        self.assertTrue(body.get("live"))
        self.assertEqual(body.get("url"), "https://wx.example/forecast")

    def test_route_need_passes_networks_restriction(self):
        calls = []

        def fake_query(need, prefer_network=None, networks=None):
            calls.append((need, prefer_network, networks))
            return {
                "items": [
                    catalog.slim_item(
                        _item("https://wx.example/sol-weather", "hourly weather forecast"),
                        "solana",
                    )
                ]
            }

        def fake_probe(url, catalog_item=None, deadline=None, **kwargs):
            return {
                "live": True,
                "url": url,
                "payTo": payment.DEFAULT_PAYTO_SOLANA,
                "invocable": False,
                "status": 402,
                "has_402_challenge": True,
                "probed_at": "2026-08-31T00:00:00Z",
                "latency_ms": 10,
                "rail": "solana",
                "amount": "10000",
                "asset": payment.USDC_SOLANA_MINT,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": payment.SOLANA_MAINNET,
                        "asset": payment.USDC_SOLANA_MINT,
                        "amount": "10000",
                        "payTo": payment.DEFAULT_PAYTO_SOLANA,
                        "maxTimeoutSeconds": 60,
                    }
                ],
                "envelope": {
                    "x402Version": 2,
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": payment.SOLANA_MAINNET,
                            "asset": payment.USDC_SOLANA_MINT,
                            "amount": "10000",
                            "payTo": payment.DEFAULT_PAYTO_SOLANA,
                            "maxTimeoutSeconds": 60,
                        }
                    ],
                },
            }

        cons = select.parse_constraints({"networks": ["solana"]})
        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe.fixtures.fixture_mode", return_value=False
        ), patch("live402.catalog.query_for_need", side_effect=fake_query), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            body = probe.route_need(
                "weather", prefer_network="solana", constraints=cons
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "weather")
        self.assertEqual(calls[0][1], "solana")
        self.assertEqual(set(calls[0][2] or []), {"solana"})
        self.assertTrue(body.get("live"))


if __name__ == "__main__":
    unittest.main()
